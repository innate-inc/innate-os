#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import gc
import json
import math
import os
import threading
import time

import cv2
import h5py
import numpy as np
import rclpy
import torch
from brain_messages.action import ExecuteBehavior
from cv_bridge import CvBridge
from geometry_msgs.msg import Twist
from mars_msgs.srv import GotoJS
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray, Int32, String
from std_srvs.srv import Trigger

# Import your policy class and trajectory generator
from manipulation.ACT import ACTPolicy  # noqa: E402
from manipulation.act_config import (  # noqa: E402
    create_act_config,
    infer_chunk_size,
    load_torch_file,
    normalize_state_dict,
    validate_action_dim,
)

# Pure (ROS-free) auto-stop logic, kept in its own module.
from manipulation.auto_stop import LearnedStopDetector, StepSignals  # noqa: E402

# NOTE: manipulation.act_trt is imported lazily inside _load_policy_for_behavior, not here.
# It imports `tensorrt` at module load, which is only installed on hardware units. Importing
# it at module top would crash the whole behavior server (respawn loop) on a sim/dev box and
# take down the poses/replay behaviors too -- which don't need TensorRT. TensorRT stays a hard
# requirement for the learned path: the lazy import raises there, failing that load loudly.
from manipulation.config_validation import (  # noqa: E402
    BehaviorConfigError,
    LearnedExecCfg,
    PosesExecCfg,
    ReplayExecCfg,
    ValidatedBehavior,
    validate_behavior_config,
)


