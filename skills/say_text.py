#!/usr/bin/env python3
"""
Say text — publish a phrase to /brain/tts so the robot says it out loud.
"""

from std_msgs.msg import String

from brain_client.skill_types import Skill, SkillResult


class SayText(Skill):
    """Publish a phrase to /brain/tts so the robot says it out loud."""

    def __init__(self, logger):
        super().__init__(logger)
        self._tts_pub = None

    @property
    def name(self):
        return "say_text"

    def guidelines(self):
        return (
            'Say out loud: "hello my friend!" (default), or a custom phrase if `text` is given. '
            "Use when the user wants the robot to speak specific words or a friendly hello."
        )

    def _ensure_tts_pub(self):
        if self._tts_pub is None and self.node is not None:
            self._tts_pub = self.node.create_publisher(String, "/brain/tts", 10)

    def execute(self, text: str | None = None, **_kwargs):
        raw = (text or "").strip() if text is not None else ""
        phrase = raw if raw else "hello my friend!"

        if self.node is None:
            return "Skill has no ROS node; cannot speak", SkillResult.FAILURE

        self._ensure_tts_pub()
        if self._tts_pub is None:
            return "Could not create TTS publisher", SkillResult.FAILURE

        msg = String()
        msg.data = phrase
        self._tts_pub.publish(msg)
        self.logger.info(f"[SayText] Published TTS: {phrase!r}")
        return f"Said out loud: {phrase}", SkillResult.SUCCESS

    def cancel(self):
        return "Say text finished (nothing to cancel)"
