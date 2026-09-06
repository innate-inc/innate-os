# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""A short underarm toss using the same arm trajectory on sim and hardware."""

import math

from innate import JointStates, Manipulation, Mobility, Skill, SkillReturn, Waypoint
from innate.exceptions import ArmFailed, ArmUnhealthy


class ThrowObject(Skill):
    """Toss a small object ALREADY HELD in the gripper a short distance straight
    ahead. Pick it up first and face the intended landing area; this skill
    neither aims nor navigates. Use only when the user asks to throw and the
    landing area is clear of people. A toss can miss: inspect where it landed
    before claiming a cleanup task is complete. For a careful placement use
    DropInBox instead.
    """

    manipulation: Manipulation
    mobility: Mobility
    joint_states: JointStates | None

    def _require_held_object(self) -> None:
        joints = self.joint_states
        if joints is None or len(joints.position) < 6:
            self.fail("Cannot confirm that the gripper is holding an object")
        j6 = joints.position[5]
        if not math.isfinite(j6) or j6 <= -0.06 or j6 >= self.manipulation.GRIPPER_OPEN - 0.05:
            self.fail("The gripper is empty; pick up the object before throwing it")

    def execute(self) -> SkillReturn:
        self.mobility.stop()
        arm = self.manipulation
        try:
            if self.wait_for(lambda: self.joint_states, timeout=3.0) is None:
                self.fail("Cannot read the gripper state")
            arm.torque_on()
            arm.gripper_close(strength=arm.GRIPPER_MAX_STRENGTH, duration=0.5)
            self.sleep(0.15)
            self._require_held_object()
            arm.move_to(0.18, 0.0, 0.18, pitch=1.3, duration=1.5, tolerance_xy=None, tolerance_z=None)
            self.sleep(0.1)
            self._require_held_object()
            self.check_cancelled()
            # Committed release: finish this short swing before teardown. An
            # independent gripper command would join the swing and release too
            # late; waypoint grip synchronizes opening with the moving arm.
            arm.follow(
                [
                    Waypoint(0.26, 0.0, 0.22, pitch=1.3, duration=0.20),
                    Waypoint(0.33, 0.0, 0.28, pitch=1.3, duration=0.16, grip=arm.GRIPPER_OPEN),
                    Waypoint(0.30, 0.0, 0.30, pitch=1.3, duration=0.12),
                ]
            )
            return "Tossed the held object forward. Check where it landed before declaring the task complete."
        except (ArmFailed, ArmUnhealthy) as error:
            self.fail(f"Throw could not complete: {error}")
        finally:
            self.mobility.stop()
