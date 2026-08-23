# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Primitive execution lifecycle over the ``execute_skill`` action.

Owns the action client and the execution state — the current goal handle and
the running primitive. Terminal results and feedback are reported to the brain
through the ``on_event`` / ``on_feedback`` callbacks so they land in the agent's
next turn.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Callable

from brain_messages.action import ExecuteSkill
from rclpy.action import ActionClient
from std_srvs.srv import Trigger

from brain_client.core.state import RunningSkill
from brain_client.skills.lifecycle import PRIMITIVE_LIFECYCLE_STATUSES, decode_substep_feedback
from brain_client.skills.types import SkillResult
from brain_client.transport.chat import Sender


class PrimitiveRunner:
    def __init__(self, node, chat, state, *, stop_robot, on_task_finished):
        self._node = node
        self._logger = node.get_logger()
        self._chat = chat
        self._state = state
        self._stop_robot = stop_robot
        self._on_task_finished = on_task_finished

        # Bound late by the node (mutual cycle: the brain needs the runner too).
        self.on_event = lambda status, skill_name, detail=None, image=None: None
        self.on_feedback = lambda skill_name, feedback, image=None: None

        self.action_client = ActionClient(node, ExecuteSkill, "execute_skill")
        # Cancels runs this client doesn't own (manual webapp/CLI runs): the
        # execute_skill action only lets the goal's sender cancel.
        self._cancel_skill_client = node.create_client(Trigger, "/brain/cancel_skill")
        self._goal_handle = None
        self._system_claim: RunningSkill | None = None
        self._system_finished: Callable[[], None] | None = None
        # Bumped whenever the brain disowns its goal (reset/deactivation). Late
        # callbacks from a disowned goal compare against it and stand down, so
        # they can never clear a newer run's state or feed a fresh context.
        self._generation = 0
        # Serializes every primitive_running transition (and the generation
        # reads/bumps they pair with) across the two threads that make them:
        # the agent loop (start_task) and the ROS executor (manual-event
        # mirror, deactivation/reset, action callbacks).
        self._slot_lock = threading.Lock()

    # --- public API ---
    def start_task(self, skill_id: str, primitive_id: str | None, inputs: dict) -> None:
        """Claim the skill slot and send a goal for ``skill_id``.

        The claim is atomic: a manual run mirrored from the executor thread
        between the agent's availability check and this call keeps the slot,
        and this start reports failure instead of clobbering it. The generation
        is captured with the claim — before the blocking server wait — so a
        reset that disowns the goal mid-send orphans it and its late callbacks
        stand down.

        The local /brain/skill_status_update echo is the skills server's job (it
        publishes for every goal it runs) — announcing it here too would double
        the "running" entry in the chat.
        """
        skill_name = self._state.registry.name_for(skill_id)
        claim = RunningSkill(primitive_name=skill_name, skill_id=skill_id, primitive_id=primitive_id)
        generation = 0
        with self._slot_lock:
            occupant = self._state.primitive_running
            if occupant is None:
                # Claimed before the goal is sent: the response and result
                # callbacks fire on the ROS thread and must find the state they clear.
                self._state.primitive_running = claim
                generation = self._generation
        if occupant is not None:
            self.report_start_failure(
                primitive_name=skill_name,
                primitive_id=primitive_id,
                skill_id=skill_id,
                reason=f"another skill ({occupant.primitive_name}) is already running",
            )
            return
        if not self._send_goal(skill_id, inputs, generation):
            self._release(claim)
            self.report_start_failure(
                primitive_name=skill_name,
                primitive_id=primitive_id,
                skill_id=skill_id,
                reason="Skill execution server unavailable — the skill never started.",
            )

    def start_system_task(
        self,
        skill_id: str,
        primitive_id: str,
        inputs: dict,
        on_finished: Callable[[], None],
    ) -> bool:
        """Atomically start an internal cue without feeding its result to the model."""
        skill_name = self._state.registry.name_for(skill_id)
        claim = RunningSkill(primitive_name=skill_name, skill_id=skill_id, primitive_id=primitive_id)
        with self._slot_lock:
            if self._state.primitive_running is not None:
                return False
            self._state.primitive_running = claim
            self._system_claim = claim
            self._system_finished = on_finished
            generation = self._generation
        if self._send_goal(skill_id, inputs, generation, silent=True):
            return True
        with self._slot_lock:
            self._release_system_locked(claim)
            if self._state.primitive_running is claim:
                self._state.primitive_running = None
        return False

    def mirror_manual_event(self, status: str, *, primitive_name, primitive_id, skill_id) -> bool:
        """Mirror a manual (webapp/CLI) run into the skill slot.

        Keeps the brain honoring one-skill-at-a-time for runs it didn't start:
        a mirrored "running" collapses the next turn's tools to stop/wait, and
        the run's own terminal event clears it. Runs under the slot lock so a
        concurrent brain claim can neither be clobbered by the mirror nor
        cleared by a manual run's terminal event. Returns whether this event
        changed the slot and therefore owns the gaze pause/resume transition.
        """
        with self._slot_lock:
            if status == "running":
                if self._state.primitive_running is None:
                    self._state.primitive_running = RunningSkill(
                        primitive_name=primitive_name, skill_id=skill_id, primitive_id=primitive_id, manual=True
                    )
                    return True
            elif status in ("completed", "failed", "interrupted"):
                running = self._state.primitive_running
                if running is None or not running.manual:
                    return False
                # Match ids when both sides carry one: a delayed terminal from
                # an older manual run must not clear a newer run's mirror. A
                # publisher that omits ids falls back to any-manual — requiring
                # a match there would wedge the slot shut forever.
                if primitive_id and running.primitive_id and primitive_id != running.primitive_id:
                    return False
                self._state.primitive_running = None
                return True
        return False

    def _release(self, claim: RunningSkill) -> None:
        """Free the slot iff it still holds ``claim`` (identity, not equality)."""
        with self._slot_lock:
            if self._state.primitive_running is claim:
                self._state.primitive_running = None

    def report_start_failure(self, *, primitive_name, primitive_id, reason, skill_id=None) -> None:
        """Tell the brain and the app that a task never started (no goal exists)."""
        self._logger.error(f"Primitive '{primitive_name}' failed to start: {reason}")
        self.on_event("failed", primitive_name, reason)
        self._chat.publish_task_status(
            primitive_name=primitive_name,
            primitive_id=primitive_id,
            status="failed",
            skill_id=skill_id,
            reason=reason,
        )
        if self._state.primitive_running is None and self._goal_handle is None:
            self._on_task_finished()

    @property
    def has_active_goal(self) -> bool:
        return self._goal_handle is not None

    def cancel_active_goal(self, on_done=None):
        """Request cancellation of the active goal (if any). Returns the future."""
        if self._goal_handle is None:
            return None
        future = self._goal_handle.cancel_goal_async()
        future.add_done_callback(on_done or self._on_cancel_response)
        return future

    def cancel_external(self) -> bool:
        """Ask the skills server to cancel a run this client didn't start.

        Fire-and-forget: the run's terminal event arrives like any other
        manual skill event. Returns False if the server is unreachable.
        """
        if not self._cancel_skill_client.service_is_ready():
            self._logger.error("Cannot cancel external skill run: /brain/cancel_skill unavailable")
            return False
        self._cancel_skill_client.call_async(Trigger.Request())
        return True

    def abort_running(self) -> None:
        """Stop the brain's primitive without announcing an interruption (used on reset)."""
        with self._slot_lock:
            running = self._state.primitive_running
        if running is not None and not running.manual:
            self._stop_robot()
        self.interrupt_for_deactivation()

    def interrupt_for_deactivation(self) -> None:
        """Cancel the running primitive on deactivate.

        Skips the local /brain/skill_status_update echo — the action server
        publishes "interrupted" itself once the cancellation actually lands.

        A manual (webapp/CLI) run is not the brain's to stop: it keeps running
        on the skills server, so its mirrored state is kept too — a reactivated
        brain still honors one-skill-at-a-time, and the run's own terminal
        event clears it (that handler is always on).

        Bumping the generation disowns the brain's goal: late callbacks compare
        against it and stand down. A goal whose handle hasn't resolved yet
        (sent moments before this) is cancelled when it does —
        _on_goal_response sees the stale generation and cancels on arrival.
        """
        with self._slot_lock:
            running = self._state.primitive_running
            if running is not None and running.manual:
                return
            self._generation += 1
            handle, self._goal_handle = self._goal_handle, None
            self._state.primitive_running = None
            self._system_claim = None
            self._system_finished = None
        if handle:
            handle.cancel_goal_async()  # fire-and-forget

    # --- action plumbing ---
    def _send_goal(self, task_type: str, inputs: dict, generation: int, *, silent: bool = False) -> bool:
        """Dispatch the goal; returns False if the action server is unavailable.

        ``generation`` is the claim-time generation: wait_for_server blocks up
        to a second, and a disown landing inside that window must orphan this
        goal rather than adopt it.
        """
        goal_msg = ExecuteSkill.Goal()
        goal_msg.skill_type = task_type
        goal_msg.inputs = json.dumps(inputs if inputs is not None else {})
        self._logger.info(f"Sending goal for skill: {task_type} with inputs: {goal_msg.inputs}")
        if not self.action_client.wait_for_server(timeout_sec=1.0):
            self._logger.error("Primitive execution action server not available!")
            return False
        future = self.action_client.send_goal_async(
            goal_msg, feedback_callback=lambda msg: self._on_feedback_msg(msg, generation, silent=silent)
        )
        future.add_done_callback(lambda f: self._on_goal_response(f, generation))
        return True

    def _on_feedback_msg(self, feedback_wrapper, generation: int, *, silent: bool = False) -> None:
        if generation != self._generation:
            return  # feedback from a disowned goal
        if silent:
            return
        try:
            feedback_text = feedback_wrapper.feedback.feedback
            substep = decode_substep_feedback(feedback_text)
            if substep is not None:
                self._handle_substep(substep)
                return
            self._logger.info(f"Received primitive feedback: {feedback_text}")
            image = None
            if feedback_wrapper.feedback.image_b64:
                image = base64.b64decode(feedback_wrapper.feedback.image_b64)
            running = self._state.primitive_running
            self.on_feedback(running.primitive_name if running else "unknown", feedback_text, image)
        except Exception as e:
            self._logger.error(f"Error in feedback handler: {e}")

    def _handle_substep(self, substep: dict) -> None:
        """Turn a chained child's piggybacked event into its own step in the app.

        Forwarded to the app only, deliberately NOT to the brain: the agent runs
        one primitive at a time and would read a child finishing as the parent
        finishing.
        """
        event = substep.get("event")
        if event not in PRIMITIVE_LIFECYCLE_STATUSES:
            self._logger.warn(f"Unknown substep event: {event}")
            return
        self._chat.publish_task_status(
            primitive_name=substep.get("name", ""),
            primitive_id=substep.get("primitive_id"),
            status=event,
            skill_id=substep.get("skill_id"),
            reason=substep.get("reason"),
        )
        output = substep.get("output")
        if event == "completed" and output and output.strip():
            self._chat.emit(Sender.SKILL_OUTPUT, output, speak=False)

    def _on_goal_response(self, future, generation: int) -> None:
        # A failure here must still release the claimed slot, or it wedges shut.
        try:
            goal_handle = future.result()
        except Exception as error:
            self._logger.error(f"Goal response failed: {error!r}")
            goal_handle = None
        accepted = goal_handle is not None and goal_handle.accepted
        running = None
        system_finished = None
        with self._slot_lock:
            stale = generation != self._generation
            if not stale:
                if accepted:
                    self._goal_handle = goal_handle
                else:
                    running, self._state.primitive_running = self._state.primitive_running, None
                    self._goal_handle = None
                    system_finished = self._release_system_locked(running)
        if stale:
            # The brain disowned this goal while its response was in flight:
            # cancel it now that a handle finally exists, touch nothing else.
            if accepted:
                goal_handle.cancel_goal_async()
            return
        if not accepted:
            reason = "Goal rejected by action server" if goal_handle is not None else "No response from action server"
            self._logger.info(f"Primitive execution goal not accepted: {reason}")
            if running is not None:
                if system_finished is None:
                    self._chat.publish_task_status(
                        primitive_name=running.primitive_name,
                        primitive_id=running.primitive_id,
                        status="failed",
                        skill_id=running.skill_id,
                        reason=reason,
                    )
                    self.on_event("failed", running.primitive_name, reason)
            self._on_task_finished()
            if system_finished is not None:
                system_finished()
            return
        self._logger.info("Primitive execution goal accepted.")
        goal_handle.get_result_async().add_done_callback(lambda f: self._on_result(f, generation))

    def _on_cancel_response(self, future) -> None:
        cancel_response = future.result()
        self._logger.info("[BrainClient] Cancel response received.")
        if getattr(cancel_response, "goals_canceling", None):
            self._logger.info("Goal cancellation accepted.")
        else:
            self._logger.error("Goal cancellation rejected.")

    def _on_result(self, future, generation: int) -> None:
        # Same slot-release guarantee as _on_goal_response: a lost result must
        # not leave the claim in place.
        try:
            result = future.result().result
        except Exception as error:
            self._logger.error(f"Skill result lost: {error!r}")
            result = None
        if result is not None:
            status_color = "\033[92m" if result.success else "\033[91m"
            self._logger.info(
                f"{status_color}Primitive execution result: {result.success}, Type: {result.success_type}\033[0m"
            )
        running = None
        system_finished = None
        with self._slot_lock:
            stale = generation != self._generation
            if not stale:
                running, self._state.primitive_running = self._state.primitive_running, None
                self._goal_handle = None
                system_finished = self._release_system_locked(running)
        if stale:
            # A disowned goal ending: a newer run (or a fresh context) may own
            # the state and the event queue now — this result concerns neither.
            return
        self._stop_robot()
        if result is None:
            if running is not None and system_finished is None:
                self.on_event("failed", running.primitive_name, "the skill's result was lost")
            self._on_task_finished()
            if system_finished is not None:
                system_finished()
            return

        skill_id = result.skill_type
        primitive_name = self._state.registry.name_for(skill_id)
        if running is not None and running.skill_id != skill_id:
            self._logger.warn(f"Skill ID mismatch in result ({skill_id}) and running ({running.skill_id})")
        self._on_task_finished()
        if system_finished is not None:
            system_finished()
            return

        is_code = self._is_code_skill(skill_id)
        # The action server publishes the terminal /brain/skill_status_update itself
        # (for every goal, not just the agent's) — only the brain event is this
        # client's job.
        status, detail = self._classify_result(result, is_code)
        if status is not None:
            image = base64.b64decode(result.image_b64) if result.image_b64 else None
            self.on_event(status, primitive_name, detail, image=image)
        self._emit_skill_output(result, is_code)

    def _release_system_locked(self, running: RunningSkill | None) -> Callable[[], None] | None:
        if running is not self._system_claim:
            return None
        finished = self._system_finished
        self._system_claim = None
        self._system_finished = None
        return finished

    def _is_code_skill(self, skill_id: str) -> bool:
        meta = self._state.registry.primitives.get(skill_id)
        return meta is not None and meta.get("type") == "code"

    def _emit_skill_output(self, result, is_code: bool) -> None:
        """Surface a successful code skill's output in the chat (never spoken)."""
        if is_code and result.success and result.success_type == SkillResult.SUCCESS.value and result.message.strip():
            self._chat.emit(Sender.SKILL_OUTPUT, result.message, speak=False)

    def _classify_result(self, result, is_code: bool) -> tuple[str | None, str | None]:
        """Map an action result to the brain-facing (status, detail) event."""
        if result.success and result.success_type == SkillResult.SUCCESS.value:
            output = result.message if is_code and result.message.strip() else None
            return "completed", output
        if result.success_type == SkillResult.CANCELLED.value:
            return "interrupted", None
        if not result.success or result.success_type == SkillResult.FAILURE.value:
            return "failed", result.message
        self._logger.error(
            f"Unknown primitive result combination: success={result.success}, type={result.success_type}"
        )
        return None, None
