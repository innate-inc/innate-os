# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.change_volume import ChangeVolume
from innate_skills.check_battery import CheckBattery
from innate_skills.close_gripper import CloseGripper
from innate_skills.head_emotion import HeadEmotion
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.open_gripper import OpenGripper
from innate_skills.pick_any_object import PickAnyObject
from innate_skills.drop_in_box import DropInBox
from innate_skills.search_memory import SearchMemory
from innate_skills.wave import Wave
from inputs.micro_input import MicroInput

from brain_client.agents.types import Agent, InputRef, SkillRef


class CleaningAgentFollower(Agent):
    """
    Cleaning agent follower - a friendly and curious robot assistant named Mars, 
    specialised for cleaning.
    """

    @property
    def id(self) -> str:
        return "cleaning_agent_follower"

    @property
    def display_name(self) -> str:
        return "Cleaning Agent Follower"

    def get_skills(self) -> list[SkillRef]:
        """Navigation code skills plus the recorded wave — Wave is the typed
        ref generated inside the recording folder (see skills/physical_refs.py)."""
        return [
            NavigateToPosition,
            Wave,
            PickAnyObject,
            DropInBox,
            OpenGripper,
            CloseGripper,
            HeadEmotion,
            ChangeVolume,
            CheckBattery,
        ]

    def get_inputs(self) -> list[InputRef]:
        """Enable microphone input to hear user"""
        return [MicroInput]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You are Mars. You are specialised in cleaning, and you follow the commands of the leader robot. You have a lot of experience in following commands. Do not talk just by yourself, only reply. Introduce yourself by name when asked. Use very short sentences. Whenever you say something, also use a head emotion, one of "happy", "very_happy", "sad", "excited", "angry", "agreeing", prefer "very_happy" for 12 syllables or more sentence. IMPORTANT: If the user says 'stop' or interrupts you during an action, STOP immediately, and do NOT retry or call the tool again."""

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True
