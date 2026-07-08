# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Chat history, chat-out publishing, task-status publishing, and TTS.

Consolidates the ``{"sender", "text", "timestamp"}`` chat-entry dict and the
task-status payload that were copy-pasted across the old node. Owns the chat
history list so no other component needs to.
"""

from __future__ import annotations

import json
import time

from std_msgs.msg import String


class ChatManager:
    def __init__(self, logger, chat_out_pub, task_status_pub, tts_handler=None):
        self._logger = logger
        self._chat_out_pub = chat_out_pub
        self._task_status_pub = task_status_pub
        self._tts_handler = tts_handler
        self.history: list[dict] = []

    @staticmethod
    def entry(sender: str, text: str) -> dict:
        return {"sender": sender, "text": text, "timestamp": time.time()}

    def emit(self, sender: str, text: str, speak: bool | None = None) -> None:
        """Append a chat entry, publish it, and (for robot speech) speak it.

        ``speak`` defaults to True only for the ``"robot"`` sender, matching the
        old behaviour where thoughts/anticipation were published but not spoken.
        """
        chat_entry = self.entry(sender, text)
        self.history.append(chat_entry)
        self._logger.debug(f"chat_out: {chat_entry}")
        self._chat_out_pub.publish(String(data=json.dumps(chat_entry)))

        if speak is None:
            speak = sender == "robot"
        if speak and text and text.strip():
            self.speak(text)

    def emit_system(self, text: str) -> None:
        """Publish a system message (never spoken)."""
        self.emit("system", text, speak=False)

    def publish_task_status(
        self,
        primitive_name: str,
        primitive_id: str | None,
        status: str,
        skill_id: str | None = None,
        reason: str | None = None,
        inputs: dict | None = None,
    ) -> None:
        """Publish a local task-status update for the controller-app UI."""
        payload = {
            "primitive_name": primitive_name,
            "primitive_id": primitive_id,
            "skill_name": primitive_name,
            "skill_id": skill_id or primitive_id,
            "status": status,
            "timestamp": time.time(),
        }
        if reason:
            payload["reason"] = reason
        if inputs:
            payload["inputs"] = inputs
        self._task_status_pub.publish(String(data=json.dumps(payload)))

    def history_json(self) -> str:
        return json.dumps(self.history)

    def clear(self) -> None:
        self.history = []

    def speak(self, text: str) -> None:
        if self._tts_handler is not None:
            self._tts_handler.speak_text_async(text)
