#!/usr/bin/env python3
"""
Head Emotion Skill - Express emotions through vertical head (tilt) movements.
"""

import time

from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


# Each pose is (angle_degrees, duration_seconds). Duration is the time to
# interpolate from the previous pose to this one.
EMOTIONS = {
    "happy": {
        "description": "Quick upward nods",
        "sequence": [(5, 0.12), (-5, 0.12), (10, 0.15), (-5, 0.12), (10, 0.15), (0, 0.18)],
    },
    "sad": {
        "description": "Slow droop downward",
        "sequence": [(0, 0.3), (-5, 0.35), (-10, 0.4), (-15, 0.4), (-20, 0.45), (-25, 0.5), (-25, 0.3)],
    },
    "excited": {
        "description": "Rapid enthusiastic bouncing",
        "sequence": [(10, 0.08), (-10, 0.08), (15, 0.1), (-15, 0.1), (10, 0.08), (-10, 0.08), (15, 0.1), (0, 0.12)],
    },
    "thinking": {
        "description": "Slow tilt up, pause, slight nod down",
        "sequence": [(5, 0.3), (10, 0.35), (15, 0.4), (15, 0.6), (15, 0.6), (10, 0.3), (5, 0.25), (-5, 0.2), (0, 0.3)],
    },
    "disappointed": {
        "description": "Slow shake-like droop then hold",
        "sequence": [(0, 0.25), (-5, 0.3), (-3, 0.2), (-10, 0.3), (-8, 0.2), (-15, 0.35), (-20, 0.4), (-20, 0.3)],
    },
    "surprised": {
        "description": "Quick jolt up then settle",
        "sequence": [(15, 0.08), (15, 0.15), (10, 0.15), (5, 0.15), (0, 0.2)],
    },
    "confused": {
        "description": "Tilt up, down, up hesitantly",
        "sequence": [(5, 0.2), (-5, 0.25), (8, 0.25), (-8, 0.25), (3, 0.2), (-3, 0.2), (0, 0.25)],
    },
    "angry": {
        "description": "Sharp downward jab, hold low, return",
        "sequence": [(-10, 0.1), (-25, 0.1), (-25, 0.2), (-25, 0.2), (-15, 0.15), (-5, 0.15), (0, 0.18)],
    },
    "sleepy": {
        "description": "Gradual droop with small recovery bobs",
        "sequence": [(0, 0.4), (-5, 0.5), (-3, 0.3), (-10, 0.5), (-8, 0.3), (-15, 0.5), (-12, 0.3), (-20, 0.5), (-25, 0.6), (-25, 0.4)],
    },
    "proud": {
        "description": "Slow confident rise and hold high",
        "sequence": [(0, 0.25), (5, 0.3), (10, 0.3), (15, 0.35), (15, 0.4), (15, 0.4), (10, 0.3), (5, 0.25), (0, 0.25)],
    },
    "agreeing": {
        "description": "Nodding yes",
        "sequence": [(-10, 0.15), (5, 0.15), (-10, 0.15), (5, 0.15), (-10, 0.15), (5, 0.15), (0, 0.2)],
    },
    "disagreeing": {
        "description": "Slow deliberate shake no via tilt",
        "sequence": [(-5, 0.18), (5, 0.18), (-8, 0.2), (8, 0.2), (-5, 0.18), (5, 0.18), (0, 0.2)],
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

        self.logger.info(f"[HeadEmotion] Playing '{emotion}' ({entry['description']}) x{repeat}")
        self._send_feedback(f"Expressing: {emotion}")

        interpolation_rate = 30.0  # Hz
        dt = 1.0 / interpolation_rate

        for r in range(repeat):
            current_angle = 0.0
            for target_angle, duration in sequence:
                if self._cancelled:
                    self.head.set_position(0)
                    return "Cancelled", SkillResult.CANCELLED
                steps = max(1, int(round(duration * interpolation_rate)))
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
