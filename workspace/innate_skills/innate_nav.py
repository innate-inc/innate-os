#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Innate Nav Skill — sends a natural-language navigation instruction to the
Innate navigation policy and drives until the robot arrives (or is canceled).

Uses the ``/innate_nav/navigate`` ROS 2 action server exposed by the
``innate_nav`` node. Sibling of navigate_with_vision: same shape, different
policy. UniNavid returns discrete actions from a cloud service; this runs our
own waypoint policy and tracks metric plans continuously.
"""

import threading

from action_msgs.msg import GoalStatus
from innate_cloud_msgs.action import NavigateInstruction
from rclpy.action import ActionClient

from brain_client.skills.types import Skill, SkillResult


class InnateNav(Skill):
    """Drive with the Innate navigation policy from a text instruction."""

    def __init__(self, logger):
        super().__init__(logger)
        self._action_client: ActionClient | None = None
        self._goal_handle = None
        self._cancel_requested = threading.Event()

    # ── Skill interface ───────────────────────────────────────────────────────

    @property
    def name(self):
        return "innate_nav"

    def guidelines(self):
        return (
            "Use to navigate with the Innate navigation policy from a "
            "natural-language instruction (e.g. 'drive past the sofa into the "
            "dining area'). The robot follows the instruction visually and stops "
            "on its own when it judges it has arrived. "
            "Requires param 'instruction' (str). "
            "Optional param 'server' (str) points this run at a specific policy "
            "server — '10.0.0.4', '10.0.0.4:8900' or a full URL; empty uses the "
            "one the robot is configured with. "
            "Examples: 'go through the doorway and stop by the table', "
            "'find the nearest sofa and stop next to it'."
        )

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, instruction: str, server: str = ""):
        """Send *instruction* to the navigation policy and block until done.

        The policy runs on a server, not on the robot. *server* addresses one
        for this run — a bare IP, an IP with a port, or a full URL — and empty
        keeps whatever the ``innate_nav`` node was configured with.
        """
        if not self.node:
            msg = "Navigation skill has no ROS node and cannot execute."
            self.logger.error(msg)
            self._send_feedback(msg)
            return msg, SkillResult.FAILURE

        self._cancel_requested.clear()

        if self._action_client is None:
            self._action_client = ActionClient(self.node, NavigateInstruction, "/innate_nav/navigate")

        where = f" via {server}" if server else ""
        self.logger.info(f"[InnateNav] Instruction: {instruction!r}{where}")
        self._send_feedback(f"Navigating: {instruction}{where}")

        if not self._action_client.wait_for_server(timeout_sec=10.0):
            msg = "Navigation policy is not available. Is the innate_nav node running?"
            self.logger.error(msg)
            self._send_feedback(msg)
            return msg, SkillResult.FAILURE

        goal_msg = NavigateInstruction.Goal()
        goal_msg.instruction = instruction
        goal_msg.server = server
        goal_future = self._action_client.send_goal_async(goal_msg)

        if not self._wait_for_future(goal_future, timeout_sec=10.0):
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

        self._send_feedback("Navigation started …")
        result_future = self._goal_handle.get_result_async()

        # Wait WITHOUT re-spinning self.node — the skills_server's dedicated
        # executor already services this future's callbacks. Registering the
        # done-callback once and polling an Event in short slices lets us react
        # to a cancel without piling up closures on a long goal. (Calling
        # spin_until_future_complete here would race that executor and corrupt
        # the shared wait set.)
        result_ready = threading.Event()
        result_future.add_done_callback(lambda _future: result_ready.set())
        cancel_sent = False
        while not result_ready.wait(timeout=0.25):
            if self._cancel_requested.is_set() and not cancel_sent:
                self.logger.info("Cancel requested — forwarding to the action server")
                self._goal_handle.cancel_goal_async()
                cancel_sent = True

        # No give-up branch: returning while the goal is still active abandons a
        # driving robot. The node brakes in its own teardown and only then sends
        # the result, so the terminal state is the one signal that the base has
        # actually stopped -- and releasing the skill slot before it arrives
        # leaves the next Stop with nothing to cancel ("No skill is running").
        response = result_future.result()
        status, result = response.status, response.result
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

        msg = result.message or "Navigation ended unexpectedly."
        self.logger.warning(msg)
        self._send_feedback(msg)
        return msg, SkillResult.FAILURE

    # ── Future waiting (no node re-spin) ──────────────────────────────────────

    def _wait_for_future(self, future, timeout_sec=None):
        if future.done():
            return True
        done_event = threading.Event()
        future.add_done_callback(lambda _future: done_event.set())
        return done_event.wait(timeout=timeout_sec)

    # ── Cancellation ──────────────────────────────────────────────────────────

    def cancel(self):
        """Request cancellation of the running navigation goal."""
        self.logger.info("[InnateNav] Cancel requested")
        self._cancel_requested.set()
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
        return "Cancellation requested for innate_nav"
