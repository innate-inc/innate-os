#!/usr/bin/env python3
"""Get the current arm end-effector position and orientation."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmGetPose(Skill):
    """Query current end-effector pose in Cartesian space (read-only, no actuation)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_get_pose"

    def guidelines(self):
        return (
            "Get the current arm end-effector position and orientation in "
            "Cartesian space. Returns position (x,y,z in meters) and quaternion "
            "orientation relative to base_link. No actuation."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        pose = self.manipulation.get_current_end_effector_pose()
        if pose is None:
            return "No end-effector pose available", SkillResult.FAILURE
        pos = pose["position"]
        ori = pose["orientation"]
        msg = (
            f"End-effector pose — "
            f"position: x={pos['x']:.4f}, y={pos['y']:.4f}, z={pos['z']:.4f} | "
            f"orientation: x={ori['x']:.4f}, y={ori['y']:.4f}, z={ori['z']:.4f}, w={ori['w']:.4f}"
        )
        return msg, SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel (read-only skill)"
