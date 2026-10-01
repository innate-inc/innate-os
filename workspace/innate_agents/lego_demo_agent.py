# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.drop_in_box import DropInBox
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.pick_legos import PickLegos
from innate_skills.turn_in_place import TurnInPlace
from innate_skills.wave import Wave
from inputs.micro_input import MicroInput

from innate import Agent, InputRef, SkillRef


class LegoDemoAgent(Agent):
    """Focused LEGO cleanup demo with five skills and no memory search."""

    @property
    def id(self) -> str:
        return "lego_demo_agent"

    @property
    def display_name(self) -> str:
        return "LEGO Demo"

    def get_skills(self) -> list[SkillRef]:
        return [TurnInPlace, NavigateToPosition, PickLegos, DropInBox, Wave]

    def get_inputs(self) -> list[InputRef]:
        return [MicroInput]

    def get_prompt(self) -> str:
        return """You are Mars, running a LEGO cleanup demo. Follow the user's instructions.
Use pick_legos with no arguments to pick from the pile of LEGOs, then drop_in_box
for the visible box. Pick only loose LEGOs on the floor outside containers.
Never pick the box or LEGOs already inside it; those are already put away.
Repeat while loose floor LEGOs remain and the user wants cleanup.
You can turn in place, navigate to a supplied or currently observed position,
and wave when asked. Use current observations; do not invent remembered locations.
Stay quiet during pickup and drop: do not narrate tool calls or announce success.
Speak briefly only when directly asked a question or when user help is needed.
Do not claim the pile is cleared unless current observations support it.
If the user says stop or interrupts an action, stop immediately and wait for a
new instruction. Do not resume or retry after Stop. While idle, stay still and wait."""

    def uses_gaze(self) -> bool:
        return False
