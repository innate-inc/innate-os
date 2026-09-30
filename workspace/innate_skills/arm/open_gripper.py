# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Manipulation, Skill, SkillReturn
from innate.exceptions import ArmFailed, ArmUnhealthy


class OpenGripper(Skill):
    """Use this to open the gripper and release whatever it is holding — the
    way to put down or hand over an object after pick_any_object. The object
    simply drops from the claw, so get close to where it should land first.
    Optional percent (0-100) sets how far the claw opens; default fully open."""

    manipulation: Manipulation

    def execute(self, percent: int = 100) -> SkillReturn:
        self.manipulation.torque_on()
        try:
            self.manipulation.gripper_open(percent=percent, duration=1.0)
        except (ArmFailed, ArmUnhealthy):
            self.fail("the gripper did not open (servo tripped shut)")
        return "Gripper opened"
