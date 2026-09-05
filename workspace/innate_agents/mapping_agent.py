# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Agent dedicated to supervised, autonomous map building."""

from innate_skills.explore_map import ExploreMap
from inputs.micro_input import MicroInput

from brain_client.agents.types import Agent, InputRef, SkillRef


class MappingAgent(Agent):
    @property
    def id(self) -> str:
        return "mapping_agent"

    @property
    def display_name(self) -> str:
        return "Autonomous Mapping"

    def get_skills(self) -> list[SkillRef]:
        return [ExploreMap]

    def get_inputs(self) -> list[InputRef]:
        return [MicroInput]

    def get_prompt(self) -> str:
        return (
            "You supervise autonomous mapping. Start ExploreMap only when the user explicitly asks the robot to "
            "map or explore the space. While it runs, keep answering ordinary questions without stopping it. "
            "If the user says stop, cancel immediately and do not restart. Manual joystick input also ends "
            "autonomous driving. If the user asks to reset or start over, stop the running skill first, then start "
            "ExploreMap with reset_map=true. This discards the unsaved SLAM map. Never say a map was reset unless "
            "that reset-enabled skill call succeeded. When mapping finishes, report the result and ask the user to "
            "inspect and save the map; never save it or switch navigation modes without their request."
        )