class ManipulationServer(Node):
    def __init__(self):
        super().__init__("manipulation_server")
        self.get_logger().set_level(rclpy.logging.LoggingSeverity.DEBUG)
        self.get_logger().info("Behavior server started.")

        # Use environment variable if set, otherwise construct from HOME
        mars_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))

        # Get data directory from recorder config
        default_data_dir = os.path.join(mars_root, "data")
        try:
            self.declare_parameter("data_directory", default_data_dir)
            self.data_directory = os.path.expanduser(self.get_parameter("data_directory").value)
            self.get_logger().info(f"Data directory: {self.data_directory}")
        except Exception as e:
            self.get_logger().warn(f"Could not load data_directory parameter: {e}")
            self.data_directory = default_data_dir

        # Learned-policy runtime knobs (from manipulation_server.yaml; overridable via config/settings.yaml).
        self.declare_parameter("inference_hz", 25.0)
        self.declare_parameter("speed", 1.5)
        # inference_hz drives the loop period (1.0 / inference_hz); guard against <= 0.
        inference_hz = self.get_parameter("inference_hz").value
        if inference_hz <= 0:
            self.get_logger().warn(f"inference_hz must be > 0 (got {inference_hz}); falling back to 25.0 Hz.")
            inference_hz = 25.0
        self.inference_hz = inference_hz
        self.policy_speed = self.get_parameter("speed").value
        # De-rate applied to the recorded/predicted base velocity at execution time. Replay and
        # learned inference are tuned independently; both play back base cmd_vel 1:1 by default.
        self.declare_parameter("replay_base_speed_scale", 1.0)
        self.declare_parameter("learned_base_speed_scale", 1.0)
        self.replay_base_speed_scale = self.get_parameter("replay_base_speed_scale").value
        self.learned_base_speed_scale = self.get_parameter("learned_base_speed_scale").value
        # n_action_steps=0 means "auto" (min(40, chunk_size)); a per-skill value still wins.
        self.declare_parameter("n_action_steps", 0)
        self.default_n_action_steps = self.get_parameter("n_action_steps").value
        # ACT temporal-ensemble smoothing coefficient -- the single control surface for
        # ensembling (see manipulation_server.yaml for the behavior of each sign). Resolve and
        # validate it once here, normalizing 0 -> None ("disabled"), so the same value drives
        # both create_act_config (which pins n_action_steps=1 when ensembling) and TRTACTPolicy.
        self.declare_parameter("temporal_ensemble_coeff", 0.0)
        coeff = self.get_parameter("temporal_ensemble_coeff").value
        if not math.isfinite(coeff):
            # nan/inf pass the float param but poison the ensemble weights (exp(-nan*i)=nan),
            # which would publish NaN joint commands. Reject like a malformed value.
            self.get_logger().warn(f"Invalid temporal_ensemble_coeff ({coeff}); disabling ensembling")
            coeff = 0.0
        self.temporal_ensemble_coeff = coeff if coeff != 0 else None

        # Image size for policy inference (matches checkpoint training)
        self.bridge = CvBridge()
        self.image_size = (224, 224)  # Resize to match checkpoint expectations
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # Cache of GPU resampling matrices keyed by source (h, w). INTER_AREA is a
        # separable linear filter, so resize == two matmuls with content-independent
        # weights; precomputing them lets the whole resize run on-GPU, bit-exact to
        # cv2.INTER_AREA (up to uint8 rounding), instead of on the contended CPU.
        self._resize_mats = {}

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            self.get_logger().info(f"PyTorch device: {self.device} ({props.name}, {props.total_memory / 1e9:.1f} GB)")
        else:
            self.get_logger().info(f"PyTorch device: {self.device} (CUDA unavailable)")

        # Current execution state
        self.execution_running = False
        self.current_goal_handle = None
        self.current_policy = None
        self.current_action_dim = 10  # Add this to track current policy's action dimension
        self._cancel_requested = threading.Event()

        # Sensor data
        self.latest_image1 = None
        self.latest_image2 = None
        self.latest_joint_state = None
        self.latest_image1_timestamp = None
        self.latest_image2_timestamp = None
        self.latest_joint_timestamp = None

        # Sensor subscriptions are created once on the first behavior and then kept for
        # the node's lifetime. They are deliberately NEVER destroyed: under the
        # MultiThreadedExecutor, destroying a subscription that the executor has already
        # selected as "ready" races _take_subscription and crashes the process
        # (InvalidHandle: "destruction was requested"). Idle CPU is instead bounded by
        # early-returning in the image callbacks when no behavior is running.
        self._image1_sub = None
        self._image2_sub = None
        self._joint_sub = None

        # Publishers
        # Skills input of the cmd_vel priority mux (teleop can override).
        self.cmd_vel_pub = self.create_publisher(Twist, "/cmd_vel_skills", 10)
        self.arm_state_pub = self.create_publisher(Float64MultiArray, "/mars/arm/commands", 10)
        # Head position command (degrees) — only published for head-enabled replay skills.
        self.head_set_position_pub = self.create_publisher(Int32, "/mars/head/set_position", 10)
        # Per-step inference timing breakdown (JSON String), for the webapp Profiling page.
        # Only published while a learned behavior is executing, so it's free when idle.
        self.inference_profile_pub = self.create_publisher(String, "/brain/manipulation/inference_profile", 10)
        self._inference_seq = 0
        # Previous emitted action, for the per-step command-jerk (smoothness) metric.
        self._prev_action_np = None
        # Service clients
        self.head_ai_position_client = self.create_client(Trigger, "/mars/head/set_ai_position")
        self.arm_goto_client = self.create_client(GotoJS, "/mars/arm/goto_js")

        # Action server
        self.action_server = ActionServer(
            self,
            ExecuteBehavior,
            "/behavior/execute",
            goal_callback=self.goal_callback,
            execute_callback=self.execute_behavior_callback,
            cancel_callback=self.cancel_behavior_callback,
        )

        self.get_logger().info("Behavior server ready - pure execution engine using absolute skill directories")

    def goal_callback(self, goal_request):
        """Accept-time validation of behavior_config.

        Rejects malformed goals with ``GoalResponse.REJECT`` so the action
        client gets immediate feedback instead of the server accepting and
        then aborting. ``execute_behavior_callback`` revalidates as a
        belt-and-braces check against the file being deleted between accept
        and execute.
        """
        skill_dir = getattr(goal_request, "skill_dir", "")
        payload = getattr(goal_request, "behavior_config", "")
        if not payload:
            self.get_logger().error(f"Rejecting behavior goal for {skill_dir}: behavior_config is empty")
            return GoalResponse.REJECT
        try:
            validate_behavior_config(payload, skill_dir, check_files_exist=True)
        except BehaviorConfigError as exc:
            self.get_logger().error(f"Rejecting behavior goal for {skill_dir}: {exc}")
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def cancel_behavior_callback(self, goal_handle_to_cancel):
        """Handle action cancel requests."""
        self.get_logger().info("Received cancel request...")

        if not self.execution_running or not self.current_goal_handle:
            self.get_logger().warn("Rejecting cancel request. No behavior running.")
            return CancelResponse.REJECT

        self.get_logger().info("Accepting cancel request.")
        self._cancel_requested.set()
        return CancelResponse.ACCEPT

    def execute_behavior_callback(self, goal_handle):
        """Execute the requested behavior."""
        skill_dir = goal_handle.request.skill_dir

        if self.execution_running:
            result = ExecuteBehavior.Result()
            result.success = False
            result.message = "Another behavior is already running"
            self.get_logger().warn(f"Behavior {skill_dir} requested but already running")
            goal_handle.abort()
            return result

        # Re-validate at execute-time. goal_callback already ran the same
        # check at accept-time, but files can disappear between accept and
        # execute, and defense in depth is cheap (pydantic parse is us-scale).
        try:
            validated = validate_behavior_config(
                goal_handle.request.behavior_config,
                skill_dir,
                check_files_exist=True,
            )
        except BehaviorConfigError as exc:
            result = ExecuteBehavior.Result()
            result.success = False
            result.message = f"Invalid behavior_config for {skill_dir}: {exc}"
            self.get_logger().error(result.message)
            goal_handle.abort()
            return result

        self.get_logger().info(f"Executing {validated.behavior_type} behavior at skill_dir: {skill_dir}")

        # Reset cancel flag
        self._cancel_requested.clear()

        # Execute the behavior
        outcome, reason = self._execute_behavior(goal_handle, skill_dir, validated)

        # Set result based on outcome
        result = ExecuteBehavior.Result()

        if outcome == "SUCCESS":
            result.success = True
            result.message = f"Behavior {skill_dir} completed successfully. {reason}"
            goal_handle.succeed()
            self.get_logger().info(f"Behavior {skill_dir} succeeded: {reason}")
        elif outcome == "CANCELLED":
            result.success = False
            result.message = f"Behavior {skill_dir} was cancelled. {reason}"
            goal_handle.canceled()
            self.get_logger().info(f"Behavior {skill_dir} was canceled: {reason}")
        elif outcome == "FAILURE":
            result.success = False
            result.message = f"Behavior {skill_dir} failed. {reason}"
            goal_handle.abort()
            self.get_logger().error(f"Behavior {skill_dir} failed: {reason}")
        else:
            result.success = False
            result.message = f"Behavior {skill_dir} failed with unexpected outcome: {outcome}. {reason}"
            goal_handle.abort()
            self.get_logger().error(f"Behavior {skill_dir} failed with unexpected outcome: {outcome}. {reason}")

        return result

    def _execute_behavior(self, goal_handle, skill_dir, validated: ValidatedBehavior):
        """Execute a behavior based on its validated configuration."""
        try:
            self.current_goal_handle = goal_handle
            self.execution_running = True
            self._start_sensor_subscriptions()

            behavior_type = validated.behavior_type
            self.get_logger().info(f"Executing {behavior_type} behavior at: {skill_dir}")

            if behavior_type == "learned":
                assert isinstance(validated.params, LearnedExecCfg)
                assert validated.resolved_path is not None
                return self._execute_learned_behavior(
                    goal_handle,
                    skill_dir,
                    validated.params,
                    checkpoint_path=validated.resolved_path,
                )
            elif behavior_type == "poses":
                assert isinstance(validated.params, PosesExecCfg)
                return self._execute_poses_behavior(goal_handle, skill_dir, validated.params)
            elif behavior_type == "replay":
                assert isinstance(validated.params, ReplayExecCfg)
                assert validated.resolved_path is not None
                return self._execute_replay_behavior(
                    goal_handle,
                    skill_dir,
                    validated.params,
                    replay_path=validated.resolved_path,
                )
            else:
                # Unreachable: validator restricts behavior_type to the three
                # branches above. Kept as defense in depth.
                self.get_logger().error(f"Unknown behavior type: {behavior_type}")
                return "FAILURE", f"Unknown behavior type: {behavior_type}"

        except Exception as e:
            self.get_logger().error(f"Error executing behavior {skill_dir}: {e}")
            return "FAILURE", f"Exception during execution: {str(e)}"
        finally:
            self._stop_sensor_subscriptions()
            self.execution_running = False
            self.current_goal_handle = None
            # Drop the jerk reference so the next behavior's first step isn't compared
            # against this one's final action.
            self._prev_action_np = None
            self._release_policy()

    def _release_policy(self):
        """Free the loaded policy and reclaim GPU memory."""
        if self.current_policy is None:
            return
        try:
            del self.current_policy
            self.current_policy = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            self.get_logger().info("Policy released and GPU memory freed")
        except Exception as e:
            self.get_logger().warn(f"Error releasing policy: {e}")
            self.current_policy = None

    def _execute_learned_behavior(self, goal_handle, skill_dir, params: LearnedExecCfg, checkpoint_path: str):
        """Execute a learned behavior using ACT policy.

        Config has already been validated by ``validate_behavior_config``;
        this method trusts types, bounds, and file existence.
        """
        try:
            behavior_name = os.path.basename(skill_dir.rstrip("/"))
            action_dim = params.action_dim
            duration = params.duration
            progress_threshold = params.progress_threshold
            start_pose = params.start_pose
            end_pose = params.end_pose
            start_pose_time = params.start_pose_time
            end_pose_time = params.end_pose_time
            n_action_steps_override = params.n_action_steps
            if n_action_steps_override is None and self.default_n_action_steps > 0:
                n_action_steps_override = self.default_n_action_steps

            # Load policy
            if not self._load_policy_for_behavior(
                checkpoint_path,
                action_dim,
                n_action_steps_override=n_action_steps_override,
            ):
                return "FAILURE", f"Failed to load policy from {checkpoint_path}"

            # Don't run inference on empty/stale buffers: wait for every required
            # sensor to come online first, and bail out if they don't.
            if not self._wait_for_sensors():
                return (
                    "FAILURE",
                    "Required sensors not available (cameras or joint state). "
                    "If the arm joint state is missing, please check that the arm's USB-C cable is plugged in.",
                )

            # Set head to AI position for optimal camera angle
            self.get_logger().info("Setting head to AI position for optimal camera angle")
            self._set_head_ai_position()

            # Move to start pose if specified
            if start_pose:
                self.get_logger().info(f"Moving to start pose: {start_pose} (time: {start_pose_time}s)")
                if not self.call_arm_goto_service(start_pose, start_pose_time):
                    self.get_logger().error("Failed to move to start pose")
                    return "FAILURE", "Failed to move arm to start pose"

            # Execute policy inference
            start_time = time.time()
            inference_hz = self.inference_hz
            period = 1.0 / inference_hz
            early_termination = False
            stop_reason = None

            # Auto-stop is opt-in per skill. When off, the loop runs to the
            # ``duration`` hard cap and the detector is never consulted.
            stop_detector = None
            if params.auto_stop:
                stop_detector = LearnedStopDetector(
                    min_duration=params.min_duration,
                    progress_threshold=progress_threshold,
                    progress_ema_alpha=params.progress_ema_alpha,
                    engage_below=params.engage_below,
                    stable_min=params.stable_min,
                    stable_seconds=params.stable_seconds,
                )

            iteration_count = 0

            auto_stop_note = f"progress threshold: {progress_threshold}" if params.auto_stop else "auto-stop off"
            self.get_logger().info(
                f"Starting policy inference for {duration} seconds at {inference_hz} Hz ({auto_stop_note})"
            )

            while rclpy.ok():
                loop_start = time.time()
                elapsed_time = loop_start - start_time
                iteration_count += 1

                # Check for cancellation
                if self._cancel_requested.is_set():
                    self.get_logger().info("Behavior execution canceled")
                    self._stop_robot()
                    if end_pose:
                        self.call_arm_goto_service(end_pose, end_pose_time)
                    return "CANCELLED", "User requested cancellation"

                # Check if duration completed (moved earlier to prevent infinite loop)
                if elapsed_time >= duration:
                    self.get_logger().info(f"Behavior timeout reached after {elapsed_time:.2f} seconds")
                    break

                signals = self._run_inference_once()

                # None means inputs weren't ready or inference failed this step.
                if signals is None:
                    # Bail only if a required sensor actually dropped out; otherwise skip
                    # this step and keep looping (timeout + feedback below still run).
                    if not self._check_sensor_availability():
                        self.get_logger().error("Required sensors became unavailable during execution")
                        self._stop_robot()
                        return "FAILURE", "Required sensors became unavailable during execution"
                    # The detector saw nothing this step; discount one loop period from
                    # its stability dwell so the failure window isn't counted as observed.
                    if stop_detector is not None:
                        stop_detector.note_gap(period)
                elif stop_detector is not None:
                    stop, reason = stop_detector.update(signals, elapsed_time, loop_start)
                    if stop:
                        early_termination = True
                        stop_reason = reason
                        self.get_logger().info(f"Early termination triggered! {reason}")
                        break

                # Send feedback
                remaining_time = max(0.0, duration - elapsed_time)
                feedback_msg = ExecuteBehavior.Feedback()
                feedback_msg.elapsed_time = float(elapsed_time)
                feedback_msg.remaining_time = float(remaining_time)
                feedback_msg.status = f"Executing {behavior_name} ({skill_dir})"
                goal_handle.publish_feedback(feedback_msg)

                # Maintain loop rate
                sleep_time = period - (time.time() - loop_start)
                if sleep_time > 0:
                    time.sleep(sleep_time)

            # Stop robot and move to end pose
            self._stop_robot()
            if end_pose:
                self.get_logger().info(f"Moving to end pose: {end_pose} (time: {end_pose_time}s)")
                if not self.call_arm_goto_service(end_pose, end_pose_time):
                    self.get_logger().error("Failed to move to end pose")
                    return "FAILURE", "Failed to move arm to end pose"

            # One concise summary per policy execution.
            total_elapsed = time.time() - start_time
            actual_hz = iteration_count / total_elapsed if total_elapsed > 0 else 0.0
            run_summary = f"{iteration_count} iters in {total_elapsed:.1f}s (~{actual_hz:.1f} Hz)"

            if early_termination:
                self.get_logger().info(
                    f"Learned behavior {behavior_name} completed early ({stop_reason}) — {run_summary}"
                )
                return "SUCCESS", f"Completed early: {stop_reason}"
            else:
                self.get_logger().info(f"Learned behavior {behavior_name} completed successfully — {run_summary}")
                return "SUCCESS", "Completed full duration successfully"

        except Exception as e:
            self.get_logger().error(f"Error in learned behavior execution: {e}")
            self._stop_robot()
            return "FAILURE", f"Exception during execution: {str(e)}"

    def _execute_poses_behavior(self, goal_handle, skill_dir, params: PosesExecCfg):
        """Execute a poses-based behavior."""
        behavior_name = os.path.basename(skill_dir.rstrip("/"))
        self.get_logger().info(f"Executing poses behavior: {behavior_name}")

        poses = params.poses
        # ``steps`` is the per-pose duration in seconds. Preserve legacy
        # fallback: if omitted, use ``len(poses)``.
        steps = params.steps if params.steps is not None else float(len(poses))

        try:
            for i, pose in enumerate(poses):
                # Check for cancellation
                if self._cancel_requested.is_set():
                    return "CANCELLED", "User requested cancellation"

                self.get_logger().info(f"Moving to pose {i + 1}/{len(poses)}: {pose}")

                # Send feedback
                feedback_msg = ExecuteBehavior.Feedback()
                feedback_msg.elapsed_time = float(i * steps)
                feedback_msg.remaining_time = float((len(poses) - i - 1) * steps)
                feedback_msg.status = f"Executing pose {i + 1}/{len(poses)}"
                goal_handle.publish_feedback(feedback_msg)

                # Execute pose movement
                if not self.call_arm_goto_service(pose, steps):
                    self.get_logger().error(f"Failed to reach pose {i + 1}")
                    return "FAILURE", f"Failed to reach pose {i + 1}/{len(poses)}"

                time.sleep(steps)

            self.get_logger().info(f"Poses behavior {behavior_name} completed successfully")
            return "SUCCESS", f"All {len(poses)} poses executed successfully"

        except Exception as e:
            self.get_logger().error(f"Error in poses behavior execution: {e}")
            return "FAILURE", f"Exception during poses execution: {str(e)}"

    def _execute_replay_behavior(self, goal_handle, skill_dir, params: ReplayExecCfg, replay_path: str):
        """Execute a replay-based behavior from H5 file."""
        behavior_name = os.path.basename(skill_dir.rstrip("/"))
        self.get_logger().info(f"Executing replay behavior: {behavior_name}")

        start_pose = params.start_pose
        end_pose = params.end_pose
        start_pose_time = params.start_pose_time
        end_pose_time = params.end_pose_time
        replay_hz = params.replay_frequency

        try:
            # Load H5 file and extract actions
            with h5py.File(replay_path, "r") as h5file:
                if "action" not in h5file:
                    self.get_logger().error("No 'action' dataset found in H5 file")
                    return "FAILURE", "No 'action' dataset found in H5 file"

                actions = h5file["action"][:]  # Shape: (n_steps, action_dim)
                self.get_logger().info(f"Loaded {actions.shape[0]} action steps from {replay_path}")

            if actions.shape[0] == 0:
                self.get_logger().error("No actions found in replay file")
                return "FAILURE", "No actions found in replay file"

            # Move to start pose if specified, otherwise use first action from H5
            if start_pose:
                # Use start_pose from metadata
                self.get_logger().info(f"Moving to start pose from metadata: {start_pose} (time: {start_pose_time}s)")
                if not self.call_arm_goto_service(start_pose, start_pose_time):
                    self.get_logger().error("Failed to move to start pose")
                    return "FAILURE", "Failed to move to start pose"
            else:
                # Fall back to first action from H5 file
                # Action format: [arm_joints(6), linear.x, angular.z, progress?, termination?]
                first_action = actions[0]
                first_arm_position = first_action[0:6].tolist()  # First 6 elements are arm joint positions
                self.get_logger().info(
                    f"Moving to initial arm position from H5: {first_arm_position} (time: {start_pose_time}s)"
                )
                if not self.call_arm_goto_service(first_arm_position, start_pose_time):
                    self.get_logger().error("Failed to move to initial arm position")
                    return "FAILURE", "Failed to move to initial arm position"

            # Replay parameters
            total_steps = actions.shape[0]
            step_duration = 1.0 / replay_hz
            total_duration = total_steps * step_duration

            self.get_logger().info(
                f"Starting replay: {total_steps} steps at {replay_hz} Hz (total: {total_duration:.1f}s)"
            )

            # Execute replay
            start_time = time.time()

            for step_idx in range(total_steps):
                loop_start = time.time()

                # Check for cancellation
                if self._cancel_requested.is_set():
                    self.get_logger().info("Replay execution canceled")
                    self._stop_robot()
                    if end_pose:
                        self.call_arm_goto_service(end_pose, end_pose_time)
                    return "CANCELLED", "User requested cancellation"

                # Get current action
                action = actions[step_idx]

                # Extract arm commands (first 6 elements = joint positions)
                self._publish_arm(action[0:6])

                # Extract cmd_vel commands (elements 6-7 = linear.x, angular.z).
                self._publish_base(float(action[6]), float(action[7]), self.replay_base_speed_scale)

                # Extract head command (element 8 = head angle in degrees), present only
                # for head-enabled replay skills; arm/base-only skills have width 8.
                if len(action) > 8:
                    head_msg = Int32()
                    head_msg.data = int(round(float(action[8])))
                    self.head_set_position_pub.publish(head_msg)

                # Send feedback
                elapsed_time = loop_start - start_time
                remaining_time = max(0.0, total_duration - elapsed_time)
                feedback_msg = ExecuteBehavior.Feedback()
                feedback_msg.elapsed_time = float(elapsed_time)
                feedback_msg.remaining_time = float(remaining_time)
                feedback_msg.status = f"Replaying step {step_idx + 1}/{total_steps}"
                goal_handle.publish_feedback(feedback_msg)

                # Maintain replay frequency
                sleep_time = step_duration - (time.time() - loop_start)
                if sleep_time > 0:
                    time.sleep(sleep_time)

            # Stop robot after replay and hold last arm position
            self._stop_robot()

            # Hold the last arm position to prevent drift
            last_arm_position = actions[-1][0:6].tolist()
            arm_msg = Float64MultiArray()
            arm_msg.data = [float(v) for v in last_arm_position]
            self.arm_state_pub.publish(arm_msg)
            self.get_logger().info(f"Holding last arm position: {last_arm_position}")

            # Move to end pose if specified
            if end_pose:
                self.get_logger().info(f"Moving to end pose: {end_pose} (time: {end_pose_time}s)")
                if not self.call_arm_goto_service(end_pose, end_pose_time):
                    self.get_logger().error("Failed to move to end pose")
                    return "FAILURE", "Failed to move to end pose"

            self.get_logger().info(f"Replay behavior {behavior_name} completed successfully")
            return "SUCCESS", f"Replay completed successfully with {total_steps} steps"

        except Exception as e:
            self.get_logger().error(f"Error in replay behavior execution: {e}")
            self._stop_robot()
            return "FAILURE", f"Exception during replay execution: {str(e)}"

    def _load_policy_for_behavior(self, checkpoint_path, action_dim, n_action_steps_override=None):
        """Load ACT policy for a specific behavior.

        n_action_steps_override: optional override for the ACT replanning horizon.
        When None, create_act_config falls back to min(40, chunk_size).
        """
        try:
            load_start = time.time()

            self._release_policy()

            # Expand user path
            checkpoint_path = os.path.expanduser(checkpoint_path)

            # Load checkpoint first so we can infer architecture params from it.
            # Load onto CPU (mmap avoids reading the whole file into RAM up front,
            # weights_only is faster/safer for a pure tensor state_dict). With mmap the
            # load below and the chunk_size inference touch only tensor metadata, so this
            # stays cheap on the cache-hit path where the weights are never materialized
            # into a model (see the engine-cache fast path further down). dataset_stats is
            # loaded lazily, only on a cache miss, for the same reason.
            self.get_logger().info(f"Loading checkpoint: {checkpoint_path}")
            state_dict = load_torch_file(checkpoint_path, mmap=True, log=self.get_logger().warn)

            # Unwrap checkpoint containers and strip torch.compile()/DDP key prefixes.
            # Shared with the offline engine pre-build (act_trt._main) so both infer the
            # same architecture from the same checkpoint.
            state_dict = normalize_state_dict(state_dict)

            # Infer chunk_size from checkpoint weights so the model matches the checkpoint
            chunk_size = infer_chunk_size(state_dict)
            self.get_logger().info(f"Using chunk_size={chunk_size} (inferred from checkpoint)")

            # Fail loudly if the metadata action_dim disagrees with the checkpoint's action
            # head. Otherwise load_state_dict(assign=True) silently exports a wrong-width head
            # and build_engine bakes it into a cached engine that persists across runs.
            validate_action_dim(state_dict, action_dim, checkpoint_path)

            policy_config = create_act_config(
                action_dim=action_dim,
                chunk_size=chunk_size,
                n_action_steps=n_action_steps_override,
                speed=self.policy_speed,
                temporal_ensemble_coeff=self.temporal_ensemble_coeff,
            )

            # TensorRT is the only inference path: a fused engine runs the ACT forward ~10x
            # faster (~6ms vs ~64ms eager) with near-exact accuracy (~0.02% RMSE). The engine
            # is built once per checkpoint and cached next to it (ideally pre-built at download
            # time; see innate_training_node).
            #
            # Because the TRT forward fits the 25Hz loop, we run the model every step with
            # temporal ensembling (coeff from the temporal_ensemble_coeff ROS param, see
            # manipulation_server.yaml) to blend overlapping chunk predictions and remove the
            # per-replan motion discontinuity. The coeff was resolved/validated at construction
            # (0 -> None = disabled, fall back to the chunked queue path); it feeds both
            # create_act_config above and TRTACTPolicy below.
            ensemble_coeff = self.temporal_ensemble_coeff

            if ensemble_coeff is not None:
                self.get_logger().info(
                    f"Resolved n_action_steps={policy_config.n_action_steps} "
                    "(unused while temporal ensembling is active; the model runs every step)"
                )
            else:
                self.get_logger().info(
                    f"Resolved n_action_steps={policy_config.n_action_steps} "
                    f"(chunked-queue replan interval; chunk_size={chunk_size})"
                )

            # Imported lazily: tensorrt is hardware-only, so importing at module top would
            # crash the whole node (incl. poses/replay) on a sim/dev box. Here it raises
            # only on the learned path, failing this load loudly -- TRT stays required.
            from manipulation.act_trt import TRTACTPolicy, build_engine, engine_path_for  # noqa: PLC0415

            # Engine-cache fast path: after pre-build (the intended common case) the engine
            # already exists, and TRTACTPolicy deserializes it directly -- it needs neither the
            # eager model nor the loaded dataset_stats. So only on a genuine cache miss do we
            # pay for the eager ACTPolicy (random-init backbone + transformer), the full
            # load_state_dict, the host->device transfer and the dataset_stats load -- all of
            # which exist purely to export the engine. engine_path_for matches build_engine's
            # own cache key, so a miss here is exactly a miss inside build_engine.
            engine_path = engine_path_for(checkpoint_path, action_dim, chunk_size, "fp32")
            if not os.path.exists(engine_path):
                stats_path = os.path.join(os.path.dirname(checkpoint_path), "dataset_stats.pt")
                dataset_stats = None
                try:
                    dataset_stats = load_torch_file(stats_path)
                    self.get_logger().info("Dataset stats loaded")
                except Exception as e:
                    self.get_logger().warn(f"Could not load dataset stats: {e}")

                self.get_logger().info(
                    f"Engine not cached; building eager ACTPolicy to export it "
                    f"(action_dim={action_dim}, chunk_size={chunk_size}) on device={self.device}"
                )
                # Build on CPU and adopt the checkpoint tensors directly (assign=True) to avoid
                # allocating random weights on the GPU and a redundant device transfer. The model
                # is moved to the device once, after the state dict is loaded.
                eager_policy = ACTPolicy(config=policy_config, dataset_stats=dataset_stats)

                # Load state dict with strict=False to handle potential mismatches gracefully
                load_result = eager_policy.load_state_dict(state_dict, strict=False, assign=True)

                # Log any missing or unexpected keys for debugging
                if load_result.missing_keys:
                    self.get_logger().warn(
                        f"Missing keys in checkpoint: {load_result.missing_keys[:5]}..."
                        if len(load_result.missing_keys) > 5
                        else f"Missing keys: {load_result.missing_keys}"
                    )
                if load_result.unexpected_keys:
                    self.get_logger().warn(
                        f"Unexpected keys in checkpoint: {load_result.unexpected_keys[:5]}..."
                        if len(load_result.unexpected_keys) > 5
                        else f"Unexpected keys: {load_result.unexpected_keys}"
                    )
                if not load_result.missing_keys and not load_result.unexpected_keys:
                    self.get_logger().info("Checkpoint loaded successfully with all keys matching")

                # Single host->device transfer now that weights are loaded on CPU
                eager_policy = eager_policy.to(self.device).eval()
                engine_path = build_engine(
                    eager_policy, checkpoint_path, self.device, precision="fp32", log=self.get_logger().info
                )
                # Release the eager export model; only the engine is needed from here on.
                del eager_policy
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

            trt_policy = TRTACTPolicy(engine_path, policy_config, self.device, temporal_ensemble_coeff=ensemble_coeff)
            self.current_policy = trt_policy
            self.get_logger().info(f"Using TensorRT engine for inference (temporal_ensemble_coeff={ensemble_coeff})")

            # Store the current action dimension
            self.current_action_dim = action_dim

            load_time = time.time() - load_start
            self.get_logger().info(
                f"Policy loaded in {load_time:.2f}s from {checkpoint_path} with action_dim={action_dim}"
            )

            # Warm-up inference: one pass exercises the engine and triggers any lazy CUDA init.
            self.get_logger().info("Running warm-up inference...")
            warmup_start = time.time()
            dummy_batch = {
                "observation.image_camera_1": torch.zeros(1, 3, 224, 224, device=self.device),
                "observation.image_camera_2": torch.zeros(1, 3, 224, 224, device=self.device),
                "observation.state": torch.zeros(1, 6, device=self.device),
            }
            with torch.no_grad():
                _ = self.current_policy.select_action(dummy_batch)
            if self.device.type == "cuda":
                torch.cuda.synchronize()
            warmup_time = time.time() - warmup_start
            self.get_logger().info(f"Warm-up inference completed in {warmup_time:.2f}s")

            # Clear any state the warmup left behind (queued dummy actions / seeded
            # ensemble) so real execution starts from a clean slate.
            self.current_policy.reset()

            return True

        except Exception as e:
            self.get_logger().error(f"Failed to load policy: {e}")
            return False

    def _start_sensor_subscriptions(self):
        """Create sensor subscriptions once; they live for the node's lifetime.

        They are never destroyed (see __init__) -- destroying a subscription under the
        MultiThreadedExecutor races _take_subscription and crashes the process. When no
        behavior is running the image callbacks early-return, so the only idle cost is
        message deserialization, not the cv_bridge conversion.
        """
        if self._image1_sub is not None:
            return
        image_qos = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, depth=1)
        self._image1_sub = self.create_subscription(
            Image, "/mars/main_camera/left/image_raw", self.image1_callback, image_qos
        )
        self._image2_sub = self.create_subscription(Image, "/mars/arm/image_raw", self.image2_callback, image_qos)
        self._joint_sub = self.create_subscription(JointState, "/mars/arm/state", self.joint_state_callback, 10)
        self.get_logger().info("Sensor subscriptions started")

    def _stop_sensor_subscriptions(self):
        """Drop cached sensor data when idle. Subscriptions are kept alive (see __init__);
        the image callbacks early-return while idle so they cost almost nothing."""
        self.latest_image1 = None
        self.latest_image2 = None
        self.latest_joint_state = None
        self.latest_image1_timestamp = None
        self.latest_image2_timestamp = None
        self.latest_joint_timestamp = None

    def image1_callback(self, msg: Image):
        if not self.execution_running:
            return
        try:
            self.latest_image1 = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
            self.latest_image1_timestamp = rclpy.time.Time.from_msg(msg.header.stamp)
        except Exception as e:
            self.get_logger().error(f"Error converting image1: {e}")

    def image2_callback(self, msg: Image):
        if not self.execution_running:
            return
        try:
            self.latest_image2 = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
            self.latest_image2_timestamp = rclpy.time.Time.from_msg(msg.header.stamp)
        except Exception as e:
            self.get_logger().error(f"Error converting image2: {e}")

    def joint_state_callback(self, msg: JointState):
        if not self.execution_running:
            return
        self.latest_joint_state = msg
        self.latest_joint_timestamp = rclpy.time.Time.from_msg(msg.header.stamp)

    def _resize_matrices(self, h, w):
        """Cached (row, col) GPU matrices replicating cv2.INTER_AREA for an (h, w) ->
        image_size resize. INTER_AREA is separable, so its weights recover exactly from
        identity inputs and apply as two matmuls."""
        key = (h, w)
        mats = self._resize_mats.get(key)
        if mats is None:
            out_h, out_w = self.image_size[1], self.image_size[0]
            row = cv2.resize(np.eye(h, dtype=np.float32), (h, out_h), interpolation=cv2.INTER_AREA)
            col = cv2.resize(np.eye(w, dtype=np.float32), (out_w, w), interpolation=cv2.INTER_AREA)
            mats = (
                torch.from_numpy(row).to(self.device),
                torch.from_numpy(col).to(self.device),
            )
            self._resize_mats[key] = mats
        return mats

    def _resize_normalize_gpu(self, img_bgr):
        """Resize a HWC uint8 BGR frame to (3, image_size) float32 in [0, 1] on the GPU,
        matching the cv2.INTER_AREA + /255 + CHW transpose the model was trained on."""
        h, w = img_bgr.shape[:2]
        row, col = self._resize_matrices(h, w)
        src = torch.from_numpy(np.ascontiguousarray(img_bgr)).to(self.device, non_blocking=True).float()
        # Contract columns first: the (h, out_w, 3) intermediate is the cheaper order for a
        # landscape frame (torch.einsum has no per-call optimize flag).
        cols = torch.einsum("jkc,kl->jlc", src, col)
        resized = torch.einsum("ij,jlc->ilc", row, cols)
        # Round to uint8 before normalizing: cv2.resize returns a uint8 image, so training
        # saw integer-quantized pixels. Matmuls stay in float, so round here to match.
        return resized.round().permute(2, 0, 1) / 255.0

    def _run_inference_once(self):
        """Run one inference step and publish its commands.

        Returns a :class:`StepSignals` (progress) for the auto-stop detector, or None
        if inputs aren't ready yet or inference failed.
        """
        if (
            not self.current_policy
            or self.latest_image1 is None
            or self.latest_image2 is None
            or self.latest_joint_state is None
        ):
            return None

        try:
            profiling = self.inference_profile_pub.get_subscription_count() > 0

            t0 = time.perf_counter()
            # BGR + INTER_AREA matches the training pipeline.
            img1 = self._resize_normalize_gpu(self.latest_image1).unsqueeze(0)
            img2 = self._resize_normalize_gpu(self.latest_image2).unsqueeze(0)
            qpos = np.asarray(self.latest_joint_state.position, dtype=np.float32)
            batch = {
                "observation.image_camera_1": img1,
                "observation.image_camera_2": img2,
                "observation.state": torch.tensor(qpos, device=self.device).unsqueeze(0),
            }
            # Sync so the GPU resize is attributed to preprocess, not inference; no net cost
            # since the engine waits on it anyway. Only when profiling.
            if profiling and self.device.type == "cuda":
                torch.cuda.current_stream().synchronize()
            t1 = time.perf_counter()

            with torch.no_grad():
                action = self.current_policy.select_action(batch)
            t_sel = time.perf_counter()
            action_np = action.cpu().numpy().squeeze(0)  # .cpu() blocks on the remaining GPU work
            t2 = time.perf_counter()
            engine_ran = bool(getattr(self.current_policy, "engine_ran", True))
            engine_ms = float(getattr(self.current_policy, "last_engine_ms", 0.0))

            if action_np.shape[0] < self.current_action_dim:
                self.get_logger().error(
                    f"Action has wrong dimensions. Expected {self.current_action_dim}, got {action_np.shape[0]}"
                )
                return None

            # Action layout: [0:6] arm joint targets, [6] linear.x, [7] angular.z,
            # [8] progress, [9] termination.
            self._publish_base(float(action_np[6]), float(action_np[7]), self.learned_base_speed_scale)
            self._publish_arm(action_np[:6])
            t3 = time.perf_counter()

            # Motion signals for the profiler's jerk metrics. arm_delta is the L2 change
            # in the commanded joint targets vs. the previous step; base_speed is the
            # commanded base velocity magnitude. Update prev first (and guard on matching
            # shape: switching to a behavior with a different action_dim leaves a stale
            # prev whose slices won't broadcast) so a failure below can't stall it.
            prev = self._prev_action_np
            self._prev_action_np = action_np
            arm_delta = (
                float(np.linalg.norm(action_np[:6] - prev[:6]))
                if prev is not None and prev.shape == action_np.shape
                else None
            )
            base_speed = abs(float(action_np[6])) + abs(float(action_np[7]))
            progress = float(action_np[8]) if self.current_action_dim >= 10 else None

            if profiling:
                # base_speed is the commanded velocity magnitude (base_jerk is its
                # per-step delta) -- both charted on the Profiling page.
                quality = {"base_speed": base_speed}
                # Full raw action (pre base-speed scaling, every dim incl. the progress /
                # termination heads): a persisted profile is then a complete per-step
                # record of the policy's output, not just derived scalars. Non-finite
                # entries become null here since fin() below only handles scalars.
                quality["action"] = [float(a) if math.isfinite(a) else None for a in action_np]
                if progress is not None:
                    quality["progress"] = progress
                if arm_delta is not None:
                    quality["arm_jerk"] = arm_delta
                    quality["base_jerk"] = float(np.linalg.norm(action_np[6:8] - prev[6:8]))
                disagreement = getattr(self.current_policy, "last_disagreement", None)
                if disagreement is not None:
                    quality["disagreement"] = float(disagreement)
                self._publish_inference_profile(t0, t1, t_sel, t2, t3, engine_ran, engine_ms, quality)

            return StepSignals(progress=progress)

        except Exception as e:
            self.get_logger().error(f"Error during inference: {e}")
            return None

    def _publish_inference_profile(self, t0, t1, t_sel, t2, t3, engine_ran, engine_ms, quality=None):
        """Publish one inference step's timing breakdown (ms) as JSON for the Profiling page.

        Non-finite values are coerced to null: a diverging policy emits NaN/Inf actions,
        which turn the derived jerk/progress fields non-finite. json.dumps would then write
        a bare `NaN` token (invalid JSON), and the page's JSON.parse would reject the whole
        sample — blanking the profiler exactly when divergence is most worth seeing.
        allow_nan=False is a tripwire for any field we missed; the publish is isolated so a
        serialization error can never break the live inference path.
        """
        self._inference_seq += 1

        def fin(v):
            return v if math.isfinite(v) else None

        payload = {
            "seq": self._inference_seq,
            "t": time.time(),
            "preprocess_ms": fin((t1 - t0) * 1000.0),
            "inference_ms": fin((t2 - t1) * 1000.0),
            "engine_ms": fin(engine_ms),
            "transfer_ms": fin((t2 - t_sel) * 1000.0),
            "postprocess_ms": fin((t3 - t2) * 1000.0),
            "total_ms": fin((t3 - t0) * 1000.0),
            "engine_ran": bool(engine_ran),
            "period_ms": fin(1000.0 / self.inference_hz),
        }
        if quality:
            # Lists (the raw action vector) are pre-sanitized at construction; fin()
            # only guards scalar fields.
            payload.update({k: v if isinstance(v, list) else fin(v) for k, v in quality.items()})
        try:
            msg = String()
            msg.data = json.dumps(payload, allow_nan=False)
            self.inference_profile_pub.publish(msg)
        except (ValueError, TypeError) as e:
            self.get_logger().warning(f"Skipping inference profile sample: {e}")

    def _publish_arm(self, positions):
        """Publish a single absolute joint-position command (first 6 joints)."""
        arm_msg = Float64MultiArray()
        arm_msg.data = [float(v) for v in positions]
        self.arm_state_pub.publish(arm_msg)

    def _publish_base(self, raw_linear_x, raw_angular_z, scale):
        """Publish a base velocity command, de-rated by `scale`, used by the learned and replay paths."""
        twist_msg = Twist()
        twist_msg.linear.x = raw_linear_x * scale
        twist_msg.angular.z = raw_angular_z * scale
        self.cmd_vel_pub.publish(twist_msg)

    def _stop_robot(self):
        """Stop the robot by sending zero commands."""
        twist_msg = Twist()
        twist_msg.linear.x = 0.0
        twist_msg.angular.z = 0.0
        self.cmd_vel_pub.publish(twist_msg)

    def _set_head_ai_position(self):
        """Set the head to AI position for recording/policy execution."""
        try:
            if not self.head_ai_position_client.service_is_ready():
                self.get_logger().warn("Head AI position service not available")
                return

            self.head_ai_position_client.call_async(Trigger.Request())
            self.get_logger().info("Head AI position command sent")
            time.sleep(3.0)  # Wait for head to move to AI position
        except Exception as e:
            self.get_logger().error(f"Error setting AI position: {e}")

    def _wait_for_sensors(self, timeout=2.0, poll_interval=0.1):
        """Poll until every required sensor is publishing, or give up after ``timeout``.

        Returns True once all sensors are available, False if the timeout elapses
        first. Returns immediately when sensors are already ready.
        """
        # Monotonic deadline: wall-clock time can jump (NTP, manual set) and
        # either fire early or hang the wait, so don't gate the timeout on it.
        deadline = time.monotonic() + timeout
        while not self._check_sensor_availability():
            if time.monotonic() > deadline:
                self.get_logger().error(
                    f"Required sensors not available after {timeout:.0f}s. Cannot execute learned behavior."
                )
                return False
            time.sleep(poll_interval)
        self.get_logger().info("All sensors available")
        return True

    def _check_sensor_availability(self):
        """Check if all required sensors are providing data."""
        current_time = self.get_clock().now()
        timeout_threshold = 2.0  # seconds

        # Check if we have received any data at all
        if self.latest_image1 is None:
            self.get_logger().warn("Camera 1 (/mars/main_camera/left/image_raw) has never received data")
            return False

        if self.latest_image2 is None:
            self.get_logger().warn("Camera 2 (/mars/arm/image_raw) has never received data")
            return False

        if self.latest_joint_state is None:
            self.get_logger().warn(
                "Joint state (/mars/arm/state) has never received data "
                "- please check that the arm's USB-C cable is plugged in"
            )
            return False

        # Check if data is recent (within timeout threshold)
        if self.latest_image1_timestamp is not None:
            time_diff = (current_time - self.latest_image1_timestamp).nanoseconds / 1e9
            if time_diff > timeout_threshold:
                self.get_logger().warn(f"Camera 1 data is stale ({time_diff:.2f}s old)")
                return False

        if self.latest_image2_timestamp is not None:
            time_diff = (current_time - self.latest_image2_timestamp).nanoseconds / 1e9
            if time_diff > timeout_threshold:
                self.get_logger().warn(f"Camera 2 data is stale ({time_diff:.2f}s old)")
                return False

        if self.latest_joint_timestamp is not None:
            time_diff = (current_time - self.latest_joint_timestamp).nanoseconds / 1e9
            if time_diff > timeout_threshold:
                self.get_logger().warn(f"Joint state data is stale ({time_diff:.2f}s old)")
                return False

        return True

    def call_arm_goto_service(self, position, time_duration=5):
        """Call the arm goto service with specified position and wait for completion."""
        if not self.arm_goto_client.wait_for_service(timeout_sec=2.0):
            self.get_logger().error("Arm goto service not available")
            return False

        # Ensure position is a list of floats (JSON parsing may produce other types)
        if not position or len(position) == 0:
            self.get_logger().warn("Empty position provided to arm goto service, skipping")
            return True

        request = GotoJS.Request()
        request.data.data = [float(p) for p in position]
        request.time = float(time_duration)

        try:
            future = self.arm_goto_client.call_async(request)
            self.get_logger().info(
                f"Arm goto service called with position: {[float(p) for p in position]} (time: {time_duration}s)"
            )

            # The goto completes when the arm ARRIVES: nominal duration plus
            # settle and scheduling drift (measured 0.2-0.25s past nominal on
            # an idle sim). Wait past the arm server's own internal bound
            # instead of racing it.
            timeout_sec = time_duration + 6.0
            start_wait = time.time()
            while not future.done():
                if self._cancel_requested.is_set():
                    # The command is already dispatched (the arm finishes or
                    # is preempted by the cleanup goto); stop waiting so
                    # cancellation tears the behavior down promptly.
                    self.get_logger().info("Arm goto wait interrupted by cancel request")
                    return False
                if time.time() - start_wait > timeout_sec:
                    self.get_logger().error(f"Arm goto service timed out after {timeout_sec}s")
                    return False
                time.sleep(0.05)  # Small sleep to avoid busy-waiting

            # Check result
            result = future.result()
            if result is not None:
                self.get_logger().info("Arm goto service completed successfully")
                return True
            else:
                self.get_logger().error("Arm goto service returned None result")
                return False

        except Exception as e:
            self.get_logger().error(
                f"Error calling arm goto service: {e}, position type: {type(position)}, position: {position}"
            )
            return False


def main(args=None):
    rclpy.init(args=args)
    node = ManipulationServer()

    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)

    try:
        executor.spin()
    except KeyboardInterrupt:
        node.get_logger().info("Behavior server shutting down.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
