# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Manipulation, Skill, SkillReturn


class ArmRestPosition(Skill):
    """Use this to move the arm to its resting position (folded against the
    body, servos unloaded). Safe while holding an object: the gripper keeps
    its current closure unless keep_gripper=False."""

    manipulation: Manipulation

    def execute(self, duration: int = 3, keep_gripper: bool = True) -> SkillReturn:
        if keep_gripper:
            self.manipulation.rest(duration=duration)
        else:
            self.manipulation.move_joints(self.manipulation.REST, duration=duration)
        return "Arm moved to rest position"
