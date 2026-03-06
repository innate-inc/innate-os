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
        self._last_feedback_action: int | None = None
        self._last_feedback_stops: int = 0

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
            "Some other examples of instructions are 'move to the nearest sofa and then stop', 'follow the human wearing black pants'."
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
            msg = "Navigation skill has no ROS node and cannot execute."
            self.logger.error(msg)
            self._send_feedback(msg)
            return msg, SkillResult.FAILURE

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
            msg = "Navigation server is not available. Please try again."
            self.logger.error(msg)
            self._send_feedback(msg)
            return msg, SkillResult.FAILURE

        # ── Send goal ─────────────────────────────────────────────────────────
        goal_msg = NavigateInstruction.Goal()
        goal_msg.instruction = instruction

        goal_future = self._action_client.send_goal_async(
            goal_msg, feedback_callback=self._on_feedback
        )

        try:
            rclpy.spin_until_future_complete(self.node, goal_future, timeout_sec=10.0)
        except Exception as exc:
            msg = f"Failed to send navigation goal: {exc}"
            self.logger.error(msg)
            self._send_feedback(msg)
            return msg, SkillResult.FAILURE

        if not goal_future.done():
            msg = "Navigation goal timed out waiting for acceptance."
            self.logger.error(msg)
            self._send_feedback(msg)
            return msg, SkillResult.FAILURE

        self._goal_handle = goal_future.result()
        if not self._goal_handle.accepted:
            msg = "Navigation goal was rejected."
            self.logger.info(msg)
            self._send_feedback(msg)
            return msg, SkillResult.FAILURE

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
            msg = "Navigation timed out."
            self.logger.error(msg)
            self._send_feedback(msg)
            self._goal_handle = None
            return msg, SkillResult.FAILURE

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
            self._send_feedback(msg)
            return msg, SkillResult.CANCELLED

        msg = result.message or f"Navigation ended unexpectedly."
        self.logger.warning(msg)
        self._send_feedback(msg)
        return msg, SkillResult.FAILURE

    # ── Feedback callback (called on the executor thread) ─────────────────────

    def _on_feedback(self, feedback_msg):
        """Relay action feedback to the brain as a human-readable string.

        Only sends when the action changes or every 5th consecutive stop
        to avoid flooding the brain logs.
        """
        fb = feedback_msg.feedback
        action_label = _ACTION_LABELS.get(fb.latest_action, str(fb.latest_action))
        text = (
            f"Action: {action_label} | "
            f"Consecutive stops: {fb.consecutive_stops}/{fb.max_consecutive_stops}"
        )
        self.logger.debug(f"[NavigateWithVision] feedback: {text}")

        action_changed = (
            fb.latest_action != self._last_feedback_action
            and fb.latest_action != 0  # don't report individual STOPs
        )
        stops_milestone = False  # no stop-count feedback
        self._last_feedback_action = fb.latest_action
        self._last_feedback_stops = fb.consecutive_stops

        if action_changed or stops_milestone:
            self._send_feedback(text)

    # ── Cancellation ──────────────────────────────────────────────────────────

    def cancel(self):
        """Request cancellation of the running navigation goal."""
        self.logger.info("[NavigateWithVision] Cancel requested")
        self._cancel_requested.set()
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
        return "Cancellation requested for navigate_with_vision"
