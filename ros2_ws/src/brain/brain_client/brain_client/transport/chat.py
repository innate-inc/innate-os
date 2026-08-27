# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Chat history, chat-out publishing, task-status publishing, and TTS.

Consolidates the ``{"sender", "text", "timestamp"}`` chat-entry dict and the
task-status payload that were copy-pasted across the old node. Owns the chat
history list so no other component needs to.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
import time

from brain_client.brain.context import split_tool_narration
from brain_client.common.enums import StrEnum

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_REPLY_IDS = itertools.count(1)


class Sender(StrEnum):
    """Chat-entry senders — the webapp switches on these exact values."""

    ROBOT = "robot"
    ROBOT_THOUGHTS = "robot_thoughts"
    SYSTEM = "system"
    SKILL_OUTPUT = "skill_output"


class ChatManager:
    def __init__(self, logger, chat_out_pub, task_status_pub, tts_handler=None):
        self._logger = logger
        self._chat_out_pub = chat_out_pub
        self._task_status_pub = task_status_pub
        self._tts_handler = tts_handler
        self.history: list[dict] = []

    @staticmethod
    def entry(sender: Sender, text: str) -> dict:
        return {"sender": sender, "text": text, "timestamp": time.time()}

    def emit(self, sender: Sender, text: str, speak: bool | None = None) -> None:
        """Append a chat entry, publish it, and (for robot speech) speak it.

        ``speak`` defaults to True only for the ROBOT sender, matching the old
        behaviour where thoughts/anticipation were published but not spoken.
        """
        chat_entry = self.entry(sender, text)
        self.history.append(chat_entry)
        self._logger.debug(f"chat_out: {chat_entry}")
        from std_msgs.msg import String  # deferred: keeps the module importable without ROS

        self._chat_out_pub.publish(String(data=json.dumps(chat_entry)))

        if speak is None:
            speak = sender == Sender.ROBOT
        if speak and text and text.strip():
            self.speak(text)

    def emit_system(self, text: str) -> None:
        """Publish a system message (never spoken)."""
        self.emit(Sender.SYSTEM, text, speak=False)

    def emit_thoughts(self, thoughts: str) -> None:
        """Publish a thought summary, trimmed — the panel wants a glimpse, not a log."""
        if len(thoughts) > 600:
            thoughts = thoughts[:600].rsplit(" ", 1)[0] + " …"
        self.emit(Sender.ROBOT_THOUGHTS, thoughts, speak=False)

    def publish_task_status(
        self,
        primitive_name: str,
        primitive_id: str | None,
        status: str,
        skill_id: str | None = None,
        reason: str | None = None,
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
        from std_msgs.msg import String  # deferred: keeps the module importable without ROS

        self._task_status_pub.publish(String(data=json.dumps(payload)))

    def history_json(self) -> str:
        return json.dumps(self.history)

    def clear(self) -> None:
        self.history = []

    def speak(self, text: str, replace_pending: bool = False, reply_id: str | None = None) -> None:
        if self._tts_handler is not None:
            self._tts_handler.speak_text_async(text, replace_pending=replace_pending, reply_id=reply_id)

    def stream_speech(self) -> SpeechStreamer:
        return SpeechStreamer(self)


class SpeechStreamer:
    """Feeds a streaming reply to TTS one sentence at a time, so the robot
    starts talking at the first sentence boundary instead of the last.

    ``feed`` runs on the generate call's worker thread while the agent loop
    reads ``spoke`` and mutes: the lock makes each sentence's mute-check-then-
    speak atomic, and :meth:`try_abandon` decides mute-if-unspoken atomically —
    without it, a reply could voice its first sentence right after the loop
    decided to abandon it.
    """

    def __init__(self, chat: ChatManager):
        self._chat = chat
        self._buffer = ""
        self._muted = False
        self._lock = threading.Lock()
        self.spoke = False
        self._reply_id = f"reply-{next(_REPLY_IDS)}"

    def feed(self, text: str) -> None:
        self._buffer += text
        *sentences, self._buffer = _SENTENCE_END.split(self._buffer)
        for sentence in sentences:
            self._say(sentence)

    def flush(self) -> None:
        self._say(self._buffer)
        self._buffer = ""

    def mute(self) -> None:
        """Drop everything not yet spoken — the reply went stale mid-stream."""
        with self._lock:
            self._muted = True
            self._buffer = ""

    def try_abandon(self) -> bool:
        """Mute iff nothing has been spoken yet; True when the reply was abandoned.

        The agent loop's preemption check: a turn that already started talking
        holds the floor and must finish, one that hasn't is silenced before its
        first sentence can slip out.
        """
        with self._lock:
            if self.spoke:
                return False
            self._muted = True
            self._buffer = ""
            return True

    def _say(self, sentence: str) -> None:
        with self._lock:
            return self._say_locked(sentence)

    def _say_locked(self, sentence: str) -> None:
        if self._muted:
            return
        # Leaked tool narration, never speech — cut mid-sentence too (the model
        # appends it without a boundary) and mute the rest of the reply. Shared
        # with context._clean_speech so the audio never carries text the chat
        # transcript scrubbed.
        sentence, truncated = split_tool_narration(sentence.strip())
        if truncated:
            self._muted = True
        if not re.search(r"[a-zA-Z0-9]", sentence):
            return
        # The first sentence supersedes stale queued utterances (a reply
        # mid-playback keeps its rest, see _survives_flush in tts.py); the
        # rest of this reply queues in order behind it.
        self._chat.speak(sentence, replace_pending=not self.spoke, reply_id=self._reply_id)
        self.spoke = True
