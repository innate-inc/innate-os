# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.knuckles_motion import KnucklesMotion

from innate import Skill, SkillReturn, perform, say, together


class Knuckles(Skill):
    """Use when someone says "knuckles," asks for some knuckles, or asks Mars for a fist bump. Act very excited"""

    motion_with_audio: KnucklesMotion

    def execute(self) -> SkillReturn:
        self.choreograph(
            say("Come on, give me some!"),
            together(
                say("knuckles"),
                perform(self.motion_with_audio),
            ),
            say("Nice."),
        )
        return "Gave some knuckles"
