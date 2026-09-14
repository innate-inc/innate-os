# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
from typing import Literal

from innate import Manipulation, Skill, SkillReturn
from innate.exceptions import ArmFailed, ArmUnhealthy


class ArmMove(Skill):
    """Move the arm using either mode='xyz' (IK) or mode='joints'.

    In xyz mode, supply x, y, z in metres relative to base_link; optional
    roll, pitch, yaw are radians. In joints mode, supply joints in motor order
    (joint1 through joint6), in radians: five angles preserve the gripper,
    six also set it (joint6 must be between -0.6 and 0.85 radians).
    Do not mix XYZ and joint targets. Duration is seconds.
    Commands may settle off-target near joint limits; success confirms motion
    completion, not exact final pose accuracy.
    """

    manipulation: Manipulation

    def execute(
        self,
        mode: Literal["xyz", "joints"],
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        joints: list[float] | None = None,
        roll: float = 0.0,
        pitch: float = 0.0,
        yaw: float = 0.0,
        duration: float = 3.0,
    ) -> SkillReturn:
        def finite(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

        if mode not in ("xyz", "joints"):
            self.fail("mode must be 'xyz' or 'joints'")
        if not finite(duration) or duration <= 0:
            self.fail("duration must be a finite positive number of seconds")
        if not all(finite(value) for value in (roll, pitch, yaw)):
            self.fail("roll, pitch and yaw must be finite radians")
        if mode == "xyz":
            if joints is not None or not all(finite(value) for value in (x, y, z)):
                self.fail("xyz mode requires finite x, y, z and no joints target")
        else:
            if any(value is not None for value in (x, y, z)) or any((roll, pitch, yaw)):
                self.fail("joints mode does not accept XYZ or orientation targets")
            if not isinstance(joints, list) or len(joints) not in (5, 6) or not all(finite(j) for j in joints):
                self.fail("joints must contain five or six finite angles in radians")
            if len(joints) == 6:
                minimum = Manipulation.GRIPPER_CLOSED - Manipulation.GRIPPER_MAX_STRENGTH
                maximum = Manipulation.GRIPPER_OPEN
                if not minimum <= joints[5] <= maximum:
                    self.fail(f"joint6 (gripper) must be between {minimum} and {maximum} radians")
        try:
            if mode == "xyz":
                self.manipulation.move_to(
                    x,
                    y,
                    z,
                    roll=roll,
                    pitch=pitch,
                    yaw=yaw,
                    duration=duration,
                    tolerance_xy=None,
                    tolerance_z=None,
                )
            else:
                self.manipulation.move_joints(joints, duration=duration)
        except (ArmFailed, ArmUnhealthy) as error:
            self.fail(f"Arm move failed: {error}")
        return "Arm motion completed."
