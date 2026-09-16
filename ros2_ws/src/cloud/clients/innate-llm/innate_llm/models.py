# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The model catalog: what each model accepts, as data.

A row's ``name`` is a prefix — ``claude-sonnet-4-6`` covers its dated
snapshots — and the longest matching prefix wins, so a family default
(``claude-``) sits under its exceptions. A name no row covers gets its
vendor's defaults: a LAN server's tag is never in here, and the newest
generation of every vendor takes the full ladder.

``vendor:name`` in a setting names the vendor outright; a bare name takes the
vendor of its catalog row, so the retired ``gemini-3.6-flash`` setting still
works. With a ``base_url`` any bare name is the server's own — an Ollama tag
such as ``qwen2.5:7b`` carries a colon of its own.
"""

from __future__ import annotations

from dataclasses import replace

from innate_llm.types import LADDER, Model, Thinking, Vendor

_NO_XHIGH = frozenset(LADDER) - {Thinking.XHIGH}
_NO_MINIMAL = frozenset(LADDER) - {Thinking.MINIMAL}

CATALOG: tuple[Model, ...] = (
    # Family rows: the current generation takes the defaults — adaptive thinking, the full ladder.
    Model("gemini-", Vendor.GOOGLE),  # Gemini 3.6 Flash and kin
    Model("claude-", Vendor.ANTHROPIC),  # Fable 5.1 / 5, Opus 5 / 4.8 / 4.7, Sonnet 5
    Model("gpt-", Vendor.OPENAI, effort_with_tools=False),  # GPT-6 Astra, GPT-5.6 Sol / Terra / Luna
    # OpenAI's o-series: retired names, kept so a bare "o3-mini" in an old settings.yaml still routes to OpenAI.
    Model("o1", Vendor.OPENAI),
    Model("o3", Vendor.OPENAI),
    Model("o4", Vendor.OPENAI),
    # Exceptions, longest prefix first in effect.
    Model("gemini-3.8-flash", Vendor.GOOGLE, thinking=_NO_MINIMAL),  # 400s on thinkingLevel MINIMAL
    Model("claude-opus-4-6", Vendor.ANTHROPIC, thinking=_NO_XHIGH),
    Model("claude-sonnet-4-6", Vendor.ANTHROPIC, thinking=_NO_XHIGH),
    Model("claude-haiku-4-5", Vendor.ANTHROPIC, budget_thinking=True),
    Model("claude-3-", Vendor.ANTHROPIC, budget_thinking=True),
    # opus/sonnet 4.0 through 4.5, aliases and dated snapshots alike (4-2 covers 4-2025xxxx).
    *(
        Model(f"claude-{tier}-4-{n}", Vendor.ANTHROPIC, budget_thinking=True)
        for tier in ("opus", "sonnet")
        for n in range(6)
    ),
)

_VENDORS = frozenset(vendor.value for vendor in Vendor)


def lookup(name: str) -> Model | None:
    """The catalog row covering ``name`` (longest prefix), or None."""
    rows = [row for row in CATALOG if name.startswith(row.name)]
    return max(rows, key=lambda row: len(row.name)) if rows else None


def split_spec(spec: str, *, base_url: str = "") -> tuple[Vendor, str]:
    """``"vendor:name"`` -> (vendor, name); a bare name infers its vendor (see the module docstring)."""
    spec = spec.strip()
    prefix, colon, name = spec.partition(":")
    if colon and prefix in _VENDORS:
        return Vendor(prefix), name
    if base_url:
        return Vendor.OPENAI_CHAT, spec
    if colon:
        raise ValueError(f"unknown LLM vendor {prefix!r} in {spec!r} (one of {sorted(_VENDORS)})")
    row = lookup(spec)
    return (row.vendor if row is not None else Vendor.GOOGLE), spec


def resolve(spec: str, *, base_url: str = "") -> Model:
    """The model a setting names, with its catalog facts; the setting's vendor wins over the row's."""
    vendor, name = split_spec(spec, base_url=base_url)
    row = lookup(name)
    return replace(row, name=name, vendor=vendor) if row is not None else Model(name, vendor)
