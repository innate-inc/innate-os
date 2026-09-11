# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The OpenAI backend: the Responses API, reasoning summaries, prefix caching."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from brain_client.brain.llm.openai import wire
from brain_client.brain.llm.openai.context import OpenAIConversation
from brain_client.brain.llm.openai.transport import pick_transport
from brain_client.brain.llm.types import Backend

if TYPE_CHECKING:
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.brain.llm.types import Conversation
    from brain_client.core.config import BrainConfig
    from innate_proxy import ProxyClient

REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


def build(config: BrainConfig, proxy: ProxyClient | None, logger: RcutilsLogger) -> tuple[Conversation | None, Backend]:
    transport = pick_transport(proxy)
    if transport is None:
        return None, Backend.UNCONFIGURED
    backend = Backend.PROXY if proxy is not None and proxy.is_available() else Backend.OPENAI_DIRECT
    conversation = OpenAIConversation(
        transport,
        model=config.brain_model,
        effort=_effort(config.brain_thinking_level, logger),
        max_history=config.history_max_entries,
        max_image_turns=config.history_max_image_turns,
        # New per conversation: the hint only has to be stable across the turns
        # that share a prefix, and a restart starts from a cold cache anyway.
        cache_key=f"innate-brain-{uuid.uuid4().hex[:16]}",
        reference=wire.reference_turns(),
    )
    return conversation, backend


def _effort(level: str, logger: RcutilsLogger) -> str:
    """The thinking-level setting is written in the provider's own vocabulary,
    so a value carried over from Gemini ("minimal") is not one of these and
    would 400 every request. Fall back to the model default."""
    if not level or level in REASONING_EFFORTS:
        return level
    logger.warn(
        f"[Brain] Unknown openai reasoning effort {level!r} — using the model default (one of {REASONING_EFFORTS})"
    )
    return ""
