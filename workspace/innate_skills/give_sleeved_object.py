# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Hand the held sleeved object to a visitor over the booth wall, letting go once a hand reaches for it."""

import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from innate_skills.drop_sleeved_object_in_box import DropSleevedObjectInBox
from innate_skills.typesafe import OVERLOADED, Jev, JevBox, ask_jev, jev_from_config
from innate_skills.vertical_lift import lift_vertical

from innate import SkillReturn
from innate.exceptions import SkillFailed

PALM_QUESTION = (
    "Is a person's hand reaching for, touching, grabbing or held open near the keychain in the gripper, "
    "as if to take it? The hand may be at any side or edge of the photo. "
    "Answer false only when no hand is near the keychain. Return only true or false."
)


def reach_questions(camera: str) -> dict:
    """Jev questions about one camera's photo, named `camera` in the request state."""
    return {
        "palm": {
            "type": "noul",
            "instructions": (
                f"Is a person's hand in `{camera}` reaching for, touching, grabbing or held open near the keychain "
                "in the robot's gripper, as if to take it? The hand may be at any side or edge of the photo."
            ),
            "criteria": {
                "true": "A hand near the held keychain: reaching for it, grabbing or touching it, or open under or beside it.",
                "false": "No hand in the photo, or only a hand far away that is not reaching for the keychain.",
            },
        },
        "hand": {"type": "box", "instructions": f"the human hand in `{camera}`"},
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
    """Dock on the taught tag, hold the object out over the wall, and open once a hand reaches for it."""

    # The tag sits on the wall's inner face, so "inset" reaches out over the wall to the visitor.
    _p = {**DropSleevedObjectInBox._p, "drop_inset": 0.08}
    DROP_RIGHT_M = 0.0
    RELEASE_Z = 0.32  # arc in well above the 20 cm wall
    LOWER_RELEASE_Z = 0.28  # held-out height: fingertips and sleeve stay above the wall
    CLEARANCE_Z = 0.32
    PRESENT_S = 1.0
    PALM_TIMEOUT_S = 15.0
    PALM_CONFIRMATIONS = 2
    PALM_P_MIN = 0.6
    JEV_TIMEOUT_S = 5.0

    _presenting = False
    _jev: Jev | None = None

    def execute(self) -> SkillReturn:
        """Hand the held sleeved keychain to the visitor at the tagged spot; lets go once their hand reaches for it."""
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
        self.overlay.readout("reach out and take the keychain", busy=True)
        deadline = time.monotonic() + self.PALM_TIMEOUT_S
        streak = 0
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            while time.monotonic() < deadline:
                streak = streak + 1 if self._is_palm(self._palm_reading(pool)) else 0
                if streak >= self.PALM_CONFIRMATIONS:
                    return
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        raise SkillFailed("No hand reached for the keychain; still holding it")

    def _is_palm(self, reading: PalmReading | None) -> bool:
        return reading is not None and reading.probability >= self.PALM_P_MIN

    def _palm_reading(self, pool: ThreadPoolExecutor) -> PalmReading | None:
        """Fresh wrist and head photos judged in parallel by Jev, or by Astra without a TypeSafe key.
        Either camera seeing a reaching hand is enough; None when neither gave a clear answer."""
        images = self._fresh_images()
        started = time.monotonic()
        judge = "jev" if self._jev else "astra"
        jobs = {camera: pool.submit(self._judge, camera, image) for camera, image in images.items()}
        readings = {camera: job.result() for camera, job in jobs.items()}
        self.check_cancelled()
        summary = " ".join(f"{camera}:{reading}" for camera, reading in readings.items())
        self.logger.info(f"[GivePalm] {judge} {summary} seconds={time.monotonic() - started:.3f}")
        clear = [reading for reading in readings.values() if reading is not None]
        return max(clear, key=lambda reading: reading.probability, default=None)

    def _fresh_images(self) -> dict[str, str]:
        """Waits up to 1 s for a new frame from both cameras and uses whichever arrived."""
        stale = self._camera_frames()

        def fresh() -> dict[str, str]:
            return {camera: image for camera, image in self._camera_frames().items() if image is not stale.get(camera)}

        self.wait_for(lambda: True if len(fresh()) == 2 else None, timeout=1.0)
        self.check_cancelled()
        return fresh()

    def _camera_frames(self) -> dict[str, str]:
        frames = {"wrist": self.wrist_image, "head": self.main_image}
        return {camera: image for camera, image in frames.items() if image}

    def _judge(self, camera: str, image: str) -> PalmReading | None:
        jev = self._jev
        return self._jev_palm(jev, camera, image) if jev else self._astra_palm(image)

    def _jev_palm(self, jev: Jev, camera: str, image: str) -> PalmReading | None:
        try:
            return jev_reading(ask_jev(jev, {camera: image}, reach_questions(camera), self.sleep, self.JEV_TIMEOUT_S))
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
