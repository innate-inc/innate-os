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

    # A few recent actions/results, with only the current camera observation.
    history_max_entries = 12
    history_max_image_turns = 0

    model = "openai-chat:qwen3.8-flash-next-iq4-xs-mtp3"
    model_extra_body = '{"chat_template_kwargs":{"enable_thinking":false}}'

    def get_skills(self):
        return [PickSockQwen if skill is PickSockFast else skill for skill in super().get_skills()]

    @property
    def id(self) -> str:
        return "qwen_sock_agent"

    @property
    def display_name(self) -> str:
        return "Qwen Sock Demo"
