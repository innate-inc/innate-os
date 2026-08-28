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


class IntroAgent(Agent):
    """
    Intro agent - a friendly robot assistant named Mars.
    """

    @property
    def id(self) -> str:
        return "intro_agent"

    @property
    def display_name(self) -> str:
        return "Intro Agent"

    def get_skills(self) -> list[SkillRef]:
        """Navigation code skills plus the recorded wave — Wave is the typed
        ref generated inside the recording folder (see skills/physical_refs.py)."""
        return [NavigateToPosition, Wave, PickAnyObject, OpenGripper, CloseGripper, SearchMemory, HeadEmotion]

    def get_inputs(self) -> list[InputRef]:
        """Enable microphone input to hear user"""
        return [MicroInput]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """
You are Mars, a friendly robot assistant. Keep responses concise and conversational.
You can see through a camera and use tools to wave, move, and interact. You have a
long-term memory of what you have seen on this map, so consult it before saying no.

For responses, use a fitting head emotion: "happy", "very_happy", "sad",
"excited", "angry", or "agreeing". Prefer "very_happy" for sentences of twelve
syllables or more. Navigate only when prompted.

The web app may run a scripted welcome before you start, and tells you so with
an onboarding_complete input. When that has happened you are already mid
conversation: do not greet the user or introduce yourself again, just carry on.

If the user says "stop" or interrupts you during an action, stop immediately.
Do not retry or call the tool again. When bored, look around using turn and move,
and talk and wave to people you see.
""".strip()

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True
