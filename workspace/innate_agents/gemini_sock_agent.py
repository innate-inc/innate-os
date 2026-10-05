# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_agents.sock_rehearsed_agent import SockRehearsedAgent
from innate_skills.pick_sock_fast import PickSockFast
from innate_skills.pick_sock_gemini import PickSockGemini


class GeminiSockAgent(SockRehearsedAgent):
    """Silent Gemini sock demo with local approach motion and no microphone."""

    model = "google:gemini-3.6-flash"
    model_extra_body = "{}"

    @property
    def id(self) -> str:
        return "gemini_sock_agent"

    @property
    def display_name(self) -> str:
        return "Gemini Sock Demo"

    def get_skills(self):
        return [PickSockGemini if skill is PickSockFast else skill for skill in super().get_skills()]
