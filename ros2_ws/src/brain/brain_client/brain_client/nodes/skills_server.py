#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skills action server: executes skills dispatched as ExecuteSkill goals.

Discovery/reload lives in skills.catalog; robot-state injection in
skills.robot_state. This node wires those together and owns the action server.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import traceback
import uuid

import rclpy
from brain_messages.action import ExecuteBehavior, ExecuteSkill
from brain_messages.srv import CreatePhysicalSkill, DeleteSkill, ReloadSkillsAgents, SaveAsReplaySkill
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger

from brain_client.perception.camera_provider import CameraProvider
from brain_client.robot.head import Head
from brain_client.robot.manipulation import Manipulation
from brain_client.robot.mobility import Mobility
from brain_client.robot.spatial_memory import SpatialMemory
from brain_client.skills.catalog import SkillRepository
from brain_client.skills.cli_bridge import SkillCliBridge, SkillCliGoalHandle
from brain_client.skills.invoker import SkillInvoker
from brain_client.skills.robot_state import RobotStateProvider
from brain_client.skills.types import (
    RobotStateType,
    SkillCancelled,
    SkillFailed,
    SkillOutput,
    SkillResult,
    normalize_skill_result,
    swap_run_cancel,
)

# result status -> (goal-handle finalize method, ExecuteSkill success flag)
_FINALIZE = {
    SkillResult.SUCCESS: ("succeed", True),
    SkillResult.CANCELLED: ("succeed", True),
    SkillResult.FAILURE: ("abort", False),
}


def _coerce_numeric_inputs(entry, inputs: dict) -> dict:
    """Widen whole-number ints to floats for float-annotated params.

    JSON has one number type and ROS float64 setters reject ints.
    """
    coerced = dict(inputs)
    for name, value in inputs.items():
        if isinstance(value, int) and not isinstance(value, bool) and name in entry.float_params:
            coerced[name] = float(value)
    return coerced


