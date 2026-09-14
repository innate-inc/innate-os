# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
from typing import Literal

from innate import Manipulation, Skill, SkillReturn
from innate.exceptions import ArmFailed, ArmUnhealthy


class ArmMove(Skill):
    """Position the arm's gripper with mode='xyz' (IK) or mode='joints'.

    XYZ directions are robot-relative base_link axes, NOT camera/screen axes:
    +x forward, -x backward; +y left, -y right; +z up, -z down. Units are metres.
    With relative=False, x/y/z are absolute coordinates, not motion distances:
    z=0.2 means 20 cm above the base origin and does NOT mean 'lift by 20 cm'.
    For 'lift/raise the arm', use relative=True with x=0, y=0 and positive z;
    e.g. z=0.10 raises the gripper 10 cm from its measured current position.
    Relative XYZ preserves current orientation; leave roll/pitch/yaw at zero.
    For other directional requests use offsets along the corresponding axis.
    If the requested distance is unspecified, use a small 5 cm offset. Do not
    invent absolute coordinates or substitute guessed joints if IK fails.
    Absolute XYZ uses roll/pitch/yaw in radians (zero is a fixed orientation,
    not 'keep the current orientation'). Duration is seconds.

    Joint mode takes ABSOLUTE angles in radians, in order:
    joint1: base yaw about +Z (positive turns left from the zero pose);
    joint2: shoulder, joint3: elbow, joint4: wrist pitch (each about local +Y);
    joint5: wrist roll about local +X; joint6: gripper.
    Positive rotations follow the right-hand rule. Pitch-joint signs do not
    mean 'up/down' independently of the other joints: use XYZ for directions.
    Zero means the URDF zero pose, not 'leave unchanged'. Joint 1-5 ranges:
    [-1.5708,1.5708], [-1.5708,1.22], [-1.5708,1.7453],
    [-1.9199,1.7453], [-1.5708,1.5708]. Five values preserve the gripper;
    six also set it: 0 closed, +0.85 open, negative adds closing preload
    (minimum -0.6). Do not mix joint targets with XYZ, orientation or relative.
    Completion confirms motion ended, not exact final pose accuracy.
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
        relative: bool = False,
    ) -> SkillReturn:
        def finite(value):
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

        if not isinstance(relative, bool):
            self.fail("relative must be a boolean")
        if mode not in ("xyz", "joints"):
            self.fail("mode must be 'xyz' or 'joints'")
        if not finite(duration) or duration <= 0:
            self.fail("duration must be a finite positive number of seconds")
        if not all(finite(value) for value in (roll, pitch, yaw)):
            self.fail("roll, pitch and yaw must be finite radians")
        if mode == "xyz":
            if joints is not None or not all(finite(value) for value in (x, y, z)):
                self.fail("xyz mode requires finite x, y, z and no joints target")
            if relative and any((roll, pitch, yaw)):
                self.fail("relative XYZ preserves orientation; leave roll, pitch and yaw at zero")
        else:
            if relative or any(value is not None for value in (x, y, z)) or any((roll, pitch, yaw)):
                self.fail("joints mode does not accept XYZ, orientation or relative targets")
            if not isinstance(joints, list) or len(joints) not in (5, 6) or not all(finite(j) for j in joints):
                self.fail("joints must contain five or six finite angles in radians")
            if len(joints) == 6:
                minimum = Manipulation.GRIPPER_CLOSED - Manipulation.GRIPPER_MAX_STRENGTH
                maximum = Manipulation.GRIPPER_OPEN
                if not minimum <= joints[5] <= maximum:
                    self.fail(f"joint6 (gripper) must be between {minimum} and {maximum} radians")
        try:
            if mode == "xyz":
                if relative:
                    pose = self.manipulation.pose
                    if not all(finite(v) for v in (*pose.position, *pose.rpy)):
                        self.fail("current arm pose is invalid; cannot calculate a relative move")
                    x, y, z = pose.x + x, pose.y + y, pose.z + z
                    roll, pitch, yaw = pose.rpy
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
