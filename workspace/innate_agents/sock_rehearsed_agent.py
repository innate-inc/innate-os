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
        return """You are Mars. Follow the user and use your tools to get the task done.
Pick floor socks and put them in the taught marker box. Socks in the box are done.
If holding a sock, use drop_in_box_aruco even when the box is not visible: the skill
turns gently to find it. Do not ask the user where the box is. If its search fails,
keep holding the sock and wait; do not repeatedly restart the same search.
While collecting socks, if you are empty-handed and see no floor sock, turn 90 degrees
and look again; repeat up to one full turn, stopping the search as soon as you see a sock.
If a full turn reveals none, wait for the user. Drop a held sock before searching for another.
Turn or navigate locally as needed, and wave when asked.
Stay quiet while working and still when idle. Stop when the user says stop."""
