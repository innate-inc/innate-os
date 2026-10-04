# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from typing import Literal, cast

from innate_skills.express import perform

from innate import Skill, SkillReturn

# Each emotion plays through the expression driver with the whole body (arm, gripper, head and a
# small stance): a built-in preset where one fits, else a prompt the planner turns into motion.
EMOTIONS: dict[str, dict[str, str]] = {
    "happy": {"preset": "happy"},
    "very_happy": {"prompt": "overjoyed, bouncing with delight"},
    "sad": {"preset": "sad"},
    "excited": {"preset": "excited"},
    "thinking": {"preset": "thinking"},
    "disappointed": {"prompt": "disappointed, deflating with a slow droop"},
    "surprised": {"preset": "surprised"},
    "confused": {"preset": "confused"},
    "angry": {"preset": "angry"},
    "sleepy": {"preset": "sleepy"},
    "proud": {"preset": "proud"},
    "agreeing": {"preset": "agreeing"},
    "disagreeing": {"preset": "disagreeing"},
}
EmotionName = Literal[
    "happy",
    "very_happy",
    "sad",
    "excited",
    "thinking",
    "disappointed",
    "surprised",
    "confused",
    "angry",
    "sleepy",
    "proud",
    "agreeing",
    "disagreeing",
]


class HeadEmotion(Skill):
    """Show an emotion with the whole body — posture, gripper, head and a small turn — for a deliberate
    gesture. Quick reactions while talking belong in emote tags in your speech instead."""

    def guidelines(self) -> str:
        return (
            "Show an emotion with the whole body, as a deliberate gesture. Requires 'emotion' "
            f"parameter, one of: {', '.join(repr(name) for name in EMOTIONS)}. "
            "Optionally pass 'repeat' (int, default 1) to play it again."
        )

    def execute(self, emotion: EmotionName, repeat: int = 1) -> SkillReturn:
        emotion = cast(EmotionName, emotion.strip().lower())
        if emotion not in EMOTIONS:
            self.fail(f"Unknown emotion '{emotion}'. Available: {', '.join(sorted(EMOTIONS))}")
        self.feedback(f"Expressing: {emotion}")
        for _ in range(max(1, min(int(repeat), 5))):
            perform(self, EMOTIONS[emotion])
        return f"Expressed '{emotion}' with the whole body"
