# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Manipulation, Skill, SkillReturn


class ArmZeroPosition(Skill):
    """Use this to move the arm to its zero/home position where all joints are
    at 0 radians. Safe while holding an object: the gripper keeps its current
    closure unless keep_gripper=False."""

    manipulation: Manipulation

    def execute(self, duration: int = 3, keep_gripper: bool = True) -> SkillReturn:
        if keep_gripper:
            # 5-joint move: j6 zero is *less* closed than a gripping pose, so
            # blindly zeroing it would drop whatever the claw holds.
            self.manipulation.move_joints(self.manipulation.ZERO[:5], duration=duration)
        else:
            self.manipulation.move_joints(self.manipulation.ZERO, duration=duration)
        return "Arm moved to zero position"
