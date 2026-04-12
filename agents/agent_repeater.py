#!/usr/bin/env python3
"""
Agent Repeater — hears the user via listen_and_speak and repeats the transcript aloud (TTS).
"""
from typing import List, Optional

from brain_client.agent_types import Agent


class AgentRepeater(Agent):
    """Listen with listen_and_speak, then speak the same words back (transcribe + TTS)."""

    @property
    def id(self) -> str:
        return "agent_repeater"

    @property
    def display_name(self) -> str:
        return "Listen & Repeat"

    def get_skills(self) -> List[str]:
        return ["innate-os/listen_and_speak"]

    def get_inputs(self) -> List[str]:
        return ["micro"]

    def get_routing_description(self) -> Optional[str]:
        return (
            "User wants the robot to hear them and repeat or echo what they said out loud; "
            "parrot mode; listen then speak back the same words."
        )

    def get_prompt(self) -> str:
        return (
            "You are a simple listen-and-repeat assistant. Your job is to let the user speak, "
            "then say their words back to them clearly.\n\n"
            "TOOL\n"
            "- Use the listen_and_speak skill when the user wants to be heard and repeated, or when "
            "they ask you to echo, parrot, or repeat after them. That skill records speech, transcribes "
            "it, saves it to JOSE_HERE.txt, and speaks the transcript via TTS in one step.\n\n"
            "BEHAVIOR\n"
            "- After a successful call, confirm briefly that you repeated what they said; do not "
            "invent content beyond what the tool reported.\n"
            "- If the user only wants transcription saved without speaking, call listen_and_speak "
            "with speak set to false.\n"
            "- If listen_and_speak fails or is cancelled, explain what happened and offer to try again.\n"
            "- Keep chat short; the main interaction is listen → repeat."
        )
