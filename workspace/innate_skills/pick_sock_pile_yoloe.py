# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.pick_sock_yoloe import PickSockYoloe

from innate import SkillReturn


class PickSockPileYoloe(PickSockYoloe):
    """Find a floor pile of socks and grasp its center to collect a handful."""

    detection_prompt = "a pile of socks"

    def execute(self) -> SkillReturn:
        """Try to grasp several socks together from a floor pile; count is not verified."""
        return super().execute()

    def _choose_cand(self, candidates):
        # Prefer the largest detected pile initially. After locking, inherited
        # identity checks keep the same pile even if another becomes larger.
        if self._last_seen is None:
            candidates = sorted(
                candidates, key=lambda c: (c[3][2] - c[3][0]) * (c[3][3] - c[3][1]), reverse=True
            )
        return super()._choose_cand(candidates)
