#!/usr/bin/env python3
"""Get current motor load/effort for all arm joints."""

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class ArmGetLoad(Skill):
    """Query current motor effort values for all 6 joints (read-only, no actuation)."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "arm_get_load"

    def guidelines(self):
        return (
            "Get current motor load/effort for all 6 arm joints. "
            "Values are percentages (-100% to 100%). Useful for detecting contact, "
            "grasp confirmation, or overload conditions. No actuation."
        )

    def execute(self):
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        loads = self.manipulation.get_motor_load()
        if loads is None:
            return "No motor load data available", SkillResult.FAILURE
        formatted = ", ".join(f"j{i+1}={v:.1f}%" for i, v in enumerate(loads))
        return f"Motor loads: {formatted}", SkillResult.SUCCESS

    def cancel(self):
        return "Nothing to cancel (read-only skill)"
