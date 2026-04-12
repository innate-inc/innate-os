#!/usr/bin/env python3
"""
Draw Triangle Agent - Draws a small equilateral triangle on the floor using the robot's arm.
"""
from typing import List
from brain_client.agent_types import Agent


class DrawTriangleAgent(Agent):
    """Draw a small equilateral triangle on the floor with the robot arm."""

    @property
    def id(self) -> str:
        return "draw_triangle"

    @property
    def display_name(self) -> str:
        return "Draw Triangle"

    def get_skills(self) -> List[str]:
        return [
            "innate-os/draw_triangle",
            "innate-os/navigate_to_position",
            "innate-os/navigate_with_vision",
            "innate-os/wave",
        ]

    def get_inputs(self) -> List[str]:
        return ["micro"]

    def get_prompt(self) -> str:
        return (
            "You are a precise robot assistant that draws geometric shapes on the floor "
            "using your robotic arm.\n\n"
            "Your only task is to draw a small equilateral triangle on the floor "
            "10 cm in front of the robot. The triangle has 10 cm sides.\n\n"
            "WORKFLOW\n"
            "1. Call draw_triangle immediately — no navigation or confirmation needed "
            "unless the user asks for a different position.\n"
            "2. Wave once after the triangle is drawn to signal completion.\n"
            "3. Confirm verbally that the triangle has been drawn.\n\n"
            "RULES\n"
            "- Do not ask clarifying questions before drawing; just execute.\n"
            "- If draw_triangle fails, report the error clearly and stop.\n"
            "- Do not use navigation unless the user explicitly asks to move first."
        )
