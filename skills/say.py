#!/usr/bin/env python3
"""
Say Skill - Speak text out loud via espeak subprocess.

Hardcoded voice output. No cloud dependency — works offline.
Optionally falls back to logging if espeak is unavailable.
"""

import subprocess
import time

from std_msgs.msg import String

from brain_client.skill_types import Skill, SkillResult


# Preset phrases the robot can say when no text is given
PRESET_PHRASES = {
    "hello": "Hello! I am Maurice, nice to meet you!",
    "ready": "I am ready and operational. How can I help?",
    "bye": "Goodbye! It was nice talking to you.",
    "thinking": "Let me think about that for a moment.",
    "done": "Task completed successfully.",
    "error": "I encountered a problem. Please check my status.",
}


class Say(Skill):
    """Speak a line of text aloud using espeak."""

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "say"

    def guidelines(self):
        return (
            "Make the robot speak text aloud. "
            "Pass 'text' (str) to say something specific. "
            f"Or pass 'preset' (str) for a built-in phrase: {list(PRESET_PHRASES.keys())}. "
            "Optionally pass 'speed' (int, 80–300, default 150) to control speech rate."
        )

    def execute(self, text: str = "", preset: str = "", speed: int = 150):
        """
        Speak the given text.

        Args:
            text:   Arbitrary text to speak.
            preset: Key from PRESET_PHRASES; used only when text is empty.
            speed:  Words-per-minute for espeak (80–300).
        """
        if not text:
            text = PRESET_PHRASES.get(preset.lower(), "")
        if not text:
            return "No text to speak", SkillResult.FAILURE

        speed = max(80, min(int(speed), 300))

        self.logger.info(f"[Say] Speaking: '{text[:80]}{'...' if len(text) > 80 else ''}'")
        self._send_feedback(f"Saying: {text}")

        self._speak(text, speed)
        return text, SkillResult.SUCCESS

    def cancel(self):
        # espeak runs as a fire-and-forget subprocess; we can't cancel mid-speech easily
        return "Say skill cannot be cancelled mid-utterance"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _speak(self, text: str, speed: int = 150):
        """
        Publish to /brain/tts so brain_client_node speaks via Cartesia.
        Falls back to espeak if the ROS node is unavailable.
        """
        # Primary: Cartesia TTS via ROS topic
        if self.node is not None:
            try:
                pub = self.node.create_publisher(String, "/brain/tts", 10)
                msg = String()
                msg.data = text
                pub.publish(msg)
                self.logger.info(f"[Say] Published to /brain/tts: '{text[:60]}'")
                time.sleep(0.2)  # let the message dispatch before returning
                return
            except Exception as e:
                self.logger.warning(f"[Say] /brain/tts publish failed ({e}), falling back to espeak")

        # Fallback: espeak
        try:
            subprocess.Popen(
                ["espeak", "-s", str(speed), "--", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.3)
        except FileNotFoundError:
            self.logger.warning("[Say] espeak not found — text not spoken aloud")
        except Exception as e:
            self.logger.error(f"[Say] espeak error: {e}")
