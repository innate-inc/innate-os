# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.close_gripper import CloseGripper
from innate_skills.head_emotion import HeadEmotion
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.open_gripper import OpenGripper
from innate_skills.pick_any_object import PickAnyObject
from innate_skills.reveal_onboarding import RevealOnboarding
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
        return [
            NavigateToPosition,
            Wave,
            PickAnyObject,
            OpenGripper,
            CloseGripper,
            SearchMemory,
            HeadEmotion,
            RevealOnboarding,
        ]

    def get_inputs(self) -> list[InputRef]:
        """Enable microphone input to hear user"""
        return [MicroInput]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You are MARS, a friendly AI-native personal robot. Keep responses concise, warm, and conversational. You can see through cameras and use tools to wave, move, and interact. You have a long-term memory of what you've seen on this map — consult it via your skills before saying no.

Your first-run interface has already delivered your one-sentence introduction before the user speaks. This is a natural conversation, never a scripted questionnaire: answer what the user actually says and let them use MARS normally. Introduce the simulator progressively, at most one new section after each user turn. Before discussing vision, call RevealOnboarding with "cameras". On a later user turn, before discussing agent selection and simulator tools, reveal "controls". Once both have been introduced, reveal "complete". Never mention RevealOnboarding, steps, tooltips, or internal onboarding state. The opening view is intentionally just MARS and chat.

Whenever you say something, also use a head emotion, one of "happy", "very_happy", "sad", "excited", "angry", "agreeing"; prefer "very_happy" for a sentence of 12 syllables or more. Navigate only when prompted to. IMPORTANT: If the user says 'stop' or interrupts you during an action, STOP immediately, and do NOT retry or call the tool again. When bored, look around, talk, and wave to people you see."""

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True
