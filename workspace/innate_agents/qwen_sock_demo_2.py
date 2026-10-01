# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_agents.qwen_sock_agent import QwenSockAgent
from innate_skills.drop_in_box_aruco import DropInBoxAruco
from innate_skills.pick_sock_qwen_search import PickSockQwenSearch


class QwenSockDemo2(QwenSockAgent):
    """Two-skill sock collection: search/pick, then marker drop."""

    @property
    def id(self) -> str:
        return "qwen_sock_demo_2"

    @property
    def display_name(self) -> str:
        return "Qwen Sock Demo 2"

    def get_skills(self):
        return [PickSockQwenSearch, DropInBoxAruco]

    def get_prompt(self) -> str:
        return """Collect floor socks silently; tools only.
Call pick_sock_qwen_search; describe the sock's color if visible, otherwise use its default.
Pickup success -> drop_in_box_aruco. Drop success -> pick_sock_qwen_search. Repeat.
The skills search/turn/approach themselves. Never pick socks inside the box.
One skill at a time; wait while running. Trust completion events.
On failure, wait for user instructions; never drop after a failed pickup.
Obey app stop/user instructions. No narration."""
