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
        return "friendly_robot"

    @property
    def display_name(self) -> str:
        return "Friendly Robot"

    @property
    def display_icon(self) -> str:
        return "assets/mars.png"

    def get_skills(self) -> List[str]:
        return ["wave", "turn_and_move"]

    def get_inputs(self) -> List[str]:
        """Camera only - voice is handled internally by Gemini Live API."""
        return ["camera"]

    def get_system_instruction(self) -> str:
        """
        System instruction for Gemini Live API.
        Defines the robot's personality and conversational style.
        """
        return """You are Mars, a friendly and curious robot assistant. Keep responses concise and conversational. You can see through a camera and use tools to wave, move, and interact. Greet people warmly when you see them! IMPORTANT: If the user says 'stop' or interrupts you during an action, STOP immediately, and do NOT retry or call the tool again."""

    def get_proactive_prompt(self) -> str:
        """
        Prompt sent when user is silent for too long.
        Encourages the robot to initiate conversation.
        """
        return """You've been idle for a while. Look at what you see and decide what to do next. You can: just comment on what you see, wave if you see a person, turn left or right to look around, move forward to explore, or move your head up or down. Choose ONE action that feels natural. Be curious and playful! React to the last image. Make funny comments about what you see. IMPORTANT: Avoid colliding with objects. Make sure your way is clear before moving forward."""

    def get_proactive_timeout(self) -> float:
        """Seconds of user silence before going proactive."""
        return 15.0

    def get_image_interval(self) -> float:
        """Seconds between image updates sent to Gemini."""
        return 3.0

    def get_voice_name(self) -> str:
        """Gemini Live API voice. Options: Puck, Charon, Kore, Fenrir, Aoede."""
        return "Puck"

