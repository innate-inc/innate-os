# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.navigate_to_position import NavigateToPosition
from innate import SkillReturn


class NavigateLocally(NavigateToPosition):
    """Navigate relative to the robot now: x metres forward, y metres left,
    theta_degrees relative heading. Uses the existing local navigation path.
    Does not accept remembered map coordinates or a map-frame mode."""

    def execute(self, x: float, y: float, theta_degrees: float = 0.0) -> SkillReturn:
        return super().execute(x, y, theta_degrees=theta_degrees, local_frame=True)
