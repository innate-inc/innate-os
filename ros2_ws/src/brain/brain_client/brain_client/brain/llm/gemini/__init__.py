# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The Gemini backend: native REST, thought summaries, implicit prefix caching."""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.brain.llm.gemini import wire
from brain_client.brain.llm.gemini.context import GeminiConversation
from brain_client.brain.llm.gemini.transport import pick_transport
from brain_client.brain.llm.types import Backend

if TYPE_CHECKING:
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.brain.llm.types import Conversation
    from brain_client.core.config import BrainConfig
    from innate_proxy import ProxyClient

THINKING_LEVELS = ("minimal", "low", "medium", "high")


def build(config: BrainConfig, proxy: ProxyClient | None, logger: RcutilsLogger) -> tuple[Conversation | None, Backend]:
    transport, backend = pick_transport(proxy)
    if transport is None:
        return None, backend
    conversation = GeminiConversation(
        transport,
        model=config.brain_model,
        thinking_level=_thinking_level(config.brain_thinking_level, logger),
        max_history=config.history_max_entries,
        max_image_turns=config.history_max_image_turns,
        reference=wire.reference_turns(),
    )
    return conversation, backend


def _thinking_level(level: str, logger: RcutilsLogger) -> str:
    """An unknown level would 400 every request; fall back to the model default."""
    if not level or level in THINKING_LEVELS:
        return level
    logger.warn(f"[Brain] Unknown gemini thinking level {level!r} — using the model default (one of {THINKING_LEVELS})")
    return ""
