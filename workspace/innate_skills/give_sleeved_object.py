# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Hand the held sleeved object to a visitor over the booth wall, letting go only onto an open palm."""

import time
import urllib.error
from dataclasses import dataclass

from innate_skills.drop_sleeved_object_in_box import DropSleevedObjectInBox
from innate_skills.typesafe import OVERLOADED, Jev, JevBox, ask_jev, jev_from_config
from innate_skills.vertical_lift import lift_vertical

from innate import SkillReturn
from innate.exceptions import SkillFailed

PALM_QUESTION = (
    "Is an open human hand held flat, palm up, directly below the object in the gripper, "
    "close enough to catch it? Answer false for hands beside, above or touching the gripper. "
    "Return only true or false."
)

JEV_PALM_QUESTIONS = {
    "palm": {
        "type": "noul",
        "instructions": (
            "Is an open human hand held flat, palm up, directly below the object in the gripper in `wrist`, "
            "close enough to catch it?"
        ),
        "criteria": {
            "true": "An open, flat, palm-up hand directly below the held object, close enough to catch it.",
            "false": (
                "No hand, or a hand beside, above or touching the gripper, "
                "or a closed, sideways or distant hand that could not catch it."
            ),
        },
    },
    "hand": {"type": "box", "instructions": "the open palm in `wrist`"},
}


@dataclass(frozen=True)
class PalmReading:
    probability: float
    box: JevBox | None = None  # Astra readings carry no box
    box_confidence: float = 0.0

    def __str__(self) -> str:
        if self.box is None:
            return f"p={self.probability:.2f}"
        centre_x = (self.box["left"] + self.box["right"]) / 2
        centre_y = (self.box["top"] + self.box["bottom"]) / 2
        return f"p={self.probability:.2f} box_centre=({centre_x:.2f},{centre_y:.2f}) box_conf={self.box_confidence:.2f}"


def jev_reading(answers: dict) -> PalmReading:
    hand = answers["hand"]
    box = JevBox(
        left=float(hand["box"]["left"]),
        top=float(hand["box"]["top"]),
        right=float(hand["box"]["right"]),
        bottom=float(hand["box"]["bottom"]),
    )
    return PalmReading(float(answers["palm"]["noul"]), box, float(hand["confidence"]))


def rejected(error: urllib.error.HTTPError) -> bool:
    """A 4xx other than a rate limit fails the same way on every retry (bad key, rejected image)."""
    return 400 <= error.code < 500 and error.code not in OVERLOADED


