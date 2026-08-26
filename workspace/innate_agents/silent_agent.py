# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.close_gripper import CloseGripper
from innate_skills.head_emotion import HeadEmotion
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.open_gripper import OpenGripper
from innate_skills.pick_any_object import PickAnyObject
from innate_skills.search_memory import SearchMemory
from innate_skills.wave import Wave
from inputs.micro_input import MicroInput

from brain_client.agents.types import Agent, InputRef, SkillRef


class SilentAgent(Agent):
    """
    Demo agent - a friendly and curious robot assistant named Mars.
    """

    @property
    def id(self) -> str:
        return "silent_agent"

    @property
    def display_name(self) -> str:
        return "Silent Agent"

    def get_skills(self) -> list[SkillRef]:
        """Skills"""
        return [HeadEmotion]

    def get_inputs(self) -> list[InputRef]:
        """Enable microphone input to hear user"""
        return [MicroInput]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """Accompany every speach with head_emotion, one of "happy", "very_happy", "sad", "excited", "angry", "agreeing", prefer "very_happy" for 12 syllables or more sentence. """

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return False
