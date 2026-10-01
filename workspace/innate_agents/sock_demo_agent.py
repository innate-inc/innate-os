# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.drop_in_box_fast import DropInBoxFast
from innate_skills.navigate_locally import NavigateLocally
from innate_skills.pick_sock_fast import PickSockFast
from innate_skills.turn_in_place import TurnInPlace
from innate_skills.wave import Wave
from inputs.micro_input import MicroInput

from innate import Agent, InputRef, SkillRef


class SockDemoAgent(Agent):
    """Local sock cleanup demo, no memory search and no person-tracking gaze."""

    @property
    def id(self) -> str:
        return "sock_demo_agent"

    @property
    def display_name(self) -> str:
        return "Sock Demo"

    def get_skills(self) -> list[SkillRef]:
        return [TurnInPlace, NavigateLocally, PickSockFast, DropInBoxFast, Wave]

    def get_inputs(self) -> list[InputRef]:
        return [MicroInput]

    def uses_gaze(self) -> bool:
        return False

    def get_prompt(self) -> str:
        return """You are Mars. Follow the user and use your tools to get the task done.
Pick socks up from the floor and put them in the box. Socks already in the box are done.
Turn or navigate locally as needed, and wave when asked.
Stay quiet while working and still when idle. Stop when the user says stop."""
