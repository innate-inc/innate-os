#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skills action server: executes skills dispatched as ExecuteSkill goals.

Focused on the action/execution flow — code-skill execution, physical-skill
delegation to behavior_server (incl. cancellation + CLI worker), and the goal
lifecycle. Skill discovery/metadata/publishing/reload lives in
:mod:`brain_client.skills.catalog`; live robot-state injection lives in
:mod:`brain_client.skills.robot_state`. This node wires those together and owns
the action server.
"""

from __future__ import annotations

import inspect
import json
import os
import queue
import threading
import time
import traceback
import uuid
from typing import get_args

import rclpy
from brain_messages.action import ExecuteBehavior, ExecuteSkill
from brain_messages.srv import CreatePhysicalSkill, DeleteSkill, ReloadSkillsAgents, SaveAsReplaySkill
from innate.skills import SkillCancelled, SkillFailed, use_invoker
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger

from brain_client.perception.camera_provider import CameraProvider
from brain_client.robot.head import HeadInterface
from brain_client.robot.manipulation import ManipulationInterface
from brain_client.robot.mobility import MobilityInterface
from brain_client.skills.catalog import SkillRepository
from brain_client.skills.cli_bridge import SkillCliBridge, SkillCliGoalHandle
from brain_client.skills.invoker import SkillInvoker
from brain_client.skills.robot_state import RobotStateProvider
from brain_client.skills.types import RobotStateType, SkillResult, normalize_skill_result


def _annotation_is_float(annotation) -> bool:
    """True if a param annotation is ``float`` (or a union including it).

    Handles both real type objects and string annotations (skills using
    ``from __future__ import annotations`` expose ``"float"`` instead).
    """
    if annotation is float or annotation == "float":
        return True
    return any(arg is float or arg == "float" for arg in get_args(annotation))


def _coerce_numeric_inputs(skill, inputs: dict) -> dict:
    """Widen whole-number ints to floats for ``float``-annotated params.

    JSON has a single number type, so a UI/agent serializing a float param
    whose value happens to be whole (e.g. ``x=3``) sends an int. ROS
    ``float64`` setters reject ints (``must be of type 'float'``), so we match
    each value to its declared ``execute()`` annotation before dispatch. bools
    are left alone (``bool`` subclasses ``int``)."""
    try:
        signature = inspect.signature(skill.execute)
    except (TypeError, ValueError):
        return inputs
    coerced = dict(inputs)
    for name, value in inputs.items():
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        param = signature.parameters.get(name)
        if param is not None and _annotation_is_float(param.annotation):
            coerced[name] = float(value)
    return coerced


class SkillsActionServer(Node):
    # Max wait for a cancelling skill's teardown before a new goal is rejected.
    TEARDOWN_GRACE_SEC = 2.0

    def __init__(self):
        super().__init__("skills_action_server")

        # Camera images handled by a dedicated lightweight node (own thread)
        self._camera_node = CameraProvider()

        self.declare_parameter("cmd_vel_topic", "/cmd_vel")
        self.cmd_vel_topic = self.get_parameter("cmd_vel_topic").value
        self.declare_parameter("head_position_topic", "/mars/head/set_position")
        self.head_position_topic = self.get_parameter("head_position_topic").value
        self.declare_parameter("head_current_position_topic", "/mars/head/current_position")
        self.head_current_position_topic = self.get_parameter("head_current_position_topic").value

        # Robot interfaces injected into skills.
        self.manipulation = ManipulationInterface(self, self.get_logger(), lazy=True)
        self.mobility = MobilityInterface(self, self.get_logger(), self.cmd_vel_topic)
        self.head = HeadInterface(self, self.get_logger(), self.head_position_topic)

        # Robot-state provider (subscriptions, interface injection, live state).
        self.robot_state = RobotStateProvider(
            self,
            self._camera_node,
            manipulation=self.manipulation,
            mobility=self.mobility,
            head=self.head,
            head_current_position_topic=self.head_current_position_topic,
        )

        # The robot runs one skill at a time. Guards execute_callback so a goal
        # that arrives while another skill is still executing is aborted promptly
        # (see execute_callback) instead of contending for the arm/robot state.
        # The condition variable lets one incoming goal wait briefly for a
        # cancelling skill to finish tearing down (Stop→Run grace window)
        # instead of being rejected during the handover.
        # Also gates disposal of reload-retired skill instances: destroying a
        # retired instance's ROS entities mid-run would crash the skill that is
        # still spinning them, so disposal waits until execution ends.
        self._skill_execution_lock = threading.Lock()
        self._skill_free = threading.Condition(self._skill_execution_lock)
        self._skill_running = False
        self._active_goal_handle = None
        self._teardown_waiter = False
        self._pending_retired_skills = []

        # Skill catalog (discovery, metadata, publishing, reload).
        self.catalog = SkillRepository(
            self,
            interface_injector=self.robot_state.inject_required_interfaces,
            retire_instances=self._retire_skill_instances,
        )

        # Broadcasts every run's lifecycle (running/completed/failed/interrupted) so any
        # client — webapp, mobile app, another webapp tab — can see a skill someone else
        # triggered, not just the one that sent the goal. Payload mirrors ChatManager's
        # (webapp consumers read either), keyed by a fresh id per run so repeats of the
        # same skill don't collapse into one chat entry.
        self._skill_status_pub = self.create_publisher(String, "/brain/skill_status_update", 10)

        # Behavior delegation for physical skills.
        self._behavior_client = ActionClient(self, ExecuteBehavior, "/behavior/execute")
        self._behavior_goal_lock = threading.Lock()
        self._behavior_goal_handles = {}
        self._behavior_goal_cancel_requested = set()
        self._behavior_goal_cancel_sent = set()

        # ReentrantCallbackGroup so a cancel request can be serviced *while* a
        # skill's execute_callback is blocked waiting on the behavior result.
        # With the default MutuallyExclusiveCallbackGroup the cancel callback is
        # skipped until execute returns, so a running physical/policy skill can't
        # be interrupted (the cancel never reaches behavior_server). The awaited
        # result future still completes inside execute's nested spin; only the
        # separate cancel service callback was being blocked by the group.
        self._action_server = ActionServer(
            self,
            ExecuteSkill,
            "execute_skill",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
            callback_group=ReentrantCallbackGroup(),
        )

        # CLI skill worker (runs CLI-submitted skills off the main spin thread).
        self._cli_skill_tasks = queue.Queue()
        self._cli_skill_worker_stop = threading.Event()
        self._cli_skill_worker = threading.Thread(target=self._run_cli_skill_worker, daemon=True)
        self._cli_skill_worker.start()
        self._skill_cli_bridge = SkillCliBridge(self.get_logger(), self._submit_cli_skill)

        # Services delegate to the catalog.
        self._reload_srv = self.create_service(Trigger, "/brain/reload_primitives", self._handle_reload_skills)
        self._create_physical_skill_srv = self.create_service(
            CreatePhysicalSkill, "/brain/create_physical_skill", self._handle_create_physical_skill
        )
        self._save_replay_skill_srv = self.create_service(
            SaveAsReplaySkill, "/brain/recorder/save_as_replay_skill", self._handle_save_as_replay_skill
        )
        self._delete_skill_srv = self.create_service(DeleteSkill, "/brain/delete_skill", self._handle_delete_skill)
        self._reload_skills_srv = self.create_service(
            ReloadSkillsAgents, "/brain/reload_skills", self._handle_reload_skills_agents
        )

        self.get_logger().debug("Skills Action Server has started.")
        self.get_logger().info(f"Total skills available: {self.catalog.code_count + self.catalog.physical_count}")
        self.catalog.publish_skills_list()
        self.catalog.start_watcher()

        # Heartbeat: the roster is published once (latched), but the webapp
        # reaches /brain/available_skills through rws, which subscribes after
        # boot and never receives the latched sample. Re-emit the cached roster
        # at a low rate so late-joining clients get it within one interval.
        self._skills_heartbeat_timer = self.create_timer(3.0, self.catalog.republish_cached)

    # ================= retired skill instances =================
    def _retire_skill_instances(self, instances):
        """Dispose skill instances a reload replaced, deferring while a skill runs.

        The running skill may itself be a retired instance (reload mid-run), and
        its execute() is still spinning the ROS entities it owns — so disposal
        waits for the run to end (drained in execute_callback's finally).
        """
        with self._skill_execution_lock:
            if self._skill_running:
                self._pending_retired_skills.extend(instances)
                return
        SkillRepository.dispose_instances(instances, self.get_logger())

    # ================= service handlers (delegate to catalog) =================
    def _handle_reload_skills(self, request, response):
        try:
            self.catalog.reload_all()
            response.success = True
            response.message = f"Reloaded {self.catalog.code_count} code, {self.catalog.physical_count} physical skills"
        except Exception as e:
            response.success = False
            response.message = f"Failed to reload skills: {e}"
        return response

    def _handle_create_physical_skill(self, request, response):
        success, message, skill_dir, skill_id = self.catalog.create_physical_skill(request.name, request.kind)
        response.success = success
        response.message = message
        response.skill_directory = skill_dir
        response.skill_id = skill_id
        return response

    def _handle_save_as_replay_skill(self, request, response):
        try:
            skill_dir, skill_id, wheeled = self.catalog.save_recording_as_replay_skill(
                request.task_directory, request.name, request.guidelines, request.episode_id
            )
            response.success = True
            response.message = f"Saved replay skill '{request.name.strip()}'."
            response.skill_directory, response.skill_id, response.wheeled = skill_dir, skill_id, wheeled
        except Exception as e:
            response.success = False
            response.message = str(e)
        return response

    def _handle_delete_skill(self, request, response):
        try:
            self.catalog.delete_skill(request.skill_directory)
            response.success = True
            response.message = f"Deleted {request.skill_directory}."
        except Exception as e:
            response.success = False
            response.message = str(e)
        return response

    def _handle_reload_skills_agents(self, request, response):
        try:
            skill_ids = list(request.skills) if request.skills else []
            reloaded = self.catalog.reload_selective(skill_ids)
            response.success = True
            response.reloaded_skills = reloaded
            response.reloaded_agents = []  # Skills server doesn't handle agents
            response.message = f"Reloaded {len(reloaded)} skills: {reloaded}"
        except Exception as e:
            response.success = False
            response.message = f"Failed to reload skills: {e}"
            response.reloaded_skills = []
            response.reloaded_agents = []
        return response

    # ================= action lifecycle =================
    def goal_callback(self, goal_request):
        self.get_logger().debug(f"Received goal for skill: '{goal_request.skill_type}'")
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        try:
            skill_type = goal_handle.request.skill_type
            code_entry = self.catalog.get_code_skill(skill_type)
            is_physical = self.catalog.get_physical_skill(skill_type) is not None

            if code_entry is not None:
                _name, instance = code_entry
                self.get_logger().debug(f"Canceling code skill: {skill_type}")
                instance.cancel()
            elif is_physical:
                self.get_logger().debug(f"Canceling physical skill: {skill_type}")
                self._request_behavior_goal_cancel(goal_handle, skill_type)
            else:
                self.get_logger().warning(f"Unknown skill type: {skill_type}")
        except Exception as e:
            self.get_logger().error(f"Error in cancel_callback: {str(e)}")
            self.get_logger().debug("Attempting to cancel all code skills")
            for sid, (_name, instance) in self.catalog.all_code_skills():
                try:
                    instance.cancel()
                except Exception as cancel_error:
                    self.get_logger().error(f"Error canceling {sid}: {str(cancel_error)}")
        return CancelResponse.ACCEPT

    def execute_callback(self, goal_handle):
        self.get_logger().debug(f"[SAS] execute_callback ENTER for skill: '{goal_handle.request.skill_type}'")
        try:
            inputs = json.loads(goal_handle.request.inputs)
        except Exception as e:
            self.get_logger().error(f"Invalid JSON for inputs: {str(e)}")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False, message="Invalid inputs JSON", success_type=SkillResult.FAILURE.value
            )

        skill_type = goal_handle.request.skill_type

        # No skill-status broadcast for a refused goal — it never ran.
        if not self._claim_skill_slot(goal_handle):
            self.get_logger().warn(f"Skill '{skill_type}' requested but another skill is already running")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message="Another skill is already running",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )

        name = self._skill_display_name(skill_type)
        run_id = uuid.uuid4().hex
        self._publish_skill_status(run_id, skill_type, name, "running", inputs=inputs)
        try:
            if self.catalog.get_code_skill(skill_type) is not None:
                result = self._execute_code_skill(goal_handle, skill_type, inputs)
            elif self.catalog.get_physical_skill(skill_type) is not None:
                result = self._execute_physical_skill(goal_handle, skill_type, inputs)
            else:
                self.get_logger().error(f"Skill '{skill_type}' not available")
                self.get_logger().error(f"Available skills: {self.catalog.all_skill_ids()}")
                goal_handle.abort()
                result = ExecuteSkill.Result(
                    success=False,
                    message="Skill not available",
                    skill_type=skill_type,
                    success_type=SkillResult.FAILURE.value,
                )
        except Exception as e:
            # A 'running' broadcast went out above — a terminal status MUST
            # follow or every client shows this skill as active forever.
            self._publish_skill_status(run_id, skill_type, name, "failed", str(e) or "internal error", inputs=inputs)
            raise
        finally:
            self._release_skill_slot()
        status, reason = self._terminal_skill_status(result)
        self._publish_skill_status(run_id, skill_type, name, status, reason, inputs=inputs)
        return result

    @staticmethod
    def _publish_initial_feedback(goal_handle):
        """Emit one "running" feedback the moment execution starts: rws only
        relays its assigned goal_id — which an app cancel must bind to — on the
        first feedback, and skills may produce none of their own for a while."""
        feedback = ExecuteSkill.Feedback()
        feedback.feedback = "running"
        feedback.image_b64 = ""
        goal_handle.publish_feedback(feedback)

    def _skill_display_name(self, skill_type: str) -> str:
        code_entry = self.catalog.get_code_skill(skill_type)
        if code_entry is not None:
            return code_entry[0]
        physical = self.catalog.get_physical_skill(skill_type)
        if physical is not None:
            return physical.get("metadata", {}).get("name", skill_type)
        return skill_type

    @staticmethod
    def _terminal_skill_status(result) -> tuple[str, str | None]:
        if result.success and result.success_type == SkillResult.SUCCESS.value:
            return "completed", None
        if result.success_type == SkillResult.CANCELLED.value:
            return "interrupted", None
        return "failed", result.message or None

    def _publish_skill_status(
        self, run_id: str, skill_type: str, name: str, status: str, reason: str | None = None, inputs: dict | None = None
    ) -> None:
        payload = {
            "primitive_name": name,
            "skill_name": name,
            "skill_id": skill_type,
            "primitive_id": run_id,
            "status": status,
            "timestamp": time.time(),
        }
        if reason:
            payload["reason"] = reason
        if inputs:
            payload["inputs"] = inputs
        self._skill_status_pub.publish(String(data=json.dumps(payload)))

    # ================= execution =================
    def _execute_code_skill(self, goal_handle, skill_type, inputs):
        entry = self.catalog.get_code_skill(skill_type)
        if entry is None:
            self.get_logger().error(f"Code skill '{skill_type}' disappeared during reload")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message=f"Skill '{skill_type}' was removed during a concurrent reload",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )
        _name, skill = entry

        def _publish_feedback(update_message: str, image_b64: str = None):
            feedback_msg = ExecuteSkill.Feedback()
            feedback_msg.feedback = update_message
            feedback_msg.image_b64 = image_b64 or ""
            goal_handle.publish_feedback(feedback_msg)
            self.get_logger().debug(f"Published feedback for '{skill_type}': {update_message}")

        skill.set_feedback_callback(_publish_feedback)
        skill.skills = SkillInvoker(self, goal_handle, _publish_feedback)

        try:
            self._publish_initial_feedback(goal_handle)
            self.robot_state.start_subscriptions()
            result_message, result_status = self._run_code_skill_body(skill, skill_type, inputs, goal_handle)

            if result_status == SkillResult.SUCCESS:
                self.get_logger().info(f"Skill '{skill_type}' succeeded: {result_message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True, message=result_message, skill_type=skill_type, success_type=SkillResult.SUCCESS.value
                )
            elif result_status == SkillResult.CANCELLED:
                self.get_logger().info(f"Skill '{skill_type}' cancelled: {result_message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True,
                    message=result_message,
                    skill_type=skill_type,
                    success_type=SkillResult.CANCELLED.value,
                )
            else:  # SkillResult.FAILURE
                self.get_logger().info(f"Skill '{skill_type}' failed: {result_message}")
                goal_handle.abort()
                return ExecuteSkill.Result(
                    success=False, message=result_message, skill_type=skill_type, success_type=SkillResult.FAILURE.value
                )
        except Exception as e:
            self.get_logger().error(f"Error executing skill: {str(e)}")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False, message=str(e), skill_type=skill_type, success_type=SkillResult.FAILURE.value
            )
        finally:
            self.robot_state.stop_subscriptions()

    def _run_code_skill_body(self, skill, skill_type, inputs, goal_handle):
        """Prepare robot state for a code skill and run its ``execute()``.

        Returns ``(message, SkillResult)``; goal finalization and subscriptions
        stay with the caller (the top-level goal owns them, so they stay up
        while a chaining skill runs its children). Nesting-safe: the 50 Hz
        state slot suspends/resumes (see RobotStateProvider) and the camera is
        refcounted.
        """
        skill._begin_run(goal_handle)
        if skill._cancelled:
            self.get_logger().info(f"Skill '{skill_type}' cancelled before it started")
            return "Skill cancelled before it started", SkillResult.CANCELLED

        required_states = skill.get_required_robot_states()
        needs_camera = required_states and (
            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64 in required_states
            or RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64 in required_states
        )
        try:
            if needs_camera:
                self._camera_node.start()
            # singleton instances keep state from previous runs; drop it so a
            # skill never mistakes a stale value for fresh sensor data
            skill.clear_robot_state()
            self.robot_state.update_skill_robot_state(skill)
            if needs_camera:
                # The camera subscription above is brand new; wait (bounded)
                # for first frames so execute() doesn't race them and fail.
                self.robot_state.wait_for_camera_states(skill, required_states)
            if required_states:
                self.robot_state.begin_continuous_updates(skill)
                self.get_logger().info(f"Started continuous state updates for '{skill_type}' at 50Hz")
            # innate.skills proxies route to this skill's invoker while
            # execute() runs
            with use_invoker(skill.skills):
                return normalize_skill_result(skill.execute(**_coerce_numeric_inputs(skill, inputs)))
        except SkillCancelled as e:
            return str(e) or "Skill cancelled", SkillResult.CANCELLED
        except SkillFailed as e:
            return str(e) or "Skill failed", SkillResult.FAILURE
        finally:
            if required_states:
                self.robot_state.end_continuous_updates()
            if needs_camera:
                self._camera_node.stop()

    def _execute_physical_skill(self, goal_handle, skill_type, inputs):
        self.get_logger().info(f"Delegating physical skill '{skill_type}' to behavior_server")
        physical_data = self.catalog.get_physical_skill(skill_type)
        if physical_data is None:
            self.get_logger().error(f"Physical skill '{skill_type}' disappeared during reload")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message=f"Skill '{skill_type}' was removed during a concurrent reload",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )
        self.robot_state.start_subscriptions()
        try:
            success, message, success_type, finalize = self._run_physical_skill(goal_handle, skill_type, physical_data)
            getattr(goal_handle, finalize)()
            return ExecuteSkill.Result(
                success=success, message=message, skill_type=skill_type, success_type=success_type
            )
        finally:
            self.robot_state.stop_subscriptions()

    def _run_physical_skill(self, goal_handle, skill_type, physical_data):
        """Send a physical skill to behavior_server and wait for its result.

        Returns ``(success, message, success_type, finalize)`` where ``finalize``
        is the goal-handle method the caller should invoke ("succeed", "abort"
        or "canceled"). Does not finalize the goal, so a chaining skill can run
        a physical child on its own goal without ending the parent.
        """
        metadata = physical_data["metadata"]
        try:
            self._publish_initial_feedback(goal_handle)
            if not self._behavior_client.wait_for_server(timeout_sec=5.0):
                self.get_logger().error("Behavior server not available!")
                return False, "Behavior server not available", SkillResult.FAILURE.value, "abort"

            behavior_goal = ExecuteBehavior.Goal()
            behavior_goal.skill_dir = physical_data["directory"]
            behavior_goal.behavior_config = json.dumps(metadata)

            self.get_logger().info(f"Sending behavior goal to behavior_server: {skill_type}")
            send_goal_future = self._behavior_client.send_goal_async(behavior_goal)

            # Wait on the behavior goal via the executor (event-based) instead of
            # re-spinning this node. The dedicated MultiThreadedExecutor services the
            # behavior response on another thread while we block here; re-spinning
            # required rclpy's global executor and raced Nav2's own global spins.
            goal_ready = self._wait_for_cli_future(send_goal_future, timeout_sec=10.0) == "done"

            if not goal_ready:
                self.get_logger().error("Timeout waiting for behavior goal acceptance")
                self._cancel_behavior_goal_when_ready(send_goal_future, skill_type)
                return False, "Timeout waiting for behavior goal acceptance", SkillResult.FAILURE.value, "abort"

            behavior_goal_handle = send_goal_future.result()
            if not behavior_goal_handle.accepted:
                self.get_logger().error("Behavior goal rejected by behavior_server")
                return False, "Behavior goal rejected by behavior_server", SkillResult.FAILURE.value, "abort"

            self._register_behavior_goal_handle(goal_handle, behavior_goal_handle, skill_type)
            self.get_logger().info("Behavior goal accepted, waiting for result...")

            result_future = behavior_goal_handle.get_result_async()
            result_wait_state = self._wait_for_cli_future(result_future, server_ready_check=self._behavior_server_ready)

            if result_wait_state == "server_unavailable":
                self.get_logger().error(f"Behavior server became unavailable while running '{skill_type}'")
                return (
                    False,
                    "Behavior server became unavailable while waiting for result",
                    SkillResult.FAILURE.value,
                    "abort",
                )

            if not result_future.done():
                self.get_logger().info(f"Physical skill '{skill_type}' cancelled before behavior result was ready")
                return True, "Physical skill cancelled", SkillResult.CANCELLED.value, "canceled"

            behavior_result = result_future.result().result
            if behavior_result.success:
                self.get_logger().info(f"Physical skill '{skill_type}' succeeded: {behavior_result.message}")
                return True, behavior_result.message, SkillResult.SUCCESS.value, "succeed"
            if "cancel" in behavior_result.message.lower():
                self.get_logger().info(f"Physical skill '{skill_type}' cancelled: {behavior_result.message}")
                return True, behavior_result.message, SkillResult.CANCELLED.value, "succeed"
            self.get_logger().error(f"Physical skill '{skill_type}' failed: {behavior_result.message}")
            return False, behavior_result.message, SkillResult.FAILURE.value, "abort"
        except Exception as e:
            self.get_logger().error(f"Unexpected error executing physical skill '{skill_type}': {e}")
            return False, f"Unexpected error executing physical skill: {e}", SkillResult.FAILURE.value, "abort"
        finally:
            self._unregister_behavior_goal_handle(goal_handle)

    # ================= behavior goal tracking =================
    def _skill_goal_key(self, goal_handle) -> int:
        return id(goal_handle)

    def _skill_goal_cancel_requested(self, goal_handle) -> bool:
        try:
            return bool(goal_handle.is_cancel_requested)
        except Exception:
            return False

    def _cancelling_teardown_in_progress(self) -> bool:
        """True when the running skill has a cancel in flight."""
        return self._active_goal_handle is not None and self._skill_goal_cancel_requested(self._active_goal_handle)

    def _claim_skill_slot(self, goal_handle) -> bool:
        """Claim the one-skill-at-a-time slot; False means another skill kept it.

        A goal that arrives while another skill is executing is refused — the
        caller aborts it so the app gets a prompt result (rejecting in
        goal_callback instead would surface nothing back through the rws
        bridge). One exception: rapid Stop→Run, where the previous skill is
        mid-teardown from a cancel — that goal waits the teardown out instead
        of failing with "already running".
        """
        with self._skill_free:
            if self._skill_running:
                self._await_cancelling_teardown()
            if self._skill_running:
                return False
            self._skill_running = True
            self._active_goal_handle = goal_handle
            return True

    def _await_cancelling_teardown(self):
        """Wait for a cancelling skill to release the slot. Call holding _skill_free.

        Only one goal waits; a pile-up keeps the prompt rejection. The first
        short wait covers a Run that beat its preceding Stop into the server.
        """
        if self._teardown_waiter:
            return
        self._teardown_waiter = True
        try:
            self._skill_free.wait_for(
                lambda: not self._skill_running or self._cancelling_teardown_in_progress(),
                timeout=0.15,
            )
            if self._cancelling_teardown_in_progress():
                self._skill_free.wait_for(lambda: not self._skill_running, timeout=self.TEARDOWN_GRACE_SEC)
        finally:
            self._teardown_waiter = False

    def _release_skill_slot(self):
        with self._skill_free:
            self._skill_running = False
            self._active_goal_handle = None
            retired, self._pending_retired_skills = self._pending_retired_skills, []
            self._skill_free.notify_all()
        SkillRepository.dispose_instances(retired, self.get_logger())

    def _register_behavior_goal_handle(self, skill_goal_handle, behavior_goal_handle, skill_type: str) -> None:
        key = self._skill_goal_key(skill_goal_handle)
        with self._behavior_goal_lock:
            self._behavior_goal_handles[key] = behavior_goal_handle
            cancel_requested = key in self._behavior_goal_cancel_requested
        if cancel_requested or self._skill_goal_cancel_requested(skill_goal_handle):
            self.get_logger().info(f"Cancel was already requested for physical skill '{skill_type}'")
            self._request_behavior_goal_cancel(skill_goal_handle, skill_type)

    def _unregister_behavior_goal_handle(self, skill_goal_handle) -> None:
        key = self._skill_goal_key(skill_goal_handle)
        with self._behavior_goal_lock:
            self._behavior_goal_handles.pop(key, None)
            self._behavior_goal_cancel_requested.discard(key)
            self._behavior_goal_cancel_sent.discard(key)

    def _request_behavior_goal_cancel(self, skill_goal_handle, skill_type: str) -> None:
        key = self._skill_goal_key(skill_goal_handle)
        with self._behavior_goal_lock:
            self._behavior_goal_cancel_requested.add(key)
            behavior_goal_handle = self._behavior_goal_handles.get(key)
            if behavior_goal_handle is None:
                return
            if key in self._behavior_goal_cancel_sent:
                return
            self._behavior_goal_cancel_sent.add(key)
        try:
            self.get_logger().info(f"Requesting behavior_server cancel for physical skill '{skill_type}'")
            behavior_goal_handle.cancel_goal_async()
        except Exception as e:
            self.get_logger().error(f"Failed to cancel behavior goal for '{skill_type}': {e}")

    def _cancel_behavior_goal_when_ready(self, send_goal_future, skill_type: str) -> None:
        def _cancel_when_ready(future):
            try:
                behavior_goal_handle = future.result()
                if behavior_goal_handle is not None and behavior_goal_handle.accepted:
                    self.get_logger().info(f"Canceling late-accepted behavior goal for physical skill '{skill_type}'")
                    behavior_goal_handle.cancel_goal_async()
            except Exception as e:
                self.get_logger().error(f"Failed to cancel late behavior goal for '{skill_type}': {e}")

        send_goal_future.add_done_callback(_cancel_when_ready)

    # ================= CLI skill worker =================
    def _submit_cli_skill(self, task):
        goal_handle = SkillCliGoalHandle(task)
        task.set_cancel_handler(lambda: self.cancel_callback(goal_handle))
        self._cli_skill_tasks.put((task, goal_handle))

    def _run_cli_skill_worker(self):
        while not self._cli_skill_worker_stop.is_set():
            try:
                item = self._cli_skill_tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            task, goal_handle = item
            try:
                task.mark_started()
                if task.cancel_event.is_set():
                    task.set_error("Skill execution was cancelled before start")
                    continue
                try:
                    result = self.execute_callback(goal_handle)
                except Exception as e:
                    self.get_logger().error(f"Unexpected error executing CLI skill '{task.skill_type}': {e}")
                    task.set_error(f"Skill execution failed: {e}")
                    continue
                if result is None:
                    task.set_error("Skill execution returned no result")
                else:
                    task.set_result(result)
            finally:
                self._unregister_behavior_goal_handle(goal_handle)

    def _behavior_server_ready(self) -> bool:
        try:
            checker = getattr(self._behavior_client, "server_is_ready", None)
            if checker is not None:
                return bool(checker())
            return bool(self._behavior_client.wait_for_server(timeout_sec=0.0))
        except Exception as e:
            self.get_logger().error(f"Could not check behavior_server readiness: {e}")
            return False

    def _wait_for_cli_future(self, future, timeout_sec=None, server_ready_check=None):
        """Wait for a ROS future while the node executor spins in the main thread."""
        if future.done():
            return "done"
        done_event = threading.Event()
        future.add_done_callback(lambda _future: done_event.set())
        deadline = time.monotonic() + timeout_sec if timeout_sec is not None else None
        while True:
            wait_timeout = 0.2
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                wait_timeout = min(wait_timeout, remaining)
            if done_event.wait(timeout=wait_timeout):
                return "done"
            if server_ready_check is not None and not server_ready_check():
                return "server_unavailable"

    # ================= teardown =================
    def destroy(self):
        self.catalog.stop_watcher()
        if hasattr(self, "_skill_cli_bridge"):
            self._skill_cli_bridge.stop()
        self._cli_skill_worker_stop.set()
        self._cli_skill_tasks.put(None)
        self._cli_skill_worker.join(timeout=1.0)
        self.manipulation.shutdown()
        self._camera_node.shutdown()
        self._action_server.destroy()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    action_server = SkillsActionServer()
    # Spin on a dedicated executor instead of rclpy's global one. Skills that drive
    # Nav2 (mobility.rotate, navigate_to_position) call BasicNavigator, whose blocking
    # helpers spin the *global* executor; sharing it with this node lets those Nav2
    # action clients enter our wait set and corrupt it ("wait set index ... out of
    # bounds" -> SIGABRT). A dedicated MultiThreadedExecutor isolates us and lets a
    # blocked physical-skill execute() wait on behavior results serviced by another
    # thread (see _wait_for_cli_future) without re-spinning this node.
    #
    # Floor the thread pool well above the max expected concurrent skill count.
    # execute_callback runs on a pool thread and blocks in _wait_for_cli_future
    # until another pool thread dispatches the behavior response it waits on; with
    # only os.cpu_count() threads (4 on a Jetson), enough concurrent skills could
    # occupy every thread and leave none to resolve their futures -> deadlock.
    executor = MultiThreadedExecutor(num_threads=max(8, (os.cpu_count() or 4) + 4))
    executor.add_node(action_server)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception:
        # An exception escaping spin() (e.g. InvalidHandle from an entity
        # destroyed while the executor was using it) must not unwind past the
        # teardown below: exiting with live zenoh entities panics rmw_zenoh's
        # Rust runtime (SIGABRT). Log it and exit through the ordered teardown;
        # launch respawns us either way, but from a clean exit.
        action_server.get_logger().fatal(f"Executor spin crashed:\n{traceback.format_exc()}")
    action_server.destroy()
    # Guard against double-shutdown: avoids a teardown RCLError that exits 1.
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == "__main__":
    main()
