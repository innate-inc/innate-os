#!/usr/bin/env python3
"""
Greeting Friends Skill — speak a short friendly hello via the brain TTS pipeline.
"""

from std_msgs.msg import String

from brain_client.skill_types import Skill, SkillResult


class GreetingFriends(Skill):
    """Publish a phrase to /brain/tts so the robot says it out loud."""

    def __init__(self, logger):
        super().__init__(logger)
        self._tts_pub = None

    @property
    def name(self):
        return "greeting_friends"

    def guidelines(self):
        return (
            'Say out loud: "hello my friend!" Use when greeting someone or when the user '
            "asks for a friendly hello."
        )

    def _ensure_tts_pub(self):
        if self._tts_pub is None and self.node is not None:
            self._tts_pub = self.node.create_publisher(String, "/brain/tts", 10)

    def execute(self):
        phrase = "hello my friend!"

        if self.node is None:
            return "Skill has no ROS node; cannot speak", SkillResult.FAILURE

        self._ensure_tts_pub()
        if self._tts_pub is None:
            return "Could not create TTS publisher", SkillResult.FAILURE

        msg = String()
        msg.data = phrase
        self._tts_pub.publish(msg)
        self.logger.info(f"[GreetingFriends] Published TTS: {phrase!r}")
        return f"Said: {phrase}", SkillResult.SUCCESS

    def cancel(self):
        return "Greeting finished (nothing to cancel)"
