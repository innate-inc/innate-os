# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Picking the model backend the brain thinks with.

Adding a provider means adding a package beside these and one line in
:func:`pick_conversation`; nothing above this module knows which one is in use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.brain.llm import gemini, openai, openai_compat
from brain_client.brain.llm.types import Backend, Provider

if TYPE_CHECKING:
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.brain.llm.types import Conversation
    from brain_client.core.config import BrainConfig
    from innate_proxy import ProxyClient

_BUILDERS = {Provider.GEMINI: gemini.build, Provider.OPENAI: openai.build, Provider.OPENAI_COMPAT: openai_compat.build}
_KEY_HINTS = {
    Provider.GEMINI: "GEMINI_API_KEY",
    Provider.OPENAI: "OPENAI_API_KEY",
    Provider.OPENAI_COMPAT: "OPENAI_COMPAT_BASE_URL (and OPENAI_COMPAT_API_KEY if the server wants one)",
}


def pick_conversation(
    config: BrainConfig, proxy: ProxyClient | None, logger: RcutilsLogger
) -> tuple[Conversation | None, Backend]:
    """The conversation the agent will think with, and how it reaches its provider.

    None means no usable credential: the Innate service key covers every
    provider, and each also accepts its own vendor key for development.
    """
    return _BUILDERS[provider(config.brain_backend, logger)](config, proxy, logger)


def provider(name: str, logger: RcutilsLogger) -> Provider:
    """The configured provider, falling back to Gemini for an unknown name."""
    try:
        return Provider(name.strip().lower())
    except ValueError:
        logger.warn(f"[Brain] Unknown brain_backend {name!r} — using {Provider.GEMINI} (one of {list(Provider)})")
        return Provider.GEMINI


def key_hint(name: str) -> str:
    """Which vendor key would configure this provider without a service key."""
    try:
        return _KEY_HINTS[Provider(name.strip().lower())]
    except ValueError:
        return _KEY_HINTS[Provider.GEMINI]
