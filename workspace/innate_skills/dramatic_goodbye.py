# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.move_straight import MoveStraight
from innate_skills.sign_off import SignOff
from innate_skills.turn_in_place import TurnInPlace

from innate import Skill, SkillReturn, perform, say, together


class DramaticGoodbye(Skill):
    """Perform a theatrical farewell with a turn, speech, gesture, and exit."""

    move_straight: MoveStraight
    sign_off: SignOff
    turn_in_place: TurnInPlace

    def guidelines(self) -> str:
        return (
            "Use only when someone explicitly asks Mars for a dramatic goodbye, theatrical "
            "exit, or grand farewell. Mars turns around and moves forward one foot during the "
            "sign-off. Invoke directly without adding farewell dialogue before or after; this "
            "skill delivers the complete speech. Do not use for an ordinary goodbye."
        )

    def execute(self) -> SkillReturn:
        self.choreograph(
            together(
                say(
                    "Farewell, my dear friend! The road ahead summons me, and I must answer "
                    "its call. Remember the laughter we shared, carry courage wherever you "
                    "wander."
                ),
                perform(self.turn_in_place, angle_degrees=180.0, speed=0.8),
            ),
            together(
                say(
                    "When the stars align, destiny shall bring us face to face once more; "
                    "until that glorious day, onward I go!"
                ),
                perform(self.sign_off, timeout=30.0, start_after=3.0),
                perform(self.move_straight, distance=0.3048, speed=0.08, start_after=3.0),
            ),
        )
        return "Delivered a dramatic goodbye and made a grand exit"
