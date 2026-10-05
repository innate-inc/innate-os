# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""LEGO-only pickup using the standard pickup pipeline and bounded local retries."""
import math
import time

from innate_skills.pick_any_object import PARAMS, PickAnyObject

from innate import SkillReturn
from innate.exceptions import SkillFailed


class PickLegos(PickAnyObject):
    """Pick LEGO bricks from the floor. Uses the standard pickup perception,
    approach and verification, with a lower bounded grasp and up to three
    local attempts when the claw fully closes empty. Use this for LEGO only;
    use pick_any_object for other objects. Several bricks per grasp are not
    guaranteed. Missing gripper feedback fails rather than retrying blindly."""

    _p = {**PARAMS, "floor_z": 0.02}
    # ee_link height, not fingertip height. Keep the pre-close un-press lift:
    # harder floor pressure stalls the fingers. No speed/current-limit changes.
    MAX_GRASPS = 3
    # Empty grasps on MARS 47 read -0.064 to -0.040 rad, not the
    # generic nominal -0.085. Leave a 0.010 rad margin above that range.
    EMPTY_J6 = -0.03

    @property
    def _soft_object(self):
        return False  # rigid LEGO must not use the fabric-winding twist

    def execute(self) -> SkillReturn:
        """Pick the pile of LEGOs; no target parameter is needed."""
        return super().execute("the pile of loose LEGOs directly on the floor, outside all boxes and containers; "
                               "exclude the box itself and any LEGOs inside it")

    def _close_twist_lift(self, x, y, roll, pitch, yaw):
        for attempt in range(self.MAX_GRASPS):
            self.check_cancelled()
            super()._close_twist_lift(x, y, roll, pitch, yaw)
            # Always lift first. This check happens before the inherited
            # verification can drive backwards or call the vision model.
            if not self._claw_empty_after_lift():
                return
            self._holding = False
            self.check_cancelled()
            if attempt + 1 == self.MAX_GRASPS:
                raise SkillFailed(f"Empty LEGO grasp after {self.MAX_GRASPS} attempts")
            self.overlay.readout(f"empty grasp; retry {attempt + 2}/{self.MAX_GRASPS}")
            self._claw_open()
            self.check_cancelled()
            self._push_to_floor(x, y, self.manipulation.pose.z, roll, pitch, yaw)

    def _claw_empty_after_lift(self):
        """One fresh gripper reading after lift; unknown feedback stops the run."""
        previous = self.joint_states
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            self.sleep(0.02)
            snapshot = self.joint_states
            if snapshot is None or snapshot is previous or len(snapshot.position) < 6:
                continue
            j6 = snapshot.position[5]
            if not math.isfinite(j6):
                continue
            empty = j6 <= self.EMPTY_J6
            self.logger.info(f"[PickLegos] after lift: j6={j6:.4f}, empty={empty}")
            return empty
        raise SkillFailed("No fresh gripper reading after lift; stopping before backing up")
