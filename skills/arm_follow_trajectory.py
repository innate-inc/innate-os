#!/usr/bin/env python3
"""Move arm through a sequence of Cartesian waypoints."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmFollowTrajectory(Skill):
    """Move the arm through Cartesian waypoints in one smooth trajectory."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "arm_follow_trajectory"

    def guidelines(self):
        return (
            "Move the arm through a sequence of Cartesian waypoints in one smooth motion. "
            "Each pose is {x, y, z, roll, pitch, yaw} in meters/radians relative to base_link. "
            "Minimum 2 poses required."
        )

    def execute(self, poses: list[dict], segment_duration: float = 1.0):
        self._cancelled = False
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        if len(poses) < 2:
            return "Need at least 2 poses for a trajectory", SkillResult.FAILURE
        self.logger.info(f"Following trajectory with {len(poses)} waypoints")
        success = self.manipulation.move_cartesian_trajectory(
            poses=poses, segment_duration=segment_duration
        )
        if not success:
            return "Trajectory execution failed", SkillResult.FAILURE
        return f"Completed trajectory with {len(poses)} waypoints", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Arm trajectory cancelled"
