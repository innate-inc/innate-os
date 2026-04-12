#!/usr/bin/env python3
"""
Greet Skill - Hardcoded friendly greeting: head wave animation + voice.

No cloud or AI dependency. Pure robot action + espeak TTS.
"""

import subprocess
import time

from std_msgs.msg import String

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


# Rotate through these so repeated greetings feel natural
GREETINGS = [
    "Hello! I am Maurice, nice to meet you!",
    "Hi there! Great to see you!",
    "Hey! How is it going?",
    "Greetings! Hope you are having a wonderful day!",
    "Good to see you! What can I do for you?",
]

# Head tilt sequence: (angle_degrees, duration_seconds)
# Quick enthusiastic nods — a universal friendly gesture
WAVE_SEQUENCE = [
    (8, 0.10),
    (-6, 0.10),
    (12, 0.12),
    (-6, 0.10),
    (10, 0.12),
    (0, 0.18),
]

_INTERP_HZ = 30  # head interpolation rate


class Greet(Skill):
    """Wave head + say hello. No AI needed — pure hardcoded behaviour."""

    head = Interface(InterfaceType.HEAD)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._next_greeting = 0

    @property
    def name(self):
        return "greet"

    def guidelines(self):
        return (
            "Wave and greet a nearby person with a head animation and friendly voice. "
            "Optionally pass 'greeting' (str) to override the default message. "
            "Use when you first see someone or want to say hello."
        )

    def execute(self, greeting: str = ""):
        self._cancelled = False

        # Pick greeting text
        if not greeting:
            greeting = GREETINGS[self._next_greeting % len(GREETINGS)]
            self._next_greeting += 1

        self.logger.info(f"[Greet] '{greeting}'")
        self._send_feedback(f"Greeting: {greeting}")

        # Fire-and-forget voice so it overlaps with the head motion
        self._say(greeting)

        # Head wave animation
        if self.head is not None:
            result = self._play_wave()
            if result == "cancelled":
                return "Greet cancelled", SkillResult.CANCELLED
        else:
            self.logger.warning("[Greet] Head interface not available — skipping animation")
            time.sleep(1.5)  # let speech finish

        return greeting, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Greet cancelled"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _play_wave(self) -> str:
        dt = 1.0 / _INTERP_HZ
        current = 0.0
        for target, duration in WAVE_SEQUENCE:
            if self._cancelled:
                self.head.set_position(0)
                return "cancelled"
            steps = max(1, int(round(duration * _INTERP_HZ)))
            for i in range(1, steps + 1):
                if self._cancelled:
                    self.head.set_position(0)
                    return "cancelled"
                t = i / steps
                angle = current + (target - current) * t
                self.head.set_position(int(round(angle)))
                time.sleep(dt)
            current = float(target)
        self.head.set_position(0)
        return "done"

    def _say(self, text: str, speed: int = 150):
        """Publish to /brain/tts (Cartesia), fall back to espeak."""
        if self.node is not None:
            try:
                pub = self.node.create_publisher(String, "/brain/tts", 10)
                msg = String()
                msg.data = text
                pub.publish(msg)
                self.logger.info(f"[Greet] Published to /brain/tts: '{text[:60]}'")
                return
            except Exception as e:
                self.logger.warning(f"[Greet] /brain/tts publish failed ({e}), falling back to espeak")

        try:
            subprocess.Popen(
                ["espeak", "-s", str(speed), "--", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.logger.warning("[Greet] espeak not found — voice output skipped")
        except Exception as e:
            self.logger.error(f"[Greet] Speech error: {e}")
