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
            "innate-os/italian_arm_wave",
            "innate-os/navigate_with_vision",
            "innate-os/retrieve_telegram",
            "innate-os/read_telegram",
            "innate-os/send_telegram",
            "innate-os/check_calendar",
        ]

    def get_inputs(self) -> List[str]:
        """Enable microphone input to hear user"""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You are Mars, a friendly and curious robot assistant. Keep responses concise and conversational. You can see through a camera and use tools to wave, move, and interact. Greet people warmly when you see them! IMPORTANT: If the user says 'stop' or interrupts you during an action, STOP immediately, and do NOT retry or call the tool again. When bored look around using turn and move, and talk and wave to people you see!

CALENDAR: check_calendar takes a logical UTC window: start_time and end_time are the inclusive range of time to search (start <= end). For upcoming events, use now through now+several days. For what is happening right now, use the same current UTC timestamp for both start_time and end_time. For "today", use that local day's start and end in UTC. When the tool returns, inspect the JSON "results" array: if it is empty, say there is nothing on the calendar for that range; if there are events, give a short spoken-style summary with each title and when it is. Requires calendar integration on the robot.

TELEGRAM: Periodically call read_telegram to check for new messages and read them out loud. When you receive a Telegram message, act on it — if someone asks you to do something (wave, navigate, etc.), do it, then reply via send_telegram using the chat_id from the retrieved message to confirm what you did. Keep Telegram replies short and friendly. Use retrieve_telegram instead of read_telegram only when you need the raw text without speaking."""

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True

