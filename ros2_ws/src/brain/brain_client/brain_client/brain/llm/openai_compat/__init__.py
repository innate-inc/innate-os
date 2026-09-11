# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The OpenAI-compatible backend: any ``/v1/chat/completions`` server.

NVIDIA's hosted NIM API, a NIM or vLLM container on the LAN, Ollama — whatever
answers the Chat Completions wire format at ``OPENAI_COMPAT_BASE_URL``. This is
the provider for models Innate does not host, so it never goes through the
proxy: the endpoint and its key are the operator's own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.brain.llm.openai_compat import wire
from brain_client.brain.llm.openai_compat.context import OpenAICompatConversation
from brain_client.brain.llm.openai_compat.transport import Endpoint, direct_transport
from brain_client.brain.llm.types import Backend

if TYPE_CHECKING:
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.brain.llm.types import Conversation
    from brain_client.core.config import BrainConfig
    from innate_proxy import ProxyClient

THINKING_LEVELS = ("off", "low", "medium", "high")


def build(config: BrainConfig, proxy: ProxyClient | None, logger: RcutilsLogger) -> tuple[Conversation | None, Backend]:
    endpoint = Endpoint.from_env()
    if endpoint is None:
        return None, Backend.UNCONFIGURED
    conversation = OpenAICompatConversation(
        direct_transport(endpoint),
        model=config.brain_model,
        thinking=_thinking_fields(config.brain_thinking_level, logger),
        max_history=config.history_max_entries,
        max_image_turns=config.history_max_image_turns,
        reference=wire.reference_turns(),
    )
    return conversation, Backend.OPENAI_COMPAT


def _thinking_fields(level: str, logger: RcutilsLogger) -> dict:
    """The request fields that set the thinking level on this kind of server.

    Chat Completions has no single knob: ``off`` is the chat-template switch
    vLLM and NIM expose for models that reason by default (Nemotron 3, Qwen3),
    and low/medium/high is ``reasoning_effort`` where the server honours it.
    Anything else (Gemini's "minimal") means the server's default.
    """
    if not level:
        return {}
    if level == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    if level in THINKING_LEVELS:
        return {"reasoning_effort": level}
    logger.warn(
        f"[Brain] Unknown openai_compat thinking level {level!r} — using the server default (one of {THINKING_LEVELS})"
    )
    return {}
