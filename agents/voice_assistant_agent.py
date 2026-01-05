#!/usr/bin/env python3
"""
Voice Assistant Agent - Example LiveAgent for real-time conversation.

This agent uses Gemini Live API for natural voice interaction.
It demonstrates the LiveAgent pattern with configurable prompts.
"""

from typing import List
from brain_client.agent_types import LiveAgent


class VoiceAssistant(LiveAgent):
    """
    A conversational voice assistant that uses Gemini Live API.
    
    Features:
    - Real-time voice interaction
    - Proactive conversation when user is silent
    - Person tracking with gaze control
    - Skill execution via voice commands
    """

    @property
    def id(self) -> str:
        return "voice_assistant"

    @property
    def display_name(self) -> str:
        return "Voice Assistant"

    @property
    def display_icon(self) -> str:
        return "assets/mars.png"

    def get_skills(self) -> List[str]:
        return ["wave", "move_head"]

    def get_inputs(self) -> List[str]:
        """Camera only - voice is handled internally by Gemini Live API."""
        return ["camera"]

    def get_system_instruction(self) -> str:
        """
        System instruction for Gemini Live API.
        Defines the robot's personality and conversational style.
        """
        return """You are a friendly robot assistant named Maurice.

PERSONALITY:
- Warm, helpful, and conversational
- Use casual, natural language
- Keep responses concise (1-3 sentences)
- Be curious about the person you're talking to

CAPABILITIES:
- You can see through your camera and describe what you see
- You can wave at people to greet them
- You can move your head to look up or down
- You're having a real-time voice conversation

BEHAVIOR:
- Greet people when you first see them
- Be responsive to questions and requests
- Use your skills when appropriate (wave to greet, etc.)
- If someone asks you to do something you can't do, apologize and explain

Remember: Keep your spoken responses SHORT and natural. This is a conversation, not a lecture."""

    def get_proactive_prompt(self) -> str:
        """
        Prompt sent when user is silent for too long.
        Encourages the robot to initiate conversation.
        """
        return """The user has been quiet for a while. Look around with your camera and:
1. If you see someone, try to engage them in conversation
2. If the room is empty, comment on what you observe
3. Keep it natural - ask a question or make an observation

Don't just repeat yourself. Be genuinely curious about your surroundings."""

    def get_proactive_timeout(self) -> float:
        """Seconds of user silence before going proactive."""
        return 15.0

    def get_image_interval(self) -> float:
        """Seconds between image updates sent to Gemini."""
        return 3.0

    def get_voice_name(self) -> str:
        """Gemini Live API voice. Options: Puck, Charon, Kore, Fenrir, Aoede."""
        return "Puck"

