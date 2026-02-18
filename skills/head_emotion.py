#!/usr/bin/env python3
"""
Head Emotion Skill - Express emotions through vertical head (tilt) movements.
"""

import time

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


EMOTIONS = {
    "happy": {
        "description": "Quick upward nods",
        "sequence": [5, -5, 10, -5, 10, 0],
        "delay": 0.15,
    },
    "sad": {
        "description": "Slow droop downward",
        "sequence": [0, -5, -10, -15, -20, -25, -25],
        "delay": 0.4,
    },
    "excited": {
        "description": "Rapid enthusiastic bouncing",
        "sequence": [10, -10, 15, -15, 10, -10, 15, 0],
        "delay": 0.1,
    },
    "thinking": {
        "description": "Slow tilt up, pause, slight nod down",
        "sequence": [5, 10, 15, 15, 15, 10, 5, -5, 0],
        "delay": 0.35,
    },
    "disappointed": {
        "description": "Slow shake-like droop then hold",
        "sequence": [0, -5, -3, -10, -8, -15, -20, -20],
        "delay": 0.3,
    },
    "surprised": {
        "description": "Quick jolt up then settle",
        "sequence": [15, 15, 10, 5, 0],
        "delay": 0.12,
    },
    "confused": {
        "description": "Tilt up, down, up hesitantly",
        "sequence": [5, -5, 8, -8, 3, -3, 0],
        "delay": 0.25,
    },
    "angry": {
        "description": "Sharp downward jab, hold low, return",
        "sequence": [-10, -25, -25, -25, -15, -5, 0],
        "delay": 0.15,
    },
    "sleepy": {
        "description": "Gradual droop with small recovery bobs",
        "sequence": [0, -5, -3, -10, -8, -15, -12, -20, -25, -25],
        "delay": 0.5,
    },
    "proud": {
        "description": "Slow confident rise and hold high",
        "sequence": [0, 5, 10, 15, 15, 15, 10, 5, 0],
        "delay": 0.3,
    },
    "agreeing": {
        "description": "Nodding yes",
        "sequence": [-10, 5, -10, 5, -10, 5, 0],
        "delay": 0.18,
    },
    "disagreeing": {
        "description": "Slow deliberate shake no via tilt",
        "sequence": [-5, 5, -8, 8, -5, 5, 0],
        "delay": 0.2,
    },
}


class HeadEmotion(Skill):
    """Express emotions through vertical head tilt movements."""

    head = Interface(InterfaceType.HEAD)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "head_emotion"

    def guidelines(self):
        emotion_list = ", ".join(f"'{e}'" for e in EMOTIONS)
        return (
            "Express an emotion through head tilt movements. "
            f"Requires 'emotion' parameter, one of: {emotion_list}. "
            "Optionally pass 'repeat' (int, default 1) to loop the animation."
        )

    def execute(self, emotion: str, repeat: int = 1):
        """
        Play a head-tilt animation for the given emotion.

        Args:
            emotion: One of the supported emotion names.
            repeat: Number of times to play the animation (default 1).
        """
        self._cancelled = False

        if self.head is None:
            return "Head interface not available", SkillResult.FAILURE

        emotion = emotion.strip().lower()
        if emotion not in EMOTIONS:
            available = ", ".join(sorted(EMOTIONS))
            return f"Unknown emotion '{emotion}'. Available: {available}", SkillResult.FAILURE

        repeat = max(1, min(int(repeat), 5))
        entry = EMOTIONS[emotion]
        sequence = entry["sequence"]
        delay = entry["delay"]

        self.logger.info(f"[HeadEmotion] Playing '{emotion}' ({entry['description']}) x{repeat}")
        self._send_feedback(f"Expressing: {emotion}")

        interpolation_rate = 30.0  # Hz
        dt = 1.0 / interpolation_rate

        for r in range(repeat):
            current_angle = 0.0
            for target_angle in sequence:
                if self._cancelled:
                    self.head.set_position(0)
                    return "Cancelled", SkillResult.CANCELLED
                steps = max(1, int(round(delay * interpolation_rate)))
                for i in range(1, steps + 1):
                    if self._cancelled:
                        self.head.set_position(0)
                        return "Cancelled", SkillResult.CANCELLED
                    t = i / steps
                    interp = current_angle + (target_angle - current_angle) * t
                    self.head.set_position(int(round(interp)))
                    time.sleep(dt)
                current_angle = float(target_angle)
            if r < repeat - 1:
                time.sleep(0.2)

        # Return to neutral
        self.head.set_position(0)

        msg = f"Expressed '{emotion}' ({entry['description']})"
        self.logger.info(f"[HeadEmotion] {msg}")
        return msg, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Head emotion cancelled"
