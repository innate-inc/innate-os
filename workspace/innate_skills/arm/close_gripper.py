# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Manipulation, Skill, SkillReturn
from innate.exceptions import ArmFailed


class CloseGripper(Skill):
    """Use this to close the gripper — to grab something a person holds out
    to you (ask them to place it between the claws first), or to fold the
    claw shut. strength is how hard to squeeze, 0.0-0.6: soft or deformable
    objects ~0.3, firm objects ~0.5. To pick objects up off the floor use
    pick_any_object instead."""

    manipulation: Manipulation

    def execute(self, strength: float = 0.4) -> SkillReturn:
        self.manipulation.torque_on()
        try:
            self.manipulation.gripper_close(strength=strength, duration=1.0)
        except ArmFailed:
            self.fail("the gripper did not close")
        return "Gripper closed"
