# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The robot side of a run: fresh telemetry, bounded motion, and the guards
that stand between a model's proposal and the arm.

Every physical action goes through ArmRuntime. It is composed into a skill
rather than inherited from, so the skill stays a plain Skill and this stays
testable against a stub. The policy only ever proposes; nothing it returns
reaches a servo until the state it reasoned about has been re-read and still
holds, because a decision costs seconds and the scene moves.
"""

import base64
import json
import math
import queue
import threading
import time
import uuid
from typing import Any, NoReturn, Protocol

from brain_client.common.geometry import quat_to_rpy
from innate import HeadState, Manipulation, Mobility
from innate.demonstration import forward_poses
from innate.imitation_actions import (
    BASE_LATERAL_FLOOR,
    BASE_LATERAL_PER_M,
    BASE_YAW_FLOOR,
    BASE_YAW_PER_M,
    joint_target,
)


class ServoHardwareFault(ValueError):
    """A driver-reported servo hardware fault, distinct from missing telemetry."""


class LiveObservation:
    """Timestamped snapshots from dedicated subscriptions, with deterministic teardown."""

    def __init__(self):
        import rclpy
        from mars_msgs.msg import ArmStatus
        from nav_msgs.msg import Odometry
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CompressedImage, JointState

        self.node = rclpy.create_node("imitation_observation_" + uuid.uuid4().hex[:8])
        self.lock = threading.Lock()
        self.values = {}
        topics = [
            ("head", CompressedImage, "/mars/main_camera/left/image_raw/compressed"),
            ("wrist", CompressedImage, "/mars/arm/image_raw/compressed"),
            ("joints", JointState, "/mars/arm/state"),
            ("odom", Odometry, "/odom"),
            ("health", ArmStatus, "/mars/arm/status"),
        ]
        for key, message, topic in topics:
            self.node.create_subscription(
                message, topic, lambda msg, k=key: self._receive(k, msg), qos_profile_sensor_data
            )
        from std_srvs.srv import Trigger

        self.fix_error = self.node.create_client(Trigger, "/mars/arm/fix_error")
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    def _receive(self, key, msg):
        now = time.monotonic()
        stamp = None
        if hasattr(msg, "header"):
            stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        with self.lock:
            old = self.values.get(key)
            # Repeated source timestamps do not make a frozen camera fresh.
            if old and stamp is not None and stamp <= old[2]:
                return
            self.values[key] = (msg, now, stamp)

    def snapshot(self, after, xml):
        with self.lock:
            values = dict(self.values)
        now = time.monotonic()
        # Surface fresh hardware errors even while camera synchronization catches up.
        if "health" in values and now - values["health"][1] <= 3:
            self.check_health(values["health"][0])
        if set(values) != {"head", "wrist", "joints", "odom", "health"}:
            return None
        if any(now - item[1] > 1.5 or item[1] <= after for key, item in values.items() if key != "health"):
            return None
        if now - values["health"][1] > 3:
            return None
        stamps = [values[k][2] for k in ("head", "wrist", "joints")]
        ros_now = self.node.get_clock().now().nanoseconds * 1e-9
        if any(stamp is None or stamp <= 0 or not 0 <= ros_now - stamp <= 1.5 for stamp in stamps):
            return None
        if max(stamps) - min(stamps) > 0.25:
            return None
        joints = values["joints"][0]
        names, qpos = list(joints.name), list(joints.position)
        pose = forward_poses(xml, names, [qpos])[0]
        odom = values["odom"][0]
        p = odom.pose.pose.position
        o = odom.pose.pose.orientation
        if (
            not all(math.isfinite(v) for v in (p.x, p.y, o.x, o.y, o.z, o.w))
            or abs(math.sqrt(o.x**2 + o.y**2 + o.z**2 + o.w**2) - 1) > 0.01
        ):
            raise ValueError("Invalid base odometry")
        base = [p.x, p.y, quat_to_rpy(o.x, o.y, o.z, o.w)[2]]
        return {
            "pose": [*pose[:3], *quat_to_rpy(*pose[3:])],
            "qpos": qpos,
            "joint_names": names,
            "gripper": qpos[names.index("joint6")],
            "base": base,
            "images": {k: base64.b64encode(bytes(values[k][0].data)).decode() for k in ("head", "wrist")},
        }

    @staticmethod
    def check_health(health):
        if not health.is_ok and "hardware error:" in health.error:
            raise ServoHardwareFault(health.error)
        if not health.is_ok or not health.is_torque_enabled:
            raise ValueError("Arm unhealthy or torque disabled")

    def reload_failed_servos(self, skill):
        from std_srvs.srv import Trigger

        skill.check_cancelled()
        if not self.fix_error.service_is_ready():
            skill.fail("Targeted servo recovery service unavailable")
        future = self.fix_error.call_async(Trigger.Request())
        deadline = time.monotonic() + 8
        while not future.done():
            if time.monotonic() >= deadline:
                skill.fail("Servo recovery timed out; outcome unknown, no motion resumed")
            skill.sleep(0.05)
        skill.check_cancelled()
        response = future.result()
        if response is None:
            skill.fail("Servo recovery returned no response")
        result = json.loads(response.message)
        if not response.success or result.get("status") not in ("fixed", "no_errors"):
            skill.fail("Targeted servo recovery failed")
        if not isinstance(result.get("error_ids"), list):
            skill.fail("Invalid servo recovery response")
        return result

    def close(self):
        self.executor.shutdown(timeout_sec=3)
        self.thread.join(timeout=3)
        self.node.destroy_node()


class SkillHost(Protocol):
    """What ArmRuntime needs from the skill it acts through. Stating it as a
    protocol keeps the dependency one-way and documents the contract: the
    framework injects these onto the skill, and every guard below needs its
    cancellation and failure semantics."""

    manipulation: Manipulation
    mobility: Mobility
    head_position: HeadState
    node: Any
    logger: Any
    decision_timeout: float

    @property
    def name(self) -> str: ...
    def check_cancelled(self) -> None: ...
    def sleep(self, seconds: float) -> None: ...
    def fail(self, message: str) -> NoReturn: ...
    def feedback(self, message: str, image_b64: str | None = None) -> None: ...


class ArmRuntime:
    """Guarded execution for one run, acting through the skill that owns it.

    The skill holds the robot interfaces the framework injected and the
    cancellation and failure semantics every guard needs, so this takes it as a
    collaborator. The live monitor and the robot model are fixed for a run and
    live here rather than being threaded through every call.
    """

    def __init__(self, skill: SkillHost, monitor: LiveObservation, xml: str):
        self.skill = skill
        self.monitor = monitor
        self.xml = xml
        self._base_origin = None
        self._base_step_active = False

    def read(self, after):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.skill.check_cancelled()
            observation = self.monitor.snapshot(after, self.xml)
            if observation is not None:
                return observation
            self.skill.sleep(0.05)
        self.skill.fail("Fresh synchronized cameras, arm state and odometry unavailable")

    def observe(self, after):
        observation = self.read(after)
        base = observation["base"]
        if self._base_origin is None:
            self._base_origin = base[:]
        origin = self._base_origin
        angle = abs(math.atan2(math.sin(base[2] - origin[2]), math.cos(base[2] - origin[2])))
        if not getattr(self, "_base_step_active", False) and (math.dist(base[:2], origin[:2]) > 0.02 or angle > 0.05):
            self.skill.fail("Base moved while the arm was working")
        return observation

    def decide(self, policy, observation, history):
        result = queue.Queue(maxsize=1)

        def request():
            try:
                result.put((policy.decide(observation, history), None))
            except Exception as exc:
                # Do not serialize HTTP bodies/credentials into robot feedback.
                result.put((None, type(exc).__name__))

        threading.Thread(target=request, daemon=True).start()
        deadline = time.monotonic() + self.skill.decision_timeout
        while time.monotonic() < deadline:
            self.skill.check_cancelled()
            try:
                value, error = result.get_nowait()
                if error:
                    self.skill.fail("Demonstration model request failed (" + error + ")")
                return value
            except queue.Empty:
                self.skill.sleep(0.05)
        self.skill.fail("Demonstration model request timed out")

    @staticmethod
    def motion_outcome(status, reason, measured, target):
        return {
            "status": status,
            "reason": reason,
            "requested_pose": target,
            "measured_pose": measured["pose"],
            "measured_qpos": measured.get("qpos"),
            "position_error_m": math.dist(measured["pose"][:3], target[:3]),
            "orientation_error_rad": max(
                abs(math.atan2(math.sin(a - b), math.cos(a - b)))
                for a, b in zip(measured["pose"][3:], target[3:], strict=True)
            ),
        }

    def try_move(self, target, current):
        x, y, z, roll, pitch, yaw = target
        joints = self.skill.manipulation.ik(x, y, z, roll=roll, pitch=pitch, yaw=yaw)
        if joints is None:
            return self.motion_outcome(
                "unreachable", "IK rejected the requested EE pose; no movement issued", current, target
            )
        # The driver clamps a backward shoulder silently, so the move would run and
        # land short instead of failing. Judge the solution and refuse it here.
        floor = self.skill.manipulation.joint2_floor(joints[0])
        if joints[1] < floor:
            return self.motion_outcome(
                "unreachable",
                f"This pose needs the shoulder folded back to joint2={joints[1]:.2f}, past the {floor:.2f} the body "
                "allows here; no movement issued. Raise the target, bring it forward, or move the base.",
                current,
                target,
            )
        self.skill.check_cancelled()
        self.skill.manipulation.move_to(x, y, z, roll=roll, pitch=pitch, yaw=yaw, duration=1.5, block=False)
        # Health failures, cancellation, and uncertain/time-out outcomes still propagate.
        self.wait_motion()
        measured = self.observe(time.monotonic() - 0.2)
        if math.dist(measured["pose"][:3], target[:3]) > 0.015:
            return self.motion_outcome(
                "not_reached",
                "Move completed but missed the EE target by more than 1.5 cm; driver joint limits or tracking may prevent this pose",
                measured,
                target,
            )
        return self.motion_outcome("reached", "Measured EE position is within 1.5 cm of target", measured, target)

    def try_joint_step(self, decision, current):
        target = joint_target(decision, current)
        joint = decision.get("joint_step", decision["pose"])
        index = int(joint[0]) - 1
        # A shoulder already sagging a hair past the floor must not veto a step on
        # another joint, so only a joint2 step is held to the floor exactly.
        floor = self.skill.manipulation.joint2_floor(target[0])
        if target[1] < floor - (0.0 if index == 1 else 0.01):
            return dict(
                status="rejected",
                reason=f"Joint2 cannot fold back past {floor:.2f} rad at this joint1 — the body is in the way "
                "and the driver would clamp it; raise with joint3/joint4 or change direction",
                requested_qpos=target,
                measured_qpos=current["qpos"],
                measured_pose=current["pose"],
            )
        started = time.monotonic()
        measured = current
        try:
            while True:
                self.skill.check_cancelled()
                self.skill.manipulation.stream_joints(target[:5], max_speed=0.25)
                self.skill.sleep(0.04)
                measured = self.observe(time.monotonic() - 0.2)
                error = abs(measured["qpos"][index] - target[index])
                if error <= min(0.02, abs(joint[1]) / 3):
                    status = "reached"
                    break
                if time.monotonic() - started > 4.0:
                    status = "not_reached"
                    break
        finally:
            self.skill.manipulation.stream_stop()
        # The other four joints are commanded to hold. Streaming runs the driver's soft
        # teleop gains, so a loaded elbow settles a couple of degrees under its hold —
        # real, and not this step failing, so it is reported apart from the error.
        held_drift, held_joint = max(
            (abs(m - t), i + 1)
            for i, (m, t) in enumerate(zip(measured["qpos"][:5], target[:5], strict=True))
            if i != index
        )
        reason = (
            "Joint stream completed"
            if status == "reached"
            else f"Joint{index + 1} stopped {error:.3f} rad short of its target; use measured joints and images "
            "to choose a different direction or joint"
        )
        if held_drift > 0.02:
            reason += f"; joint{held_joint} sagged {held_drift:.3f} rad under load while commanded to hold"
        return dict(
            status=status,
            requested_qpos=target,
            measured_qpos=measured["qpos"],
            measured_pose=measured["pose"],
            joint_error_rad=error,
            held_drift_rad=held_drift,
            held_drift_joint=held_joint,
            reason=reason,
        )

    def try_base_step(self, distance, current):
        start = current["base"][:]
        measured = current
        started = time.monotonic()
        progress = 0.0
        lateral_allow = BASE_LATERAL_FLOOR + BASE_LATERAL_PER_M * abs(distance)
        angle_allow = BASE_YAW_FLOOR + BASE_YAW_PER_M * abs(distance)
        wandered = ""
        self._base_step_active = True
        try:
            while True:
                self.skill.check_cancelled()
                dx, dy = measured["base"][0] - start[0], measured["base"][1] - start[1]
                forward = dx * math.cos(start[2]) + dy * math.sin(start[2])
                lateral = -dx * math.sin(start[2]) + dy * math.cos(start[2])
                progress = math.copysign(1, distance) * forward
                angle = abs(
                    math.atan2(math.sin(measured["base"][2] - start[2]), math.cos(measured["base"][2] - start[2]))
                )
                if (
                    abs(lateral) > lateral_allow
                    or angle > angle_allow
                    or progress > abs(distance) + 0.02
                    or progress < -0.01
                ):
                    # Stopping where it is beats ending the run: the arm is unharmed and
                    # the next decision plans from the base pose actually measured.
                    wandered = f" after wandering {lateral:+.3f} m sideways and {angle:.3f} rad off heading"
                    status = "not_reached"
                    break
                if progress >= abs(distance) - 0.005:
                    status = "reached"
                    break
                if time.monotonic() - started > 5.0:
                    status = "not_reached"
                    break
                self.skill.mobility.send_cmd_vel(linear_x=math.copysign(0.04, distance), duration=0.15)
                self.skill.sleep(0.05)
                measured = self.observe(time.monotonic() - 0.2)
        finally:
            self.skill.mobility.stop()
            self._base_origin = measured["base"][:]
            self._base_step_active = False
        return dict(
            status=status,
            requested_base_distance_m=distance,
            measured_base_distance_m=math.copysign(progress, distance),
            measured_pose=measured["pose"],
            measured_qpos=measured["qpos"],
            reason="Base adjustment completed; reassess target distance"
            if status == "reached"
            else f"Base adjustment stopped{wandered}; reassess measured distance and choose another approach",
        )

    def wait_motion(self):
        deadline = time.monotonic() + 8
        while self.skill.manipulation.moving:
            self.skill.check_cancelled()
            if time.monotonic() > deadline:
                self.skill.fail("Arm motion timed out")
            self.observe(time.monotonic() - 1)
            self.skill.sleep(0.05)
        self.skill.manipulation.wait(timeout=1)
