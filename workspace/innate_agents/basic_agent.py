# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.explore_map import ExploreMap
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.navigate_with_vision import NavigateWithVision
from innate_skills.search_memory import SearchMemory
from inputs.micro_input import MicroInput

from brain_client.agents.types import Agent, InputRef, SkillRef


class BasicAgent(Agent):
    """
    Default directive for the robot.
    Provides a basic professional personality and enables navigation primitives.
    """

    @property
    def id(self) -> str:
        return "basic_agent"

    @property
    def display_name(self) -> str:
        return "Basic Navigation"

    def get_skills(self) -> list[SkillRef]:
        """The skills this directive can use — classes for code skills;
        physical skills (no class) stay id strings like "local/pick_socks"."""
        return [NavigateToPosition, NavigateWithVision, ExploreMap, SearchMemory]

    def get_inputs(self) -> list[InputRef]:
        """Enable microphone input to hear user"""
        return [MicroInput]

    def get_prompt(self) -> str:
        return ""
