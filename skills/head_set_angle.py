#!/usr/bin/env python3
"""Set the head tilt to a specific angle."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult

HEAD_MIN_ANGLE = -25
HEAD_MAX_ANGLE = 15


class HeadSetAngle(Skill):
    """Set the head tilt to a specific angle in degrees."""

    head = Interface(InterfaceType.HEAD)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "head_set_angle"

    def guidelines(self):
        return (
            "Set the head tilt to a specific angle in degrees. "
            f"Range: {HEAD_MIN_ANGLE} (looking down) to {HEAD_MAX_ANGLE} (looking up). "
            "0 = level. Use head_emotion for expressive animations instead."
        )

    def execute(self, angle_degrees: int):
        if self.head is None:
            return "Head interface not available", SkillResult.FAILURE
        angle_degrees = int(max(HEAD_MIN_ANGLE, min(HEAD_MAX_ANGLE, angle_degrees)))
        self.logger.info(f"Setting head angle to {angle_degrees}°")
        self.head.set_position(angle_degrees)
        return f"Head set to {angle_degrees}°", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel (fire-and-forget)"
