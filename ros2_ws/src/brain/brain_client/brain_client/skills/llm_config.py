# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The model skills ask by default — what the skills server was launched with, else the environment.

The server sets it once at boot from its ``llm_*`` parameters (the brain's settings, handed
over by the launch file); ``innate.llm`` reads it, so a model chosen in Settings reaches skills
the way it reaches the brain. The environment is the fallback for anything that runs outside
the server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from innate_llm.configure import DEFAULT_MODEL


@dataclass(frozen=True)
class LlmConfig:
    model: str
    base_url: str = ""
    extra_body: str = ""


def _from_environment() -> LlmConfig:
    return LlmConfig(
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        base_url=os.environ.get("LLM_BASE_URL", ""),
        extra_body=os.environ.get("LLM_EXTRA_BODY", ""),
    )


_current = _from_environment()


def configured() -> LlmConfig:
    return _current


def set_configured(config: LlmConfig) -> None:
    global _current
    _current = config
