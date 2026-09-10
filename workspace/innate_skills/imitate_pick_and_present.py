# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Demonstration-conditioned, stationary pickup and presentation. No automatic release."""

import base64
import hashlib
import json
import math
import os
import queue
import threading
import time
import uuid
from pathlib import Path

from brain_client.common.geometry import quat_to_rpy
from innate import HeadState, MainImage, Manipulation, Mobility, Skill, SkillOutput, WristImage
from innate.gesture import Gesture, GesturePolicy, forward_poses, validate_action
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


class ImitatePickAndPresent(Skill):
    """Use a recorded MARS gesture's images and EE trajectory as Astra context to
    pick up an object and hold it forward. Experimental, supervised, arm-only:
    object already within reach, base stationary, clear workspace. Requires a
    finalized demonstration .h5 with head/wrist frames. Never releases the
    object automatically. No fine-tuning or task-specific replay policy.
    """

    head_position: HeadState
    main_image: MainImage
    wrist_image: WristImage
    manipulation: Manipulation
    mobility: Mobility

    def make_demo(self, demonstration, legacy_urdf):
        return Gesture(demonstration, legacy_urdf=legacy_urdf or None)

    def make_policy(self, demo):
        return GesturePolicy(demo)

    def make_trace(self, run):
        """This run's live mirror for the In Context Learning page. Silent when
        the skill runs without a ROS node, so tests need no publisher."""
        from std_msgs.msg import String

        publisher = None if self.node is None else self.node.create_publisher(String, ICL_TRACE_TOPIC, 10)

        def publish(payload):
            if publisher is not None:
                publisher.publish(String(data=payload))

        return IclTrace(self.name, run.name, publish, self.logger)

    decision_timeout = 50
    grip_strength = 0.3
    max_servo_recoveries = 0

    def _observe(self, monitor, after, xml):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.check_cancelled()
            observation = monitor.snapshot(after, xml)
            if observation is not None:
                return observation
            self.sleep(0.05)
        self.fail("Fresh synchronized cameras, arm state and odometry unavailable")

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

    def execute(
        self, demonstration: str, object_description: str = "the demonstrated object", legacy_urdf: str = ""
    ) -> SkillOutput:
        from ament_index_python.packages import get_package_share_directory

        # An explicit episode avoids silently selecting a different demonstration.
        demo = self.make_demo(demonstration, legacy_urdf)
        model = Path(get_package_share_directory("mars_sim")) / "urdf/mars.urdf"
        xml = model.read_text()
        if hashlib.sha256(xml.encode()).hexdigest() != demo.model_hash:
            self.fail("Demonstration uses a different robot model; recalibrate or record a new gesture")
        policy = self.make_policy(demo)
        root = Path(os.environ.get("INNATE_OS_ROOT", Path(__file__).resolve().parents[2]))
        run = root / "workspace/custom_skills/.gesture_runs" / uuid.uuid4().hex
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(
            json.dumps(
                {
                    "demonstration": str(demo.path),
                    "model_sha256": demo.model_hash,
                    "model": "gpt-6-astra",
                    "object": object_description,
                }
            )
        )
        icl = self.make_trace(run)
        icl.begin(
            "gpt-6-astra",
            str(demo.path),
            len(demo.poses),
            object=object_description,
            overview=[frame["index"] for frame in demo.frames],
        )
        policy.trace = icl
        succeeded = False
        ending = "Run ended without a result"
        monitor = None
        committed = False
        seen_holding = False
        closed_at = None
        verified = 0
        history = []
        motion_failures = 0
        recoveries = 0
        rejected_targets = set()
        previous_speed = self.manipulation.safety.max_ee_speed
        try:
            self.manipulation.safety.max_ee_speed = 0.03
            self.mobility.stop()
            monitor = LiveGestureObservation()
            started = time.monotonic()
            after = started
            for step in range(60):
                if time.monotonic() - started > 600:
                    self.fail("Gesture execution exceeded ten minutes")
                try:
                    observation = self._observe(monitor, after, xml)
                    observation["object"] = object_description
                    observation["head_degrees"] = self.head_position.pitch_degrees
                    observation["grasp_committed"] = committed
                    for name, image in observation["images"].items():
                        (run / f"{step:03d}_{name}.jpg").write_bytes(base64.b64decode(image))
                    asked = time.monotonic()
                    value = self._decide(policy, observation, history)
                    latency = time.monotonic() - asked
                    self.check_cancelled()
                    # Revalidate telemetry after network latency; no queued motion survives Stop.
                    current = self._observe(monitor, time.monotonic() - 0.2, xml)
                    if math.dist(current["pose"][:3], observation["pose"][:3]) > 0.01:
                        history.append(
                            {
                                "step": step,
                                "execution": {
                                    "status": "stale_observation",
                                    "reason": "Arm moved while planning; discarded action, replan from fresh state",
                                    "measured_pose": current["pose"],
                                    "measured_qpos": current.get("qpos"),
                                },
                            }
                        )
                        self.feedback("Arm shifted; refreshing the observation before replanning")
                        icl.note("Arm shifted while planning; the action was discarded")
                        after = time.monotonic()
                        continue
                    decision = validate_action(value, current["pose"], len(demo.poses), committed)
                    entry = {
                        "step": step,
                        "observation": {k: v for k, v in observation.items() if k != "images"},
                        "decision": decision,
                    }
                    with (run / "trace.jsonl").open("a") as trace:
                        trace.write(json.dumps(entry, allow_nan=False) + "\n")
                    if hasattr(policy, "phase_map"):
                        (run / "phase_map.json").write_text(json.dumps(policy.phase_map))
                        with (run / "inspection.jsonl").open("a") as inspection:
                            inspection.write(json.dumps(policy.last_trace) + "\n")
                    self.feedback(decision["reason"])
                    icl.step(
                        step,
                        decision,
                        entry["observation"],
                        observation["images"],
                        latency,
                        getattr(policy, "phase", 0),
                    )
                    if committed and seen_holding and not decision["holding"]:
                        self.fail("Object no longer visually retained")
                    seen_holding = seen_holding or (committed and decision["holding"])
                    action = decision["action"]
                    if action == "move":
                        target_key = tuple(round(v, 4) for v in decision["pose"])
                        if target_key in rejected_targets:
                            outcome = self._motion_outcome(
                                "rejected",
                                "This exact target already failed; choose another approach",
                                current,
                                decision["pose"],
                            )
                        else:
                            outcome = self._try_move(decision["pose"], current, monitor, xml)
                        entry["execution"] = outcome
                        icl.execution(step, outcome)
                        with (run / "execution.jsonl").open("a") as execution:
                            execution.write(json.dumps({"step": step, **outcome}) + "\n")
                        if outcome["status"] != "reached":
                            motion_failures += 1
                            rejected_targets.add(target_key)
                            self.feedback(outcome["reason"] + "; returning measured state to the agent")
                            if motion_failures >= 3:
                                self.fail(
                                    "Three motion proposals failed without a successful move; stopping replanning"
                                )
                        else:
                            motion_failures = 0
                            rejected_targets.clear()
                    elif action == "open":
                        self.manipulation.gripper_open(duration=0.8, block=False)
                        self._wait_motion(monitor, xml)
                    elif action == "close":
                        if not committed:
                            # Latch before submission: any partial failure must preserve grip.
                            committed = True
                            closed_at = current["pose"][:3]
                            self.manipulation.gripper_close(strength=self.grip_strength, duration=0.8, block=False)
                            self._wait_motion(monitor, xml)
                    elif action == "done":
                        p = current["pose"][:3]
                        if (
                            not committed
                            or not decision["holding"]
                            or not decision["presented"]
                            or p[2] < closed_at[2] + 0.04
                            or p[0] < 0.20
                            or math.dist(p, demo.final_pose[:3]) > 0.10
                        ):
                            self.fail("Pickup/presentation not verified")
                        verified += 1
                        if verified >= 2:
                            succeeded = True
                            ending = "Object visually verified held forward"
                            return SkillOutput(
                                "Object visually verified held forward; gripper remains closed.",
                                {"demonstration": str(demo.path), "trace": str(run), "released": False},
                            )
                    if action != "done":
                        verified = 0
                    history.append(entry)
                    after = time.monotonic()
                    self.sleep(0.5 if action == "done" else 0.1)
                except ServoHardwareFault as fault:
                    if recoveries >= self.max_servo_recoveries:
                        self.fail("Servo hardware fault; automatic recovery budget exhausted")
                    recoveries += 1
                    recovered = self._recover_servo(monitor, xml, str(fault))
                    history.append({"step": step, "execution": recovered})
                    icl.execution(step, recovered)
                    with (run / "execution.jsonl").open("a") as execution:
                        execution.write(json.dumps({"step": step, **recovered}) + "\n")
                    verified = 0
                    # Keep the grasp latch and failed-target history. Never replay
                    # the interrupted action: the next turn uses new camera frames.
                    after = time.monotonic()
            self.fail("Gesture action budget exhausted")
        finally:
            icl.end(succeeded, self.cancelled, ending)
            # Never rest, release, reseed the gripper from measured joints, or torque off.
            # Current goto services cannot preempt: at most one bounded move may finish.
            self.manipulation.halt()
            self.mobility.stop()
            self.manipulation.safety.max_ee_speed = previous_speed
            if monitor is not None:
                monitor.close()

    def _recover_servo(self, monitor, xml, fault):
        self.feedback("Servo hardware fault; reloading only failed servos")
        self.manipulation.halt()
        # An already committed goto cannot be preempted. Let it finish before reboot.
        deadline = time.monotonic() + 8
        while self.manipulation.moving:
            if time.monotonic() >= deadline:
                self.fail("Pending arm motion did not finish before recovery")
            self.sleep(0.05)
        result = monitor.reload_failed_servos(self)
        completed = time.monotonic()
        # Require a post-reboot status, not the healthy status from before the fault.
        deadline = completed + 5
        while time.monotonic() < deadline:
            self.check_cancelled()
            with monitor.lock:
                health = monitor.values.get("health")
            if health and health[1] > completed and health[0].is_ok and health[0].is_torque_enabled:
                break
            self.sleep(0.05)
        else:
            self.fail("Servo remains unhealthy after targeted recovery")
        self.manipulation.wait(timeout=1)
        measured = self._observe(monitor, completed, xml)
        self.feedback("Servo recovered; replanning from fresh images and measured pose")
        return {
            "status": "servo_recovered",
            "fault": fault,
            "servo_ids": result["error_ids"],
            "reason": "Interrupted action discarded after servo recovery; replan from actual state",
            "measured_pose": measured["pose"],
            "measured_qpos": measured.get("qpos"),
        }

    @staticmethod
    def _motion_outcome(status, reason, measured, target):
        return {
            "status": status,
            "reason": reason,
            "requested_pose": target,
            "measured_pose": measured["pose"],
            "measured_qpos": measured.get("qpos"),
            "position_error_m": math.dist(measured["pose"][:3], target[:3]),
        }

    def _try_move(self, target, current, monitor, xml, grip=None):
        x, y, z, roll, pitch, yaw = target
        if not self.manipulation.reachable(x, y, z, roll=roll, pitch=pitch, yaw=yaw):
            return self._motion_outcome(
                "unreachable", "IK rejected the requested EE pose; no movement issued", current, target
            )
        self.check_cancelled()
        # move_to carries j6 too. Left to itself it uses the standing grip target,
        # which is stale when the run began already holding something it never
        # closed on — and a Cartesian move then opens the hand mid-carry.
        self.manipulation.move_to(x, y, z, roll=roll, pitch=pitch, yaw=yaw, duration=1.5, block=False, grip=grip)
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

    def _wait_motion(self, monitor, xml):
        deadline = time.monotonic() + 8
        while self.manipulation.moving:
            self.check_cancelled()
            if time.monotonic() > deadline:
                self.fail("Arm motion timed out")
            self._observe(monitor, time.monotonic() - 1, xml)
            self.sleep(0.05)
        self.manipulation.wait(timeout=1)
