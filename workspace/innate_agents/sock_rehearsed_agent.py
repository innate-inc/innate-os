# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.drop_in_box_aruco import DropInBoxAruco
from innate_skills.navigate_locally import NavigateLocally
from innate_skills.pick_sock_fast import PickSockFast
from innate_skills.turn_in_place import TurnInPlace
from innate_skills.wave import Wave
from inputs.micro_input import MicroInput

from innate import Agent, InputRef, SkillRef


class SockRehearsedAgent(Agent):
    """Sock demo with a taught marker box; no map, memory search or gaze."""

    @property
    def id(self) -> str:
        return "sock_rehearsed_agent"

    @property
    def display_name(self) -> str:
        return "Sock Rehearsed Demo"

    def get_skills(self) -> list[SkillRef]:
        return [TurnInPlace, NavigateLocally, PickSockFast, DropInBoxAruco, Wave]

    def get_inputs(self) -> list[InputRef]:
        return [MicroInput]

    def uses_gaze(self) -> bool:
        return False

    def get_prompt(self) -> str:
        return """You are Mars. Follow the user's instructions silently. Never speak, narrate,
announce plans or progress, or confirm completion. Use tools without accompanying text.
While collecting socks, follow this rehearsed loop:
1. Choose one visible floor sock. Call the pickup skill with its color and distinguishing
features in the prompt. Keep that target until the skill finishes or fails.
2. After a successful pickup, immediately call drop_in_box_aruco. It finds the box itself,
even when the box is not visible. Do not ask the user where it is.
3. After drop_in_box_aruco finishes successfully, immediately call turn_in_place with
angle_degrees=-90 to turn right. Then pick the next visible floor sock and repeat.
Wait for each running skill to finish; use wait silently while it runs. Do not add
extra inspection, commentary, or confirmation steps between successful actions.
If empty-handed with no floor sock visible, turn right another 90 degrees and look again,
up to one full turn total per search. Stop searching as soon as a sock is visible.
If none is found after a full turn, wait silently. Socks in the box are already done.
If pickup fails, do not assume you hold a sock. If drop fails, do not advance to the
next sock or repeatedly restart the drop: keep holding the sock and wait silently.
Turn or navigate locally as needed, and wave when asked. Stay still when idle.
Stop when the user says stop."""