class GiveSleevedObject(DropSleevedObjectInBox):
    """Dock on the taught tag, hold the object out over the wall, and open once a palm is under it."""

    # The tag sits on the wall's inner face, so "inset" reaches out over the wall to the visitor.
    _p = {**DropSleevedObjectInBox._p, "drop_inset": 0.08}
    DROP_RIGHT_M = 0.0
    RELEASE_Z = 0.32  # arc in well above the 20 cm wall
    LOWER_RELEASE_Z = 0.28  # held-out height: fingertips and sleeve stay above the wall
    CLEARANCE_Z = 0.32
    PRESENT_S = 1.0
    PALM_TIMEOUT_S = 15.0
    PALM_CONFIRMATIONS = 2
    PALM_P_MIN = 0.8
    # A confident hand box must sit under the fingers, not beside them; image fractions.
    PALM_BOX_CONFIDENCE = 0.5
    PALM_CENTRE_X = (0.25, 0.75)
    PALM_TOP_MIN = 0.35
    JEV_TIMEOUT_S = 5.0

    _presenting = False
    _jev: Jev | None = None

    def execute(self) -> SkillReturn:
        """Hand the held sleeved keychain to the visitor at the tagged spot; lets go only once an open hand is under it."""
        self._presenting = False
        self._jev = jev_from_config()
        self.logger.info(f"[GivePalm] judge={self._jev.model if self._jev else 'astra'}")
        super().execute()
        return "Handed the keychain to the visitor."

    def _release_at(self, near_x, near_y):
        x, y = self._release_xy(near_x, near_y)
        self._require_inset(x, near_x)
        self.overlay.stage("release")
        if self.manipulation.torque_enabled is not True:
            self.manipulation.torque_on()
        self._presenting = True
        self._reach_over_box(x, y)
        held_out = self.manipulation.move_to(
            x,
            y,
            self.LOWER_RELEASE_Z,
            pitch=self._p["arm_pitch"],
            duration=self.PRESENT_S,
            tolerance_xy=None,
            tolerance_z=None,
        )
        self._require_pose(held_out, x, y, self.LOWER_RELEASE_Z, stage="present")
        self._await_palm()
        self.manipulation.gripper_open(duration=0.6)
        self._released = True
        self._lift_out()
        if self._still_holding():
            raise SkillFailed("The keychain stuck to the gripper after opening")
        return x, y, self.LOWER_RELEASE_Z

    def _await_palm(self) -> None:
        self.overlay.readout("hold your hand flat under the keychain", busy=True)
        deadline = time.monotonic() + self.PALM_TIMEOUT_S
        streak = 0
        while time.monotonic() < deadline:
            streak = streak + 1 if self._is_palm(self._palm_reading()) else 0
            if streak >= self.PALM_CONFIRMATIONS:
                return
        raise SkillFailed("No hand under the keychain; still holding it")

    def _is_palm(self, reading: PalmReading | None) -> bool:
        if reading is None or reading.probability < self.PALM_P_MIN:
            return False
        if reading.box is None or reading.box_confidence < self.PALM_BOX_CONFIDENCE:
            return True
        centre_x = (reading.box["left"] + reading.box["right"]) / 2
        low, high = self.PALM_CENTRE_X
        return low <= centre_x <= high and reading.box["top"] >= self.PALM_TOP_MIN

    def _palm_reading(self) -> PalmReading | None:
        """One fresh wrist photo judged by Jev, or by Astra without a TypeSafe key; None when unclear."""
        stale = self.wrist_image
        image = self.wait_for(lambda: self.wrist_image if self.wrist_image is not stale else None, timeout=1.0)
        self.check_cancelled()
        if not image:
            return None
        started = time.monotonic()
        judge = "jev" if self._jev else "astra"
        reading = self._jev_palm(self._jev, image) if self._jev else self._astra_palm(image)
        self.check_cancelled()
        self.logger.info(f"[GivePalm] {judge} {reading} seconds={time.monotonic() - started:.3f}")
        return reading

    def _jev_palm(self, jev: Jev, image: str) -> PalmReading | None:
        try:
            return jev_reading(ask_jev(jev, {"wrist": image}, JEV_PALM_QUESTIONS, self.sleep, self.JEV_TIMEOUT_S))
        except urllib.error.HTTPError as error:
            self.logger.warning(f"[GivePalm] Jev HTTP {error.code}: {error.read()[:300]!r}")
            if rejected(error):
                self._jev = None  # finish this handoff on Astra rather than time out the visitor
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.logger.warning(f"[GivePalm] Jev failed: {error!r}")
        return None

    def _astra_palm(self, image: str) -> PalmReading | None:
        answer = self._grip_llm.ask(image, PALM_QUESTION, logger=self.logger, retries=1)
        answer = answer.strip().lower() if isinstance(answer, str) else None
        return {"true": PalmReading(1.0), "false": PalmReading(0.0)}.get(answer or "")

    def _still_holding(self) -> bool:
        try:
            return self._object_held()
        except SkillFailed as error:  # an unclear photo after release must not fail a handoff
            self.logger.warning(f"[GivePalm] post-release check unclear: {error}")
            return False

    def _retract(self):
        if not self._presenting or self._released:
            super()._retract()
            return
        try:
            self._lift_out()
            # REST would swing the held keychain down through the wall; carry keeps it high.
            lift_vertical(self, height=None, carry_joints=self._p["travel_joints"])
        except Exception as e:  # noqa: BLE001 — teardown must not mask the skill result
            self.logger.warning(f"[GivePalm] carry retract failed; holding arm: {e}")
