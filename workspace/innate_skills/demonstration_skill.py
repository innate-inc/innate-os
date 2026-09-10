# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The robot side of a demonstration-conditioned run: fresh telemetry, bounded
motion, and the guards that stand between a model's proposal and the arm.

Every physical action goes through here. The policy only ever proposes; nothing
it returns reaches a servo until the state it reasoned about has been re-read
and still holds, because a decision costs seconds and the scene moves.
"""

import base64
import json
import math
import queue
import threading
import time
import uuid

from brain_client.common.geometry import quat_to_rpy
from innate import HeadState, MainImage, Manipulation, Mobility, Skill, WristImage
from innate.demo_actions import (
    BASE_LATERAL_FLOOR,
    BASE_LATERAL_PER_M,
    BASE_YAW_FLOOR,
    BASE_YAW_PER_M,
    joint_target,
)
from innate.gesture import forward_poses
from innate.icl_trace import ICL_TRACE_TOPIC, IclTrace


class ServoHardwareFault(ValueError):
    """A driver-reported servo hardware fault, distinct from missing telemetry."""


class LiveGestureObservation:
    """Timestamped snapshots from dedicated subscriptions, with deterministic teardown."""

    def __init__(self):
        import rclpy
        from mars_msgs.msg import ArmStatus
        from nav_msgs.msg import Odometry
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CompressedImage, JointState

        self.node = rclpy.create_node("gesture_observation_" + uuid.uuid4().hex[:8])
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


class _DemonstrationSkill(Skill):
    """Shared execution for demonstration-conditioned skills. Underscore-prefixed
    so the registry treats it as a helper base rather than a runnable skill."""

    head_position: HeadState
    main_image: MainImage
    wrist_image: WristImage
    manipulation: Manipulation
    mobility: Mobility

    decision_timeout = 175
    grip_strength = 0.4

    def _read(self, monitor, after, xml):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.check_cancelled()
            observation = monitor.snapshot(after, xml)
            if observation is not None:
                return observation
            self.sleep(0.05)
        self.fail("Fresh synchronized cameras, arm state and odometry unavailable")

    def _observe(self, monitor, after, xml):
        observation = self._read(monitor, after, xml)
        base = observation["base"]
        if self._base_origin is None:
            self._base_origin = base[:]
        origin = self._base_origin
        angle = abs(math.atan2(math.sin(base[2] - origin[2]), math.cos(base[2] - origin[2])))
        if not getattr(self, "_base_step_active", False) and (math.dist(base[:2], origin[:2]) > 0.02 or angle > 0.05):
            self.fail("Base moved during prop gesture")
        return observation

    def _decide(self, policy, observation, history):
        result = queue.Queue(maxsize=1)

        def request():
            try:
                result.put((policy.decide(observation, history), None))
            except Exception as exc:
                # Do not serialize HTTP bodies/credentials into robot feedback.
                result.put((None, type(exc).__name__))

        threading.Thread(target=request, daemon=True).start()
        deadline = time.monotonic() + self.decision_timeout
        while time.monotonic() < deadline:
            self.check_cancelled()
            try:
                value, error = result.get_nowait()
                if error:
                    self.fail("Gesture model request failed (" + error + ")")
                return value
            except queue.Empty:
                self.sleep(0.05)
        self.fail("Gesture model request timed out")

    def make_trace(self, run):
        """This run's live mirror for the In Context Learning page. Silent when
        the skill runs without a ROS node, so tests need no publisher."""
        from std_msgs.msg import String

        publisher = None if self.node is None else self.node.create_publisher(String, ICL_TRACE_TOPIC, 10)

        def publish(payload):
            if publisher is not None:
                publisher.publish(String(data=payload))

        return IclTrace(self.name, run.name, publish, self.logger)

    @staticmethod
    def _motion_outcome(status, reason, measured, target):
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

    def _try_move(self, target, current, monitor, xml):
        x, y, z, roll, pitch, yaw = target
        joints = self.manipulation.ik(x, y, z, roll=roll, pitch=pitch, yaw=yaw)
        if joints is None:
            return self._motion_outcome(
                "unreachable", "IK rejected the requested EE pose; no movement issued", current, target
            )
        # The driver clamps a backward shoulder silently, so the move would run and
        # land short instead of failing. Judge the solution and refuse it here.
        floor = self.manipulation.joint2_floor(joints[0])
        if joints[1] < floor:
            return self._motion_outcome(
                "unreachable",
                f"This pose needs the shoulder folded back to joint2={joints[1]:.2f}, past the {floor:.2f} the body "
                "allows here; no movement issued. Raise the target, bring it forward, or move the base.",
                current,
                target,
            )
        self.check_cancelled()
        self.manipulation.move_to(x, y, z, roll=roll, pitch=pitch, yaw=yaw, duration=1.5, block=False)
        # Health failures, cancellation, and uncertain/time-out outcomes still propagate.
        self._wait_motion(monitor, xml)
        measured = self._observe(monitor, time.monotonic() - 0.2, xml)
        if math.dist(measured["pose"][:3], target[:3]) > 0.015:
            return self._motion_outcome(
                "not_reached",
                "Move completed but missed the EE target by more than 1.5 cm; driver joint limits or tracking may prevent this pose",
                measured,
                target,
            )
        return self._motion_outcome("reached", "Measured EE position is within 1.5 cm of target", measured, target)

    def _try_joint_step(self, decision, current, monitor, xml):
        target = joint_target(decision, current)
        joint = decision.get("joint_step", decision["pose"])
        index = int(joint[0]) - 1
        # A shoulder already sagging a hair past the floor must not veto a step on
        # another joint, so only a joint2 step is held to the floor exactly.
        floor = self.manipulation.joint2_floor(target[0])
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
                self.check_cancelled()
                self.manipulation.stream_joints(target[:5], max_speed=0.25)
                self.sleep(0.04)
                measured = self._observe(monitor, time.monotonic() - 0.2, xml)
                error = abs(measured["qpos"][index] - target[index])
                if error <= min(0.02, abs(joint[1]) / 3):
                    status = "reached"
                    break
                if time.monotonic() - started > 4.0:
                    status = "not_reached"
                    break
        finally:
            self.manipulation.stream_stop()
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

    def _try_base_step(self, distance, current, monitor, xml):
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
                self.check_cancelled()
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
                self.mobility.send_cmd_vel(linear_x=math.copysign(0.04, distance), duration=0.15)
                self.sleep(0.05)
                measured = self._observe(monitor, time.monotonic() - 0.2, xml)
        finally:
            self.mobility.stop()
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

    def _wait_motion(self, monitor, xml):
        deadline = time.monotonic() + 8
        while self.manipulation.moving:
            self.check_cancelled()
            if time.monotonic() > deadline:
                self.fail("Arm motion timed out")
            self._observe(monitor, time.monotonic() - 1, xml)
            self.sleep(0.05)
        self.manipulation.wait(timeout=1)