class SkillsActionServer(Node):
    # Max wait for a cancelling skill's teardown before a new goal is rejected.
    TEARDOWN_GRACE_SEC = 2.0

    def __init__(self):
        super().__init__("skills_action_server")

        self._camera_node = CameraProvider()

        self.declare_parameter("cmd_vel_topic", "/cmd_vel_skills")
        self.cmd_vel_topic = str(self.get_parameter("cmd_vel_topic").value)
        self.declare_parameter("head_position_topic", "/mars/head/set_position")
        self.head_position_topic = str(self.get_parameter("head_position_topic").value)
        self.declare_parameter("head_current_position_topic", "/mars/head/current_position")
        self.head_current_position_topic = str(self.get_parameter("head_current_position_topic").value)

        self.manipulation = Manipulation(self, self.get_logger(), lazy=True)
        self.mobility = Mobility(self, self.get_logger(), self.cmd_vel_topic)
        self.head = Head(self, self.get_logger(), self.head_position_topic)
        self.spatial_memory = SpatialMemory(self, self.get_logger())

        self.robot_state = RobotStateProvider(
            self,
            self._camera_node,
            manipulation=self.manipulation,
            mobility=self.mobility,
            head=self.head,
            memory=self.spatial_memory,
            head_current_position_topic=self.head_current_position_topic,
        )

        # One skill at a time; Condition lets Stop→Run wait out teardown briefly.
        self._skill_execution_lock = threading.Lock()
        self._skill_free = threading.Condition(self._skill_execution_lock)
        self._skill_running = False
        self._active_goal_handle = None
        self._teardown_waiter = False
        self._active_code_skill = None
        # Cancel that arrived before the run finished wiring up.
        self._pending_cancel_goal = None

        self.catalog = SkillRepository(self)

        self._skill_status_pub = self.create_publisher(String, "/brain/skill_status_update", 10)

        # At most one behavior goal in flight; owner is the skill goal handle.
        self._behavior_client = ActionClient(self, ExecuteBehavior, "/behavior/execute")
        self._behavior_lock = threading.Lock()
        self._behavior_owner = None
        self._behavior_handle = None
        self._behavior_cancel_requested = False
        self._behavior_cancel_sent = False

        # Reentrant so cancels are serviced while execute_callback blocks.
        self._action_server = ActionServer(
            self,
            ExecuteSkill,
            "execute_skill",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
            callback_group=ReentrantCallbackGroup(),
        )

        self._skill_cli_bridge = SkillCliBridge(self.get_logger(), self._submit_cli_skill)

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
        self._cancel_skill_srv = self.create_service(Trigger, "/brain/cancel_skill", self._handle_cancel_skill)

        self.get_logger().debug("Skills Action Server has started.")
        self.get_logger().info(f"Total skills available: {self.catalog.code_count + self.catalog.physical_count}")
        self.catalog.publish_skills_list()
        self.catalog.start_watcher()

        # rws may miss the latched roster at boot; re-emit periodically.
        self._skills_heartbeat_timer = self.create_timer(3.0, self.catalog.republish_cached)

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

    def _handle_cancel_skill(self, request, response):
        """Cancel the currently running skill (for clients without a goal handle)."""
        with self._skill_execution_lock:
            goal_handle = self._active_goal_handle if self._skill_running else None
        if goal_handle is None:
            response.success = False
            response.message = "No skill is running"
            return response
        # Outside the lock: on_cancel hooks must not deadlock the slot.
        self.cancel_callback(goal_handle)
        response.success = True
        response.message = f"Cancellation requested for '{goal_handle.request.skill_type}'"
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

    def goal_callback(self, goal_request):
        self.get_logger().debug(f"Received goal for skill: '{goal_request.skill_type}'")
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        try:
            skill_type = goal_handle.request.skill_type
            with self._skill_execution_lock:
                live = goal_handle is self._active_goal_handle
                if live:
                    # Latch so cancels that land before wiring still take effect.
                    self._pending_cancel_goal = goal_handle
                skill = self._active_code_skill if live else None
            # Code path first — same resolution order as execute_callback.
            if skill is not None:
                self.get_logger().debug(f"Canceling code skill: {skill_type}")
                skill.cancel()
            elif self.catalog.get_physical_skill(skill_type) is not None:
                self.get_logger().debug(f"Canceling physical skill: {skill_type}")
                self._request_behavior_goal_cancel(goal_handle, skill_type)
            else:
                self.get_logger().debug(f"No live run to cancel for '{skill_type}'")
        except (Exception, SkillCancelled) as e:
            # SkillCancelled is a BaseException; still return ACCEPT.
            self.get_logger().error(f"Error in cancel_callback: {str(e)}")
        return CancelResponse.ACCEPT

    def _abort_result(self, goal_handle, skill_type: str, message: str):
        goal_handle.abort()
        return ExecuteSkill.Result(
            success=False, message=message, skill_type=skill_type, success_type=SkillResult.FAILURE.value
        )

    def execute_callback(self, goal_handle):
        skill_type = goal_handle.request.skill_type
        self.get_logger().debug(f"[SAS] execute_callback ENTER for skill: '{skill_type}'")
        try:
            inputs = json.loads(goal_handle.request.inputs)
        except Exception as e:
            self.get_logger().error(f"Invalid JSON for inputs: {str(e)}")
            return self._abort_result(goal_handle, skill_type, "Invalid inputs JSON")

        # Resolve once so a mid-run reload cannot swap the entry out.
        entry = self.catalog.get_code_skill(skill_type)
        physical = None if entry is not None else self.catalog.get_physical_skill(skill_type)

        if not self._claim_skill_slot(goal_handle):
            self.get_logger().warn(f"Skill '{skill_type}' requested but another skill is already running")
            return self._abort_result(goal_handle, skill_type, "Another skill is already running")

        if entry is not None:
            name = entry.display_name
        elif physical is not None:
            name = physical.metadata.get("name", skill_type)
        else:
            name = skill_type
        run_id = uuid.uuid4().hex
        self._publish_skill_status(run_id, skill_type, name, "running", args=inputs)
        try:
            if entry is not None:
                result = self._execute_code_skill(goal_handle, skill_type, inputs, entry)
            elif physical is not None:
                result = self._execute_physical_skill(goal_handle, skill_type, physical)
            else:
                reason = self.catalog.unavailable_reason(skill_type)
                message = f"Skill '{skill_type}' {reason}" if reason else "Skill not available"
                self.get_logger().error(f"Skill '{skill_type}' not available: {self.catalog.all_skill_ids()}")
                result = self._abort_result(goal_handle, skill_type, message)
            # Terminal status goes out BEFORE the slot is released: a queued
            # goal claims the slot the moment it frees, and its "running"
            # overtaking this run's terminal status would make latest-message
            # consumers (the web app's external-run banner) clear the banner
            # for the run that just started.
            status, reason = self._terminal_skill_status(result)
            self._publish_skill_status(run_id, skill_type, name, status, reason, args=inputs)
        except Exception as e:
            # Clients stick on "running" forever without a terminal status.
            self._publish_skill_status(run_id, skill_type, name, "failed", str(e) or "internal error", args=inputs)
            raise
        finally:
            self._release_skill_slot()
        return result

    @staticmethod
    def _publish_initial_feedback(goal_handle):
        """Emit early "running" feedback so clients can bind a cancel to goal_id."""
        feedback = ExecuteSkill.Feedback()
        feedback.feedback = "running"
        feedback.image_b64 = ""
        goal_handle.publish_feedback(feedback)

    @staticmethod
    def _terminal_skill_status(result) -> tuple[str, str | None]:
        if result.success and result.success_type == SkillResult.SUCCESS.value:
            return "completed", None
        if result.success_type == SkillResult.CANCELLED.value:
            return "interrupted", None
        return "failed", result.message or None

    def _publish_skill_status(
        self,
        run_id: str,
        skill_type: str,
        name: str,
        status: str,
        reason: str | None = None,
        args: dict | None = None,
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
        # The inputs the run was given — "head emotion" alone doesn't say which
        # emotion, so clients show them alongside the name.
        if args:
            payload["args"] = args
        self._skill_status_pub.publish(String(data=json.dumps(payload)))

    def _create_run_node(self):
        """Throwaway node for one run — destroyed whole at end (avoids #497)."""
        run_node = Node(
            f"skill_run_{uuid.uuid4().hex[:12]}",
            context=self.context,
            enable_rosout=False,
            start_parameter_services=False,
        )
        if self.executor is not None:
            self.executor.add_node(run_node)
        return run_node

    def _destroy_run_node(self, run_node) -> None:
        try:
            if self.executor is not None:
                self.executor.remove_node(run_node)
            run_node.destroy_node()
        except Exception as e:
            self.get_logger().error(f"Error destroying run node: {e}")

    def _instantiate_for_run(self, entry, run_node, invoker, publish_feedback):
        """Fresh, fully wired instance for one run; caller owns disposal."""

        def wire(skill_class):
            skill = skill_class(self.get_logger())
            skill.node = run_node
            self.robot_state.inject_required_interfaces(skill)
            skill.set_feedback_callback(publish_feedback)
            skill.skills = invoker
            return skill

        skill = wire(entry.skill_class)
        skill.source = entry.source
        skill.wire_subskills(wire)
        return skill

    def _dispose_run_instance(self, skill) -> None:
        try:
            skill.shutdown()
        except (Exception, SkillCancelled) as e:
            # SkillCancelled is a BaseException; must not escape the caller's finally.
            self.get_logger().error(f"Error shutting down {type(skill).__name__} run instance: {e}")

    def _execute_code_skill(self, goal_handle, skill_type, inputs, entry):
        def _publish_feedback(update_message: str, image_b64: str | None = None):
            feedback_msg = ExecuteSkill.Feedback()
            feedback_msg.feedback = update_message
            feedback_msg.image_b64 = image_b64 or ""
            goal_handle.publish_feedback(feedback_msg)
            self.get_logger().debug(f"Published feedback for '{skill_type}': {update_message}")

        run_node = None
        try:
            run_node = self._create_run_node()
            invoker = SkillInvoker(self, goal_handle, _publish_feedback, run_node)
            skill = self._instantiate_for_run(entry, run_node, invoker, _publish_feedback)
        except Exception as e:
            if run_node is not None:
                self._destroy_run_node(run_node)
            self.get_logger().error(f"Error constructing skill '{skill_type}': {e}")
            return self._abort_result(goal_handle, skill_type, f"Skill construction failed: {e}")

        with self._skill_execution_lock:
            self._active_code_skill = skill
            cancel_raced_start = self._pending_cancel_goal is goal_handle
        if cancel_raced_start:
            skill.cancel()
        try:
            self._publish_initial_feedback(goal_handle)
            self.robot_state.start_subscriptions()
            output = self._run_code_skill_body(skill, entry, skill_type, inputs, goal_handle)
            self.get_logger().info(f"Skill '{skill_type}' {output.status.value}: {output.message}")
            finalize, success = _FINALIZE[output.status]
            getattr(goal_handle, finalize)()
            return ExecuteSkill.Result(
                success=success,
                message=output.message,
                skill_type=skill_type,
                success_type=output.status.value,
                image_b64=base64.b64encode(output.image).decode() if output.image else "",
            )
        except Exception as e:
            self.get_logger().error(f"Error executing skill: {str(e)}")
            return self._abort_result(goal_handle, skill_type, str(e))
        finally:
            with self._skill_execution_lock:
                self._active_code_skill = None
            # Motion never outlives a run; dispose while interfaces are live —
            # teardowns may command hardware.
            skill._halt_interfaces()
            self._dispose_run_instance(skill)
            self.robot_state.stop_subscriptions()
            self._destroy_run_node(run_node)

    def _run_code_skill_body(self, skill, entry, skill_type, inputs, goal_handle) -> SkillOutput:
        """Prepare robot state and run execute(); returns the SkillOutput.

        Binds this skill's cancel latch as the process-wide run latch for the
        duration (save/restore, so a chained child rebinds and its parent's
        latch comes back): interface helpers block on it via cancellable_sleep
        and unwind on cancel without any plumbing from the skill.
        """
        previous_run_cancel = swap_run_cancel(skill._cancel_latch())
        try:
            return self._run_code_skill_prepared(skill, entry, skill_type, inputs, goal_handle)
        finally:
            swap_run_cancel(previous_run_cancel)

    def _run_code_skill_prepared(self, skill, entry, skill_type, inputs, goal_handle) -> SkillOutput:
        skill._begin_run(goal_handle)
        if skill.cancelled:
            self.get_logger().info(f"Skill '{skill_type}' cancelled before it started")
            return SkillOutput("Skill cancelled before it started", status=SkillResult.CANCELLED)

        missing = skill.missing_required_interfaces()
        if missing:
            names = "/".join(m.capitalize() for m in missing)
            plural = "s" if len(missing) > 1 else ""
            return SkillOutput(f"{names} interface{plural} not available", status=SkillResult.FAILURE)

        declared_states = skill.declared_robot_state_types()
        camera_feeds = {
            feed
            for state_type, feed in (
                (RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64, "main"),
                (RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64, "wrist"),
                (RobotStateType.LAST_DEPTH_IMAGE, "depth"),
            )
            if state_type in declared_states
        }
        began_continuous_updates = False
        try:
            if camera_feeds:
                self._camera_node.start(camera_feeds)
            self.robot_state.wait_for_required_states(skill)
            if skill.cancelled:
                return SkillOutput("Skill cancelled before it started", status=SkillResult.CANCELLED)
            missing = skill.missing_required_robot_states()
            if missing:
                return SkillOutput(f"No data from the {' / '.join(missing)}", status=SkillResult.FAILURE)
            if declared_states:
                self.robot_state.begin_continuous_updates(skill)
                began_continuous_updates = True
                self.get_logger().info(f"Started continuous state updates for '{skill_type}' at 50Hz")
            return normalize_skill_result(
                skill.execute(**_coerce_numeric_inputs(entry, inputs)), skill.name, logger=skill.logger
            )
        except SkillCancelled as e:
            return SkillOutput(str(e) or "Skill cancelled", status=SkillResult.CANCELLED)
        except SkillFailed as e:
            return SkillOutput(str(e) or "Skill failed", status=SkillResult.FAILURE)
        finally:
            if began_continuous_updates:
                self.robot_state.end_continuous_updates()
            if camera_feeds:
                self._camera_node.stop(camera_feeds)

    def _execute_physical_skill(self, goal_handle, skill_type, physical_data):
        self.get_logger().info(f"Delegating physical skill '{skill_type}' to behavior_server")
        self.robot_state.start_subscriptions()
        try:
            output = self._run_physical_skill(goal_handle, skill_type, physical_data)
            finalize, success = _FINALIZE[output.status]
            getattr(goal_handle, finalize)()
            return ExecuteSkill.Result(
                success=success, message=output.message, skill_type=skill_type, success_type=output.status.value
            )
        finally:
            self.robot_state.stop_subscriptions()

    def _run_physical_skill(self, goal_handle, skill_type, physical_data) -> SkillOutput:
        """Delegate to behavior_server; returns the SkillOutput."""
        metadata = physical_data.metadata
        self._begin_behavior_goal(goal_handle)
        try:
            self._publish_initial_feedback(goal_handle)
            if not self._behavior_client.wait_for_server(timeout_sec=5.0):
                self.get_logger().error("Behavior server not available!")
                return SkillOutput("Behavior server not available", status=SkillResult.FAILURE)

            behavior_goal = ExecuteBehavior.Goal()
            behavior_goal.skill_dir = physical_data.directory
            behavior_goal.behavior_config = json.dumps(metadata)

            self.get_logger().info(f"Sending behavior goal to behavior_server: {skill_type}")
            send_goal_future = self._behavior_client.send_goal_async(behavior_goal)

            goal_ready = self._wait_for_future(send_goal_future, timeout_sec=10.0) == "done"

            if not goal_ready:
                self.get_logger().error("Timeout waiting for behavior goal acceptance")
                self._cancel_behavior_goal_when_ready(send_goal_future, skill_type)
                return SkillOutput("Timeout waiting for behavior goal acceptance", status=SkillResult.FAILURE)

            behavior_goal_handle = send_goal_future.result()
            if behavior_goal_handle is None or not behavior_goal_handle.accepted:
                self.get_logger().error("Behavior goal rejected by behavior_server")
                return SkillOutput("Behavior goal rejected by behavior_server", status=SkillResult.FAILURE)

            self._attach_behavior_goal(goal_handle, behavior_goal_handle, skill_type)
            self.get_logger().info("Behavior goal accepted, waiting for result...")

            result_future = behavior_goal_handle.get_result_async()
            result_wait_state = self._wait_for_future(result_future, server_ready_check=self._behavior_server_ready)

            if result_wait_state == "server_unavailable":
                self.get_logger().error(f"Behavior server became unavailable while running '{skill_type}'")
                return SkillOutput(
                    "Behavior server became unavailable while waiting for result", status=SkillResult.FAILURE
                )

            behavior_result = result_future.result().result
            if behavior_result.success:
                self.get_logger().info(f"Physical skill '{skill_type}' succeeded: {behavior_result.message}")
                return SkillOutput(behavior_result.message)
            if "cancel" in behavior_result.message.lower():
                self.get_logger().info(f"Physical skill '{skill_type}' cancelled: {behavior_result.message}")
                return SkillOutput(behavior_result.message, status=SkillResult.CANCELLED)
            self.get_logger().error(f"Physical skill '{skill_type}' failed: {behavior_result.message}")
            return SkillOutput(behavior_result.message, status=SkillResult.FAILURE)
        except Exception as e:
            self.get_logger().error(f"Unexpected error executing physical skill '{skill_type}': {e}")
            return SkillOutput(f"Unexpected error executing physical skill: {e}", status=SkillResult.FAILURE)
        finally:
            self._end_behavior_goal(goal_handle)

    def _skill_goal_cancel_requested(self, goal_handle) -> bool:
        try:
            return bool(goal_handle.is_cancel_requested)
        except Exception:
            return False

    def _cancelling_teardown_in_progress(self) -> bool:
        """True when the running skill has a cancel in flight."""
        handle = self._active_goal_handle
        if handle is None:
            return False
        return self._pending_cancel_goal is handle or self._skill_goal_cancel_requested(handle)

    def _claim_skill_slot(self, goal_handle) -> bool:
        """Claim the one-skill-at-a-time slot; False if another skill kept it."""
        with self._skill_free:
            if self._skill_running:
                self._await_cancelling_teardown()
            if self._skill_running:
                return False
            self._skill_running = True
            self._active_goal_handle = goal_handle
            return True

    def _await_cancelling_teardown(self):
        """Wait for a cancelling skill to release the slot. Call holding _skill_free."""
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
            self._pending_cancel_goal = None
            self._skill_free.notify_all()

    def _begin_behavior_goal(self, skill_goal_handle) -> None:
        """Claim the behavior slot for this skill goal, before the goal is sent."""
        with self._behavior_lock:
            self._behavior_owner = skill_goal_handle
            self._behavior_handle = None
            self._behavior_cancel_requested = False
            self._behavior_cancel_sent = False
        with self._skill_execution_lock:
            cancel_raced_start = self._pending_cancel_goal is skill_goal_handle
        if cancel_raced_start:
            self._request_behavior_goal_cancel(skill_goal_handle, skill_goal_handle.request.skill_type)

    def _attach_behavior_goal(self, skill_goal_handle, behavior_goal_handle, skill_type: str) -> None:
        """Record the accepted behavior goal; re-dispatch a cancel that raced it."""
        with self._behavior_lock:
            if self._behavior_owner is not skill_goal_handle:
                return
            self._behavior_handle = behavior_goal_handle
            cancel_requested = self._behavior_cancel_requested
        if cancel_requested or self._skill_goal_cancel_requested(skill_goal_handle):
            self.get_logger().info(f"Cancel was already requested for physical skill '{skill_type}'")
            self._request_behavior_goal_cancel(skill_goal_handle, skill_type)

    def _end_behavior_goal(self, skill_goal_handle) -> None:
        with self._behavior_lock:
            if self._behavior_owner is skill_goal_handle:
                self._behavior_owner = None
                self._behavior_handle = None
                self._behavior_cancel_requested = False
                self._behavior_cancel_sent = False

    def _request_behavior_goal_cancel(self, skill_goal_handle, skill_type: str) -> None:
        with self._behavior_lock:
            if self._behavior_owner is not skill_goal_handle:
                return
            self._behavior_cancel_requested = True
            behavior_goal_handle = self._behavior_handle
            if behavior_goal_handle is None or self._behavior_cancel_sent:
                return
            self._behavior_cancel_sent = True
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

    def _submit_cli_skill(self, task):
        # One thread per task; the skill slot already enforces one-at-a-time.
        goal_handle = SkillCliGoalHandle(task)
        task.set_cancel_handler(lambda: self.cancel_callback(goal_handle))
        threading.Thread(target=self._run_cli_skill, args=(task, goal_handle), daemon=True).start()

    def _run_cli_skill(self, task, goal_handle):
        task.mark_started()
        if task.cancel_event.is_set():
            task.set_error("Skill execution was cancelled before start")
            return
        try:
            result = self.execute_callback(goal_handle)
        except Exception as e:
            self.get_logger().error(f"Unexpected error executing CLI skill '{task.skill_type}': {e}")
            task.set_error(f"Skill execution failed: {e}")
            return
        if result is None:
            task.set_error("Skill execution returned no result")
        else:
            task.set_result(result)

    def _behavior_server_ready(self) -> bool:
        try:
            return bool(self._behavior_client.server_is_ready())
        except Exception as e:
            self.get_logger().error(f"Could not check behavior_server readiness: {e}")
            return False

    def _wait_for_future(self, future, timeout_sec=None, server_ready_check=None):
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

    def destroy(self):
        self.catalog.stop_watcher()
        if hasattr(self, "_skill_cli_bridge"):
            self._skill_cli_bridge.stop()
        self.manipulation.shutdown()
        self._camera_node.shutdown()
        self._action_server.destroy()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    action_server = SkillsActionServer()
    # Dedicated executor — never share rclpy's global one with Nav2 (SIGABRT risk).
    # Floor threads well above CPU count so blocked callbacks can't deadlock futures.
    executor = MultiThreadedExecutor(num_threads=max(8, (os.cpu_count() or 4) + 4))
    executor.add_node(action_server)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception:
        # Log and tear down cleanly — live zenoh entities on exit can SIGABRT.
        action_server.get_logger().fatal(f"Executor spin crashed:\n{traceback.format_exc()}")
    action_server.destroy()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == "__main__":
    main()
