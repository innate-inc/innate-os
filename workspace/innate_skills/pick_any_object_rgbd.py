# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Experimental bounded RGB-D pickup; independent feed subscription."""

from innate_skills.pick_any_object import PickAnyObject

from innate import RgbdObservation, SkillReturn


class PickAnyObjectRgbd(PickAnyObject):
    """Pick a compact low rigid object using calibrated head depth and a wrist veto.

    Unsupported geometry or changing observations abort conservatively.
    """

    rgbd: RgbdObservation | None

    def execute(self, prompt: str = "the red cube") -> SkillReturn:
        return super().execute(prompt=prompt, controller="rgbd")
