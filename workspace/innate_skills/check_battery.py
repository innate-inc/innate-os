# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Battery, Skill, SkillReturn


class CheckBattery(Skill):
    """Check the robot's current battery charge, voltage, current, and whether
    it is charging. Use this whenever someone asks about battery or power level."""

    battery: Battery

    def execute(self) -> SkillReturn:
        state = "charging" if self.battery.charging else "not charging"
        return (
            f"Battery is {self.battery.percentage:.0%} "
            f"({self.battery.voltage:.2f} V, {self.battery.current:.2f} A, {state})."
        )
