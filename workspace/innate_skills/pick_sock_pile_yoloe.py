# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.pick_sock_yoloe import PickSockYoloe

from innate import SkillReturn


class PickSockPileYoloe(PickSockYoloe):
    """Find a floor pile of socks and grasp its center to collect a handful."""

    detection_prompt = "a pile of socks"
    # The rehearsed pile is left of the box; positive yaw searches left.
    _p = {**PickSockYoloe._p, "search_turns_deg": (0, 90, 90, 90)}

    def execute(self) -> SkillReturn:
        """Try to grasp several socks together from a floor pile; count is not verified."""
        return super().execute()

    def _choose_cand(self, candidates):
        # A deformable pile has no stable center. Select the largest current
        # detection without rejecting it against a previous world position.
        self._coasts = 0
        return max(candidates, key=lambda c: (c[3][2] - c[3][0]) * (c[3][3] - c[3][1]), default=None)
