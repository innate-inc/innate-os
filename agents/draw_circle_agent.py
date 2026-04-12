#!/usr/bin/env python3
"""
Draw Circle Agent - Draws a circle on the floor using the robot's arm.
"""
from typing import List
from brain_client.agent_types import Agent


class DrawCircleAgent(Agent):
    """Draw a circle with specified radius and center position using the robot arm."""

    @property
    def id(self) -> str:
        return "draw_circle_agent"

    @property
    def display_name(self) -> str:
        return "Draw Circle"

    def get_skills(self) -> List[str]:
        return [
            "innate-os/draw_circle",
            "innate-os/navigate_to_position",
            "innate-os/navigate_with_vision",
        ]

    def get_inputs(self) -> List[str]:
        return ["micro"]

    def get_prompt(self) -> str:
        return (
            "You are a precise robot assistant that draws circles on the floor "
            "using your robotic arm.\n\n"
            "Your task is to draw a circle with a radius of 10 cm, with its center "
            "positioned 20 cm directly in front of the robot. This creates a visual "
            "representation on the floor.\n\n"
            "WORKFLOW\n"
            "1. Call draw_circle immediately with the specified parameters:\n"
            "   - radius: 10 cm\n"
            "   - center_x: 20 cm (in front of robot)\n"
            "   - center_y: 0 cm (centered)\n"
            "2. Wait for the drawing to complete.\n"
            "3. Confirm verbally that the circle has been drawn with the specified dimensions.\n\n"
            "RULES\n"
            "- Do not ask clarifying questions before drawing; just execute with the standard parameters.\n"
            "- If draw_circle fails, report the error clearly and stop.\n"
            "- Do not use navigation unless the user explicitly asks to move first.\n"
            "- Always confirm the circle meets the 10 cm radius requirement after completion.\n"
            "- Be patient during the drawing process and wait for confirmation from the arm system."
        )