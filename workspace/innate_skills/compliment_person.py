# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import threading

from innate_skills.come_here import ComeHere
from innate_skills.head_emotion import HeadEmotion
from innate_skills.short_clap import ShortClap

from innate import Skill, SkillCancelled, SkillReturn

HAPPY_NOD_SECONDS = 0.84
SPEECH_WORDS_PER_SECOND = 2.5
CLAP_SPEECH_FRACTION = 0.7


class ComplimentPerson(Skill):
    """Compliment a visible person after gaze has turned Mars toward them."""

    come_here: ComeHere
    head_emotion: HeadEmotion
    short_clap: ShortClap

    def guidelines(self) -> str:
        return (
            "Always use when the user asks for a compliment; never substitute plain speech, "
            "head motion, or navigation. After gaze faces a clearly visible person, compose a "
            "fresh, detailed, genuine compliment about only their visible clothing, styling, "
            "or appearance. Write two or three natural sentences with the vivid warmth of a "
            "love-story author, but remain specific rather than excessively flowery. Pass only "
            "the compliment body, without a greeting. Teleop may supply its own compliment."
        )

    def execute(self, compliment: str) -> SkillReturn:
        compliment = compliment.strip()
        if not compliment:
            self.fail("A freshly generated compliment is required")

        self.say("Hey, you! Come a little closer.")
        nod = threading.Thread(target=self._happy_nod, daemon=True)
        nod.start()
        try:
            self.come_here(timeout=30.0)
        finally:
            nod.join(timeout=2.0)

        self.say(compliment)
        self._happy_nod()
        self.sleep(self._clap_delay(compliment))
        self.short_clap(timeout=30.0)
        return "Beckoned, complimented, and applauded"

    @staticmethod
    def _clap_delay(compliment: str) -> float:
        speech_seconds = len(compliment.split()) / SPEECH_WORDS_PER_SECOND
        clap_at = speech_seconds * CLAP_SPEECH_FRACTION
        return max(0.0, clap_at - HAPPY_NOD_SECONDS)

    def _happy_nod(self) -> None:
        try:
            self.head_emotion(emotion="happy")
        except SkillCancelled:
            pass
