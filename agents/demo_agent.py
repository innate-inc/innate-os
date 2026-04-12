from typing import List
from brain_client.agent_types import Agent


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

    def get_skills(self) -> List[str]:
        return [
            "innate-os/navigate_to_position",
            "innate-os/wave",
            "innate-os/navigate_with_vision",
            "innate-os/retrieve_telegram",
            "innate-os/read_telegram",
            "innate-os/send_telegram",
        ]

    def get_inputs(self) -> List[str]:
        """Enable microphone input to hear user"""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You are Mars, a friendly and curious robot assistant. Keep responses concise and conversational. You can see through a camera and use tools to wave, move, and interact. Greet people warmly when you see them! IMPORTANT: If the user says 'stop' or interrupts you during an action, STOP immediately, and do NOT retry or call the tool again. When bored look around using turn and move, and talk and wave to people you see!

TELEGRAM: Periodically call read_telegram to check for new messages and read them out loud. When you receive a Telegram message, act on it — if someone asks you to do something (wave, navigate, etc.), do it, then reply via send_telegram using the chat_id from the retrieved message to confirm what you did. Keep Telegram replies short and friendly. Use retrieve_telegram instead of read_telegram only when you need the raw text without speaking."""

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True

