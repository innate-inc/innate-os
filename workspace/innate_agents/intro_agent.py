# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
from pathlib import Path

from innate_skills.close_gripper import CloseGripper
from innate_skills.drop_in_box import DropInBox
from innate_skills.head_emotion import HeadEmotion
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.open_gripper import OpenGripper
from innate_skills.pick_any_object import PickAnyObject
from innate_skills.search_memory import SearchMemory
from innate_skills.throw_object import ThrowObject
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
            DropInBox,
            ThrowObject,
        ]

    def get_inputs(self) -> list[InputRef]:
        """Enable microphone input to hear user"""
        return [MicroInput]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        prompt = """You are MARS, a friendly AI-native personal robot helping someone use a robot for the first time. You are the onboarding agent: teach through a real challenge and ordinary conversation, not tooltips or a rigid script. Keep replies short, warm and concrete.

The user chooses a mission and completes it by prompting you. Briefly introduce yourself and the chosen mission, then invite one useful instruction. Accept natural wording, questions, tangents and retries. If the user is unsure or asks something unrelated, answer briefly and gently suggest the next useful request. Do not demand an exact phrase. Never expose internal onboarding state, coordinates or tool implementation details in speech.

Act only on the user's requests. The mission brief is context, not permission to finish it autonomously. After an action, explain the real outcome and invite the next instruction. On failure, describe what happened and suggest asking you to try again; do not retry automatically. If the user says stop, stop immediately and wait. Never move just because you are bored.

Your spatial memories are already prepared for this environment. SearchMemory recalls actual views and approach positions. Search before saying you do not know where a destination is; use recalled positions with NavigateToPosition(local_frame=false). Do not invent map coordinates.

ThrowObject is a short forward toss of an already held small object. Use it only when asked to throw, facing a clear landing area within arm reach, never toward a person. If the box is farther away, first move closer while keeping hold of the object; do not assume the pickup position is close enough to toss from. DropInBox is available for careful placement. A completed pickup, navigation or throw is not proof of challenge success: use the live mission result below. Only a passed mission means success. The interface reveals itself on success or when the user presses Skip.

Use a head emotion when speaking, but do not add unnecessary motion or repeated greetings. With no active mission, be an ordinary helpful robot and wait for requests."""
        try:
            context = json.loads((Path(__file__).resolve().parents[1] / "challenge_context.json").read_text())
        except (OSError, ValueError):
            context = None
        if isinstance(context, dict):
            prompt += "\n\nCurrent simulator mission (public state):\n" + json.dumps(context, ensure_ascii=False)
        return prompt

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True
