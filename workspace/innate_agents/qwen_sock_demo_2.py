# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_agents.qwen_sock_agent import QwenSockAgent
from innate_skills.drop_in_box_aruco import DropInBoxAruco
from innate_skills.pick_sock_yoloe import PickSockYoloe
from innate_skills.wave import Wave
from inputs.micro_input import MicroInput


class QwenSockDemo2(QwenSockAgent):
    """Sock collection with spoken instructions and waving."""

    wait_for_skill_completion = True

    @property
    def id(self) -> str:
        return "qwen_sock_demo_2"

    @property
    def display_name(self) -> str:
        return "Qwen Sock Demo 2"

    def get_skills(self):
        return [PickSockYoloe, DropInBoxAruco, Wave]

    def get_inputs(self):
        return [MicroInput]

    def get_prompt(self) -> str:
        return """Collect floor socks silently; tools only.
Call pick_sock_yoloe to find and pick a green floor sock.
Pickup success -> drop_in_box_aruco. Drop success -> pick_sock_yoloe. Repeat.
The skills search/turn/approach themselves. Never pick socks inside the box.
One skill at a time; wait while running. Trust completion events.
On failure, wait for user instructions; never drop after a failed pickup.
Obey spoken and app instructions, including stop. Wave when asked, after any running skill finishes.
No narration."""
