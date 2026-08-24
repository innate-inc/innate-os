# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Brain lifecycle: activate / deactivate / reset + directive switching.

The state machine that turns the local agent loop on and off. With the brain
in-process there is no registration handshake or connection dance: activating
means starting the sensors and the agent's turn timer, deactivating means
stopping them and interrupting whatever skill is running.
"""

from __future__ import annotations

import json

from std_msgs.msg import String


class BrainLifecycle:
    def __init__(
        self,
        node,
        state,
        *,
        brain,
        camera,
        pose_tracker,
        runner,
        gaze,
        chat,
        rest_pose,
        active_inputs_pub,
        stop_robot,
        publish_status,
    ):
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._brain = brain
        self._camera = camera
        self._pose = pose_tracker
        self._runner = runner
        self._gaze = gaze
        self._chat = chat
        self._rest_pose = rest_pose
        self._active_inputs_pub = active_inputs_pub
        self._stop_robot = stop_robot
        self._publish_status = publish_status

    # --- activate / deactivate ---
    def activate_brain(self) -> None:
        if self._state.current_directive is None:
            self._logger.warn("[BrainClient] No directive configured. Brain remains inactive.")
            return
        self._logger.info(f"[BrainClient] Activating brain with directive: {self._state.current_directive.id}")
        self._state.is_brain_active = True
        self._camera.start()
        self._pose.start()
        self._gaze.update()
        if not self._brain.start():
            # The loop refused to spawn (the previous one is stuck unwinding).
            # Roll the activation back: a status claiming active with no loop
            # behind it would stick — re-activating is a no-op while
            # is_brain_active is set. brain.start() already told the user.
            self._state.is_brain_active = False
            self._camera.stop()
            self._pose.stop()
            self._gaze.stop()
            self._publish_status()
            return
        self.activate_directive_inputs()
        self._rest_pose.fold()
        self._publish_status()
        # Announce in the shared chat so EVERY client sees it, not just the
        # device whose button was pressed (clients don't echo locally).
        self._chat.emit_system(f"{self._directive_label()} started.")

    def deactivate_brain(self) -> None:
        self._logger.info("[BrainClient] Deactivating brain...")
        self._state.is_brain_active = False
        self._brain.stop()
        self._camera.stop()
        self._pose.stop()
        self._runner.interrupt_for_deactivation()
        self._gaze.stop()
        self._active_inputs_pub.publish(String(data=json.dumps({"inputs": []})))
        self._stop_robot()
        self._publish_status()
        self._chat.emit_system(f"{self._directive_label()} stopped.")

    # --- reset ---
    def perform_brain_reset(self, memory_state: str) -> None:
        self._logger.info(f"[BrainClient] Resetting brain (memory_state={memory_state})")
        # Reset first: it joins any in-flight turn, so a decision from the old
        # conversation can't start a skill under abort_running's feet. (The
        # restarted loop's first turn may still glimpse the aborting skill for
        # one observation — harmless, its tools collapse to stop/wait once.)
        self._brain.reset()
        self._runner.abort_running()
        self._chat.clear()
        self._chat.emit_system("Brain has been reset.")

    # --- directive switching ---
    def set_directive(self, name: str) -> None:
        name = name.strip()
        self._logger.info(f"Received directive change request: {name}")
        if name not in self._state.directives:
            available = list(self._state.directives.keys())
            self._chat.emit_system(f"Error: Unknown directive '{name}'. Available directives: {available}")
            self._logger.error(f"Unknown directive: {name}")
            return
        self._state.current_directive = self._state.directives[name]
        self._state.active_skill_ids = list(self._state.current_directive.skill_ids())
        self._logger.info(f"Activated directive: {name}")
        self._chat.clear()
        self._brain.reset()
        self.activate_directive_inputs()
        self._gaze.update()
        self._publish_status()
        # Only announce a LIVE switch; arming a directive while idle is followed
        # by activate_brain's "… started." message, which carries the name.
        if self._state.is_brain_active:
            self._chat.emit_system(f"Directive updated to: {self._directive_label()}")

    # --- inputs ---
    def activate_directive_inputs(self) -> None:
        directive = self._state.current_directive
        if not directive or not self._state.is_brain_active:
            return
        required_inputs = directive.input_names()
        self._active_inputs_pub.publish(String(data=json.dumps({"inputs": required_inputs})))
        if required_inputs:
            self._logger.info(f"🔌 Activated inputs for directive '{directive.id}': {required_inputs}")

    def _directive_label(self) -> str:
        directive = self._state.current_directive
        if directive is None:
            return "Agent"
        return getattr(directive, "display_name", None) or directive.id
