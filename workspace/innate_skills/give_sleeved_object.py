# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Hand the held sleeved object to a visitor over the booth wall, letting go only onto an open palm."""

import time

from innate_skills.drop_sleeved_object_in_box import DropSleevedObjectInBox
from innate_skills.vertical_lift import lift_vertical

from innate import SkillReturn
from innate.exceptions import SkillFailed

PALM_QUESTION = (
    "Is an open human hand held flat, palm up, directly below the object in the gripper, "
    "close enough to catch it? Answer false for hands beside, above or touching the gripper. "
    "Return only true or false."
)


def palm_under(host) -> bool | None:
    """One fresh wrist photo judged by Astra; None when there is no fresh photo or no clear answer."""
    stale = host.wrist_image
    image = host.wait_for(lambda: host.wrist_image if host.wrist_image is not stale else None, timeout=1.0)
    host.check_cancelled()
    if not image:
        return None
    started = time.monotonic()
    answer = host._grip_llm.ask(image, PALM_QUESTION, logger=host.logger, retries=1)
    host.check_cancelled()
    answer = answer.strip().lower() if isinstance(answer, str) else None
    host.logger.info(f"[GivePalm] answer={answer!r} seconds={time.monotonic() - started:.3f}")
    return {"true": True, "false": False}.get(answer)


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

    _presenting = False

    def execute(self) -> SkillReturn:
        """Hand the held sleeved keychain to the visitor at the tagged spot; lets go only once an open hand is under it."""
        self._presenting = False
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
            streak = streak + 1 if palm_under(self) else 0
            if streak >= self.PALM_CONFIRMATIONS:
                return
        raise SkillFailed("No hand under the keychain; still holding it")

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
