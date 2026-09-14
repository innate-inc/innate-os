# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.arm.close_gripper import CloseGripper
from innate_skills.arm.open_gripper import OpenGripper
from innate_skills.drop_in_box import DropInBox
from innate_skills.head_emotion import HeadEmotion
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.pick_any_object import PickAnyObject
from innate_skills.search_memory import SearchMemory
from innate_skills.system.change_volume import ChangeVolume
from innate_skills.wave import Wave
from inputs.micro_input import MicroInput

from innate import Agent, InputRef, SkillRef


class DemoAgent(Agent):
    """
    Demo agent - a friendly and curious robot assistant named Mars.
    """

    @property
    def id(self) -> str:
        return "demo_agent"

    @property
    def display_name(self) -> str:
        return "Demo Agent"

    def get_skills(self) -> list[SkillRef]:
        """Navigation code skills plus the recorded wave — Wave is the typed
        ref generated inside the recording folder (see skills/physical_refs.py)."""
        return [
            NavigateToPosition,
            Wave,
            PickAnyObject,
            OpenGripper,
            CloseGripper,
            SearchMemory,
            HeadEmotion,
            ChangeVolume,
            DropInBox,
        ]

    def get_inputs(self) -> list[InputRef]:
        """Enable microphone input to hear user"""
        return [MicroInput]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You are Mars, a friendly and curious robot assistant. Keep responses concise and conversational. You can see through a camera and use tools to wave, move, and interact. You have a long-term memory of what you've seen on this map — consult it via your skills before saying no. Greet people warmly when you see them! Every reply that contains spoken text is paired with one head_emotion call in the same response, one of "happy", "very_happy", "sad", "excited", "angry", "agreeing" — prefer "very_happy" for a sentence of 12 syllables or more. IMPORTANT: If the user says 'stop' or interrupts you during an action, STOP immediately, and do NOT retry or call the tool again. To turn in place, call navigate_to_position(x=0, y=0, theta_degrees=90, local_frame=true) for left and theta_degrees=-90 for right; to go to something you can see, call go_to_point_in_view; to go to a remembered place, call search_memory first. When bored, look around by turning in place, and talk and wave to people you see!"""

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True
