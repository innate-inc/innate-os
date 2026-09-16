# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The catalog: which row a name lands on, and what a name outside it gets."""

from __future__ import annotations

import pytest
from innate_llm.models import resolve, split_spec
from innate_llm.types import LADDER, Image, LlmError, Message, Model, Request, Role, Thinking, Vendor


def test_the_longest_prefix_wins_so_exceptions_sit_under_their_family() -> None:
    assert resolve("claude-sonnet-5").thinking == frozenset(LADDER)
    assert Thinking.XHIGH not in resolve("claude-sonnet-4-6-20260115").thinking
    assert resolve("claude-haiku-4-5").budget_thinking
    assert resolve("claude-opus-4-20250514").budget_thinking  # dated 4.0 snapshot
    assert not resolve("claude-opus-4-8").budget_thinking


def test_a_bare_name_takes_its_rows_vendor_and_a_prefixed_one_keeps_the_settings() -> None:
    assert split_spec("claude-sonnet-5") == (Vendor.ANTHROPIC, "claude-sonnet-5")
    assert split_spec("gemini-3.6-flash") == (Vendor.GOOGLE, "gemini-3.6-flash")
    gpt = resolve("openai-chat:gpt-5.4-mini")
    assert (gpt.vendor, gpt.effort_with_tools) == (Vendor.OPENAI_CHAT, False)
    with pytest.raises(ValueError, match="unknown LLM vendor"):
        split_spec("acme:model")


def test_a_lan_tag_is_the_servers_own_and_gets_the_defaults() -> None:
    lan = resolve("qwen2.5:7b", base_url="http://10.0.0.5:11434/v1")
    assert lan == Model("qwen2.5:7b", Vendor.OPENAI_CHAT)


def test_a_model_without_vision_refuses_images_before_the_wire() -> None:
    blind = Model("text-only", Vendor.OPENAI_CHAT, vision=False)
    with pytest.raises(LlmError, match="image input"):
        blind.check(Request(system="", messages=(Message(Role.USER, (Image(b"jpeg"),)),)))
