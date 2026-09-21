# SPDX-License-Identifier: Apache-2.0
"""Visually guided battery pickup and handoff on MARS."""

import json
import base64
import math
import time

from innate import MainImage, Manipulation, Mobility, SkillReturn, WristImage
from innate.exceptions import ArmFailed, ArmUnhealthy

from .battery_handoff_policy import BatteryHandoffPolicy
from .cabinet_agent_policy import ModelActionRejected
from .ee_pose_ik import EEPoseIK
from .duel_with_lightsaber import DuelWithLightsaber


class HandBatteryToRobot(DuelWithLightsaber):
    """Use GPT-6 Astra to pick up a battery and hand it to the robot ahead.

    Starts at zero with joint 4 at +90 degrees (gripper pointing down),
    preserving the gripper.
    Start with torque on and the path to the starting pose clear; the gripper may be empty
    or already holding the battery,
    and a loose disconnected battery on a stable surface within reach.
    The stationary recipient must be ready to grasp it. Supervised arm-only
    experiment using head/wrist vision. Holds the battery if transfer cannot
    be verified; only releases after seeing the recipient support it.
    """

    manipulation: Manipulation
    mobility: Mobility
    main_image: MainImage
    wrist_image: WristImage
    starting_envelope = None
    joint_limits = None
    arm_load_limit_percent = None
    close_strength = 0.4
    startup_joints = (0.0, 0.0, 0.0, math.pi / 2, 0.0)

    def _make_policy(self):
        return BatteryHandoffPolicy()

    def _load_pose_ik(self):
        from rcl_interfaces.srv import GetParameters

        client = self.manipulation.node.create_client(GetParameters, "/mars_arm/get_parameters")
        deadline = time.monotonic() + 5
        try:
            while not client.service_is_ready():
                if time.monotonic() > deadline:
                    self.fail("Arm joint-limit service unavailable")
                self._wait(0.04)
            request = GetParameters.Request()
            request.names = [f"joint_{i}.position_limits" for i in range(1, 6)]
            future = client.call_async(request)
            while not future.done():
                if time.monotonic() > deadline:
                    self.fail("Arm joint-limit lookup timed out")
                self._wait(0.04)
            parameters = future.result().values
            if len(parameters) != 5 or any(p.type != 8 for p in parameters):
                self.fail("Missing driver joint limits")
            self._pose_ik = EEPoseIK([tuple(p.double_array_value) for p in parameters])
            if tuple(self._pose_ik.names) != tuple(self._state().name[:5]):
                self.fail("Kinematics and telemetry joint order differ")
        finally:
            self.manipulation.node.destroy_client(client)

    def _observer_frame(self):
        """Read one new frame from the stationary recipient's rosbridge camera."""
        import websocket

        self.check_cancelled()
        connection = None
        try:
            connection = websocket.create_connection(self._observer_camera_url, timeout=0.3)
            connection.send(json.dumps({"op": "subscribe", "topic": "/mars/main_camera/left/image_raw/compressed",
                                        "type": "sensor_msgs/msg/CompressedImage", "queue_length": 1}))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                self.check_cancelled()
                try:
                    message = json.loads(connection.recv())
                except websocket.WebSocketTimeoutException:
                    self._wait(0.01)
                    continue
                if message.get("topic") == "/mars/main_camera/left/image_raw/compressed":
                    stamp = message["msg"]["header"]["stamp"]
                    timestamp = stamp["sec"] + stamp["nanosec"] / 1e9
                    if abs(time.time() - timestamp) > 2:
                        continue
                    return MainImage.from_jpeg(base64.b64decode(message["msg"]["data"]))
            raise ValueError("No fresh observer frame")
        except (OSError, ValueError, KeyError, websocket.WebSocketException) as error:
            self._event("battery_observer_unavailable", reason=str(error))
            return None
        finally:
            if connection is not None:
                connection.close()

    def _observe(self, step):
        observation, frames = super()._observe(step)
        if getattr(self, "_observer_camera_url", ""):
            frame = self._observer_frame()
            observation["recipient_camera_available"] = frame is not None
            if frame is not None:
                frames["recipient_head_looking_back_at_Blue"] = frame
        observation.pop("saber_length_m")
        observation.pop("recovery_grip_check")
        observation.update(
            ee_pose=list(self._pose_ik.pose(observation["joint_positions_rad"][:5])),
            ee_frame="base_link", ee_link="ee_link", angle_convention="fixed-axis RPY radians",
            hardware_joint_limits_rad=self._pose_ik.limits,
            motion_limits={"translation_m": 0.03, "rotation_rad": 0.15,
                           "joint_travel_rad": 0.35, "joint_speed_rad_s": 0.25,
                           "arm_load_percent": self.arm_load_limit_percent},
            startup_zero=getattr(self, "_startup_zero", None),
            hold_for_recipient=getattr(self, "_hold_for_recipient", False),
            phase=self._phase, current_objective=(
            "Locate, grasp and lift the battery; recipient readiness is not required yet"
            if self._phase in ("approach", "lifting") else ("Hold the battery forward toward the receiving robot without releasing" if getattr(self, "_hold_for_recipient", False) else "Transfer the held battery to the receiving robot")
        ))
        return observation, frames

    def _plan_pose(self, target_pose, observed):
        self.check_cancelled()
        current = self._state().position[:5]
        drift_m, drift_rad = self._pose_ik.errors(self._pose_ik.pose(current), self._pose_ik.pose(observed[:5]))
        if drift_m > 0.01 or drift_rad > 0.10:
            raise ValueError(f"Wrist moved during model decision: {drift_m:.4f} m, {drift_rad:.4f} rad; reassess")
        position_error, angle_error = self._pose_ik.errors(self._pose_ik.pose(current), target_pose)
        if position_error > 0.030001 or angle_error > 0.150001:
            raise ValueError("EE target must be within 3 cm and 0.15 rad of the measured pose")
        target = self._pose_ik.solve(target_pose, current)
        travel = max(abs(a - b) for a, b in zip(target, current, strict=True))
        if travel > 0.35:
            raise ValueError("IK needs more than 0.35 rad joint travel; request a closer EE pose")
        self.check_cancelled()
        # IK may take time; never execute against a state changed during planning.
        if max(abs(a - b) for a, b in zip(self._state().position[:5], current, strict=True)) > 0.015:
            raise ValueError("Arm moved during IK; reassess fresh images")
        return target

    def _move_pose(self, target_pose, observed):
        target = self._plan_pose(target_pose, observed)
        speed = 0.25
        initial = tuple(self._state().position[:5])
        # stream_joints queues a slew; accepting a small residual after one tick
        # can cancel that slew before the requested target has been dispatched.
        slew_duration = max(abs(q - t) for q, t in zip(initial, target, strict=True)) / speed + 0.1
        started = time.monotonic()
        self._event("battery_ee_move_start", requested_pose=target_pose, target_joints=target)
        commanded = False
        try:
            while True:
                self.check_cancelled()
                state = self._state()
                measured = self._pose_ik.pose(state.position[:5])
                distance, angle = self._pose_ik.errors(measured, target_pose)
                # Completion is the requested EE pose, not exact IK motor angles.
                elapsed = time.monotonic() - started
                joints_reached = max(abs(q - t) for q, t in zip(state.position[:5], target, strict=True)) <= 0.001
                if commanded and (elapsed >= slew_duration or joints_reached) and distance <= 0.005 and angle <= math.radians(3):
                    return {"complete": True, "requested_ee_pose": target_pose,
                            "measured_ee_pose": measured, "position_error_m": distance,
                            "orientation_error_rad": angle, "measured_joints_rad": list(state.position[:5])}
                if time.monotonic() - started > 3.0:
                    return {"complete": False, "requested_ee_pose": target_pose,
                            "measured_ee_pose": measured, "position_error_m": distance,
                            "orientation_error_rad": angle, "measured_joints_rad": list(state.position[:5])}
                # Five arm joints preserve the standing gripper command.
                self.manipulation.stream_joints(target, max_speed=speed)
                commanded = True
                self._wait(0.04)
        finally:
            self.manipulation.stream_stop()

    def _gripper(self, opening, observed):
        self.check_cancelled()
        state = self._state()
        position_drift, angle_drift = self._pose_ik.errors(
            self._pose_ik.pose(state.position[:5]), self._pose_ik.pose(observed[:5]))
        if position_drift > 0.01 or angle_drift > 0.10 or abs(state.position[5] - observed[5]) > 0.10:
            raise ValueError(f"Gripper pose changed during decision: {position_drift:.4f} m, {angle_drift:.4f} rad; reassess")
        # Streaming is cancellable and does not trigger an implicit servo reboot.
        # Use the standard CloseGripper default preload and its hardware cap.
        strength = min(self.close_strength, self.manipulation.GRIPPER_MAX_STRENGTH)
        target = self.manipulation.GRIPPER_OPEN if opening else self.manipulation.GRIPPER_CLOSED - strength
        arm = tuple(state.position[:5])
        initial = state.position[5]
        speed = 0.25
        duration = abs(target - initial) / speed + 0.75
        started = time.monotonic()
        commanded = False
        try:
            while time.monotonic() - started < duration:
                self.check_cancelled()
                state = self._state()
                if max(abs(a - b) for a, b in zip(state.position[:5], arm, strict=True)) > 0.03:
                    self.fail("Arm drifted during gripper action")
                if commanded and abs(state.position[5] - target) <= 0.04:
                    break
                self.manipulation.stream_joints([*arm, target], max_speed=speed)
                commanded = True
                self._wait(0.04)
            # A battery may prevent closing fully; assess grasp/release visually.
            measured = self._state().position[5]
            return {"command": "open_gripper" if opening else "close_gripper",
                    "target_reached": abs(measured - target) <= 0.04,
                    "requested_gripper_rad": target, "initial_gripper_rad": initial,
                    "measured_gripper_rad": measured, "error_rad": target - measured}
        finally:
            self.manipulation.stream_stop()

    def _move_to_zero(self):
        """Move to zero with wrist pitched down, preserving gripper closure."""
        target = list(self.startup_joints)
        if any(not lo <= q <= hi for q, (lo, hi) in zip(target, self._pose_ik.limits, strict=True)):
            self.fail("Starting pose is outside the configured arm joint limits")
        initial = list(self._state().position[:5])
        deadline = time.monotonic() + max(abs(q - t) for q, t in zip(initial, target, strict=True)) / 0.25 + 1.0
        self.feedback("Moving arm to zero with wrist pointing down before Astra observes")
        try:
            while True:
                self.check_cancelled()
                measured = list(self._state().position[:5])
                reached = max(abs(q - t) for q, t in zip(measured, target, strict=True)) <= 0.04
                if reached or time.monotonic() >= deadline:
                    result = {"target_reached": reached, "requested_joints_rad": target,
                              "measured_joints_rad": measured}
                    self._event("battery_startup_zero", **result)
                    return result
                # Five-joint commands preserve the existing gripper target.
                self.manipulation.stream_joints(target, max_speed=0.25)
                self._wait(0.04)
        finally:
            self.manipulation.stream_stop()

    def execute(self, max_steps: int = 60, hold_for_recipient: bool = False, start_at_zero: bool = True, observer_camera_url: str = "") -> SkillReturn:
        """Pick up and transfer a battery in 1–100 decisions, at most five minutes."""
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 100:
            self.fail("max_steps must be an integer between 1 and 100")
        if not isinstance(start_at_zero, bool):
            self.fail("start_at_zero must be a boolean")
        if not isinstance(hold_for_recipient, bool):
            self.fail("hold_for_recipient must be a boolean")
        self._observer_camera_url = observer_camera_url
        policy = self._make_policy()
        original_result = policy.result
        def record_result(call_id, result):
            self._event("battery_action_result", call_id=call_id, result=result)
            original_result(call_id, result)
        policy.result = record_result
        if hold_for_recipient:
            policy.instructions += "\nFor this run, finish by holding the picked-up battery forward toward the receiving robot, clear of the table. Do not release it. Once visibly presented forward, use done with scene=held and describe evidence of retention and presentation; two fresh confirmations finish.\n"
        if not start_at_zero:
            policy.instructions += "\nResume from the measured pose without homing. Previous grasps temporarily retained the battery but slipped; it may now be empty. Determine the actual state from fresh images before moving. Diagonal corner contact and retention at only one pad failed: At clearance with empty open jaws, restore a downward pickup approach (pitch near 1.57 rad), then roll to align opposite casing sidewalls before descending. The previous pitch near 1.05 was a carrying tilt; repeated grasps rotated out. Use incremental feasible poses and fresh images to adapt, keeping the pads level for table pickup. For forward presentation, aim around wrist x=0.30 m, z=0.15 m, y near -0.054 m, using incremental movements. IK verified that pitch around 1.1 rad allows that reach; adapt from the current pose while retaining grip. Do not finish at the previous low pose near x=0.225,z=0.079.\n"
        if observer_camera_url:
            policy.instructions += "\nA third labeled view, recipient_head_looking_back_at_Blue, is the receiving robot looking back at your blue robot and gripper. Use it to judge casing height, enclosure, and whether the actual battery follows your gripper. Its camera axes differ from base_link. The white plates and black labeled motor panel in your own head view are ARM HOUSING, not the battery. The target is the separate black Sony camera battery on the table. Previous apparent successes confused arm housing with payload; verify the actual battery retained clear of the table in the recipient view and wrist view. If it is on the table, reopen empty fingers and retry.\n"
        self._hold_for_recipient = hold_for_recipient
        self._deadline = time.monotonic() + 300
        if hasattr(self, "_anchor"):
            del self._anchor
        self._phase = "approach"
        self._camera_seen = {attr: (getattr(self, attr), time.monotonic())
                             for attr in ("main_image", "wrist_image")}
        supported = confirmed = 0
        initial_observation = True
        try:
            self._start_telemetry()
            self._anchor = tuple(self._state().position[:5])
            self._load_pose_ik()
            self.mobility.stop()
            self._startup_zero = self._move_to_zero() if start_at_zero else {"skipped": True, "measured_joints_rad": list(self._state().position[:5])}
            self._anchor = tuple(self._state().position[:5])
            for step in range(max_steps):
                observation, frames = self._observe(step)
                try:
                    call, (action, values, scene, note) = policy.decide(observation, frames, self._wait)
                except ModelActionRejected as error:
                    self.check_cancelled()
                    self._state()
                    self._event("battery_action_rejected", step=step, reason=str(error))
                    if policy.last_usage is not None:
                        self._event("gpt_usage", step=step, **policy.last_usage)
                    self.feedback(f"Astra is correcting its action: {error}")
                    # The policy already appended the rejection to tool history.
                    # Spend one decision, then obtain fresh telemetry and images.
                    continue
                self.check_cancelled()
                self._state()
                self._event("battery_handoff_action", phase=self._phase, action=action, scene=scene, note=note)
                if policy.last_usage is not None:
                    self._event("gpt_usage", step=step, **policy.last_usage)
                self.feedback(f"Astra battery {step + 1}: {action} — {note}")
                if scene == "unsafe":
                    self.fail(f"Battery handoff stopped ({scene}): {note}")
                if scene == "lost":
                    self._phase = "approach"
                    supported = confirmed = 0
                    initial_observation = True
                    policy.result(call, "Grasp attempt failed or the battery is no longer held. No motion or opening dispatched. "
                                  "Keep what you learned from this attempt; reassess fresh images, locate the settled battery, "
                                  "then reopen empty fingers, reposition and retry with a revised approach. Do not reset the arm.")
                    continue
                if action == "give_up":
                    policy.result(call, "Reassess and choose a different approach using your action/result history. "
                                  "An ordinary task failure is retryable. Report unsafe only for an actual hazard; "
                                  "otherwise continue from the current measured state.")
                    continue
                # Scene evidence can correct an unsuccessful grasp without requiring
                # the model to emit a separate lost report first.
                if self._phase in ("lifting", "carrying") and scene in ("empty", "aligned"):
                    self._phase = "approach"
                    supported = confirmed = 0
                if initial_observation:
                    if scene in ("searching", "empty", "aligned"):
                        self._phase = "approach"
                    elif scene == "grasped":
                        self._phase = "lifting"
                    elif scene == "held":
                        self._phase = "carrying"
                    else:
                        policy.result(call, "Startup view is uncertain. No motion dispatched. Recipient readiness is not needed for pickup. If a clear swept path is visible, use searching with a small move_ee_pose to improve the view; otherwise explain the obstruction.")
                        continue
                    initial_observation = scene == "searching"
                if self._phase == "lifting" and scene == "held":
                    self._phase = "carrying"
                if hold_for_recipient and action == "done" and scene == "held":
                    confirmed += 1
                    if confirmed >= 2:
                        return f"Astra visually verified battery held forward for the receiving robot: {note}"
                    policy.result(call, "Keep holding still. Confirm with a fresh image that the battery remains retained clear of the table and presented forward toward the receiving robot.")
                    continue
                if hold_for_recipient:
                    confirmed = 0
                supported = supported + 1 if scene == "recipient_supported" and self._phase in ("carrying", "release_check") else 0
                if self._phase == "release_check":
                    if action == "open_gripper" and scene == "recipient_supported" and supported >= 2:
                        confirmed = 0
                        try:
                            result = self._gripper(True, observation["joint_positions_rad"])
                            policy.result(call, "Gripper feedback: " + json.dumps(result) + ". Verify visually; incomplete opening can be retried while recipient support is confirmed.")
                        except ValueError as error:
                            policy.result(call, f"No action dispatched: {error}; reassess support before retrying.")
                        continue
                    if action == "done" and scene == "transferred":
                        confirmed += 1
                        if confirmed >= 2:
                            return f"Astra visually verified battery held by the receiving robot in two observations: {note}"
                    else:
                        confirmed = 0
                    policy.result(call, "Release was attempted: keep the arm still and assess actual gripper/battery state. Confirm transfer visually, or retry opening after two fresh recipient-support observations.")
                    continue
                movement = gripper = None
                try:
                    if action == "done" or scene == "transferred":
                        raise ValueError("Transfer cannot complete before verified pickup and release")
                    if action == "close_gripper":
                        if self._phase not in ("approach", "lifting"):
                            raise ValueError("Do not regrasp during carry or release")
                        gripper = self._gripper(False, observation["joint_positions_rad"])
                        self._phase = "lifting"
                    elif action == "open_gripper":
                        if self._phase == "approach" and scene == "empty":
                            gripper = self._gripper(True, observation["joint_positions_rad"])
                        elif self._phase == "carrying" and supported >= 2 and not hold_for_recipient:
                            gripper = self._gripper(True, observation["joint_positions_rad"])
                            self._phase = "release_check"
                        else:
                            raise ValueError("Hold grip; two consecutive recipient-support observations after pickup are required")
                    elif action == "move_ee_pose":
                        # The validated scene permits grip-preserving view adjustments
                        # even when a prior grasp phase no longer matches the evidence.
                        movement = self._move_pose(values, observation["joint_positions_rad"])
                        supported = 0
                    if gripper is not None:
                        policy.result(call, "Gripper feedback: " + json.dumps(gripper) + ". Streaming stopped. Use fresh images and the measured aperture to assess grasp/release. A partial move is not task failure; retry the same command only if visually appropriate, without increasing force.")
                    elif movement is not None:
                        policy.result(call, "Motion feedback: " + json.dumps(movement) +
                                      ". Streaming stopped. Compare intended and measured motion, update your motion/geometry priors, and choose the next pose from fresh observations. Incomplete motion is not success; do not blindly repeat or increase force.")
                    else:
                        policy.result(call, "Action completed; measured ee_pose=" + str(self._pose_ik.pose(self._state().position[:5])) + ". Reassess fresh images. Gripper closure alone is not proof of pickup.")
                except ValueError as error:
                    policy.result(call, f"No action dispatched: {error}. Update your priors and revise the target using the next fresh observation.")
            self.fail("Battery handoff decision budget exhausted; transfer not verified")
        except (ArmFailed, ArmUnhealthy, ValueError, RuntimeError, OSError) as error:
            self.fail(str(error))
        finally:
            # Never open or reboot on failure/cancellation while holding a battery.
            self.manipulation.stream_stop()
            self.mobility.stop()
            self._stop_telemetry()
