#!/usr/bin/env python3
"""
Navigate With Vision Skill — sends a natural-language navigation instruction
to the UniNavid cloud service and follows the returned action commands until
the goal is reached (or canceled).

Uses the ``navigate_instruction`` ROS 2 action server exposed by the
``innate_uninavid`` node.
"""

import threading

import rclpy
from action_msgs.msg import GoalStatus
from brain_client.skill_types import Skill, SkillResult
from innate_cloud_msgs.action import NavigateInstruction
from rclpy.action import ActionClient

# Human-readable labels for the integer action codes returned by the server.
_ACTION_LABELS = {
    0: "STOP",
    1: "FORWARD",
    2: "LEFT",
    3: "RIGHT",
}


class NavigateWithVision(Skill):
    """Send text navigation instructions to the UniNavid cloud service."""

    def __init__(self, logger):
        super().__init__(logger)
        self._action_client: ActionClient | None = None
        self._goal_handle = None
        self._cancel_requested = threading.Event()

    # ── Skill interface ───────────────────────────────────────────────────────

    @property
    def name(self):
        return "navigate_with_vision"

    def guidelines(self):
        return (
            "Use when you want the robot to navigate using camera vision and a "
            "natural-language instruction (e.g. 'walk to the red chair and stop'). "
            "The instruction is sent to the UniNavid cloud service which streams "
            "back movement commands until the goal is reached. "
            "Requires param 'instruction' (str)."
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, instruction: str):
        """Send *instruction* to UniNavid and block until the goal finishes.

        Args:
            instruction: A natural-language navigation command,
                         e.g. ``"walk to the red chair and stop"``.

        Returns:
            tuple: ``(result_message, SkillResult)``
        """
        if not self.node:
            self.logger.error(
                "NavigateWithVision skill has no ROS node — cannot execute."
            )
            return "Skill not initialised (no ROS node)", SkillResult.FAILURE

        self._cancel_requested.clear()

        # Lazily create the action client (same pattern as PhysicalSkill)
        if self._action_client is None:
            self._action_client = ActionClient(
                self.node, NavigateInstruction, "/navigate_instruction"
            )

        self.logger.info(f"[NavigateWithVision] Instruction: {instruction!r}")
        self._send_feedback(f"Sending instruction: {instruction}")

        # ── Wait for the action server ────────────────────────────────────────
        if not self._action_client.wait_for_server(timeout_sec=10.0):
            self.logger.error("navigate_instruction action server not available")
            return (
                "navigate_instruction action server not available",
                SkillResult.FAILURE,
            )

        # ── Send goal ─────────────────────────────────────────────────────────
        goal_msg = NavigateInstruction.Goal()
        goal_msg.instruction = instruction

        goal_future = self._action_client.send_goal_async(
            goal_msg, feedback_callback=self._on_feedback
        )

        try:
            rclpy.spin_until_future_complete(self.node, goal_future, timeout_sec=10.0)
        except Exception as exc:
            self.logger.error(f"Exception sending goal: {exc}")
            return f"Failed to send goal: {exc}", SkillResult.FAILURE

        if not goal_future.done():
            self.logger.error("Goal acceptance timed out")
            return "Goal acceptance timed out", SkillResult.FAILURE

        self._goal_handle = goal_future.result()
        if not self._goal_handle.accepted:
            self.logger.info("Goal rejected by action server")
            return "Goal rejected by action server", SkillResult.FAILURE

        self.logger.info("Goal accepted — waiting for result …")
        self._send_feedback("Navigation started, waiting for completion …")

        result_future = self._goal_handle.get_result_async()

        # Spin until the result arrives (or cancellation).
        # We use a short spin timeout so we can periodically check for cancel.
        while not result_future.done():
            if self._cancel_requested.is_set():
                self.logger.info("Cancel requested — forwarding to action server")
                self._goal_handle.cancel_goal_async()
                # Keep spinning until the server acknowledges the cancel
                try:
                    rclpy.spin_until_future_complete(
                        self.node, result_future, timeout_sec=10.0
                    )
                except Exception:
                    pass
                break
            try:
                rclpy.spin_until_future_complete(
                    self.node, result_future, timeout_sec=0.25
                )
            except Exception:
                pass

        if not result_future.done():
            self.logger.error("Result wait timed out after cancel")
            self._goal_handle = None
            return "Navigation timed out", SkillResult.FAILURE

        result_response = result_future.result()
        status = result_response.status
        result = result_response.result

        self._goal_handle = None

        if status == GoalStatus.STATUS_SUCCEEDED:
            msg = result.message or "Navigation completed"
            self.logger.info(f"Goal succeeded: {msg}")
            self._send_feedback(msg)
            return msg, SkillResult.SUCCESS

        if status in (GoalStatus.STATUS_CANCELED, GoalStatus.STATUS_ABORTED):
            msg = result.message or "Navigation canceled"
            self.logger.info(f"Goal canceled/aborted: {msg}")
            return msg, SkillResult.CANCELLED

        msg = result.message or f"Navigation ended with status {status}"
        self.logger.warning(msg)
        return msg, SkillResult.FAILURE

    # ── Feedback callback (called on the executor thread) ─────────────────────

    def _on_feedback(self, feedback_msg):
        """Relay action feedback to the brain as a human-readable string."""
        fb = feedback_msg.feedback
        action_label = _ACTION_LABELS.get(fb.latest_action, str(fb.latest_action))
        text = (
            f"Action: {action_label} | "
            f"Consecutive stops: {fb.consecutive_stops}/20"
        )
        self.logger.debug(f"[NavigateWithVision] feedback: {text}")
        self._send_feedback(text)

    # ── Cancellation ──────────────────────────────────────────────────────────

    def cancel(self):
        """Request cancellation of the running navigation goal."""
        self.logger.info("[NavigateWithVision] Cancel requested")
        self._cancel_requested.set()
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
        return "Cancellation requested for navigate_with_vision"
