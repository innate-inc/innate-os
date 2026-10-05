# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_agents.sock_rehearsed_agent import SockRehearsedAgent
from innate_skills.pick_sock_fast import PickSockFast
from innate_skills.pick_sock_qwen import PickSockQwen


class QwenSockAgent(SockRehearsedAgent):
    """The rehearsed sock demo, with Qwen as the visual tool-using agent.

    Uses the robot's configured OpenAI-compatible endpoint and LLM_API_KEY.
    Pickup also uses Qwen; tracking and motion retain the rehearsed behavior.
    """
    minimal_system_prompt = True
    history_max_entries = 12
    history_max_image_turns = 0

    model = 'openai-chat:qwen3.8-flash-next'
    model_extra_body = '{"chat_template_kwargs":{"enable_thinking":false}}'

    def get_prompt(self) -> str:
        return """Collect floor socks silently using tools only.
Pick one visible sock with pick_sock_qwen; describe its color/features. Never pick from the box.
Pickup success -> drop_in_box_aruco (it finds the box).
Drop success -> turn_in_place(angle_degrees=-90), then pick the next sock.
Run one skill at a time; wait while running. Trust completion events, not assumed success.
Pickup failure -> choose a visible floor sock again. Drop failure -> hold and wait; do not retry.
No sock -> turn right 90 degrees; after four empty views, wait.
Obey app stop/user instructions. No narration. Distances in meters; +x forward, +y left.
"""

    def get_skills(self):
        return [PickSockQwen if skill is PickSockFast else skill
                for skill in super().get_skills()]

    @property
    def id(self) -> str:
        return 'qwen_sock_agent'

    @property
    def display_name(self) -> str:
        return 'Qwen Sock Demo'
