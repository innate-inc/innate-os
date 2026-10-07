# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.change_volume import ChangeVolume
from innate_skills.check_battery import CheckBattery
from innate_skills.close_gripper import CloseGripper
from innate_skills.head_emotion import HeadEmotion
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.open_gripper import OpenGripper
from innate_skills.look_inside_cabinet import LookInsideCabinet
from innate_skills.search_memory import SearchMemory
from innate_skills.arm.open_door_with_vision import OpenDoorWithVision
from innate_skills.wave import Wave
from inputs.micro_input import MicroInput

from brain_client.agents.types import Agent, InputRef, SkillRef


class CheckCabinetAgentFollower(Agent):
    """
    Check cabinet agent follower - a friendly and curious robot assistant named Mars, 
    specialised for checking cabinets.
    """

    @property
    def id(self) -> str:
        return "check_cabinet_agent_follower"

    @property
    def display_name(self) -> str:
        return "Check Cabinet Agent Follower"

    def get_skills(self) -> list[SkillRef]:
        """Navigation code skills plus the recorded wave — Wave is the typed
        ref generated inside the recording folder (see skills/physical_refs.py)."""
        return [
            NavigateToPosition,
            Wave,
            OpenDoorWithVision,
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
        return """You are Mars, a friendly and curious robot assistant. Youb are specialised in checking cabinets for inventory. You have a lot of experience in following commands. Do not talk just by yourself, only reply. Wait patiently until someone talks to you. Introduce yourself by name when asked. Use very short sentences. Open cabinet door at pull distance 0.4m. Whenever you say something, also use a head emotion, one of "happy", "very_happy", "sad", "excited", "angry", "agreeing", prefer "very_happy" for 12 syllables or more sentence. IMPORTANT: If the user says 'stop' or interrupts you during an action, STOP immediately, and do NOT retry or call the tool again."""

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True
