# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The model a skill asks about what it sees, declared like any interface::

    class PickAnyObject(Skill):
        llm: Llm                                   # the model the robot is set to
        llm: Llm = Llm("google:gemini-3.5-flash")  # a skill tuned to one model pins it

The robot's default is what the skills server was launched with (the brain's
``llm_model`` setting), else the environment; a pinned one is reached the
same way — the Innate proxy, a vendor key, or an ``llm_base_url`` server.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from innate_llm import Image, Message, Request, Role, Text, configure
from innate_llm.configure import DEFAULT_MODEL

from brain_client.skills.types import cancellable_sleep

if TYPE_CHECKING:
    from innate_llm import Llm as Route
    from innate_llm import Provider

_TIMEOUT_SECS = 60.0


class Llm:
    def __init__(self, model: str | None = None, *, base_url: str = "", extra_body: str = ""):
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self._base_url = base_url
        self._extra_body = extra_body
        self._route: Route | None = None  # configured on first use: a pinned class default must not dial at import

    @property
    def available(self) -> bool:
        """Whether there is a way to reach the model — a skill that pins one checks this itself."""
        return self._provider() is not None

    def ask(self, images_b64: str | Sequence[str], question: str, *, logger=None, retries: int = 3) -> str | None:
        """JPEG(s) + question -> reply text. None if unreachable / all retries fail.
        images_b64: one base64 string or a list of them — sent in order, so the
        question can refer to them as image 1, image 2, ... (640x480 JPEGs, at
        most two per call). Raises SkillCancelled between attempts if the run is
        cancelled."""
        provider = self._provider()
        if provider is None:
            return None
        if isinstance(images_b64, str):
            images_b64 = [images_b64]
        message = Message(Role.USER, (Text(question), *(Image(_jpeg(b)) for b in images_b64)))
        request = Request(system="", messages=(message,), temperature=0.0)
        for attempt in range(retries):
            cancellable_sleep(0)
            try:
                return provider.run(request, timeout=_TIMEOUT_SECS).message.text()
            except Exception as e:  # noqa: BLE001 — a failed attempt is retried, the last one reported
                if logger:
                    logger.warning(f"[llm] {self.model} vision call failed (try {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    cancellable_sleep(2.0 * (attempt + 1))
        return None

    def _provider(self) -> Provider | None:
        if self._route is None:
            from innate_proxy import ProxyClient

            self._route = configure(self.model, ProxyClient(), base_url=self._base_url, extra_body=self._extra_body)
        return self._route.provider


def _jpeg(b64: str) -> bytes:
    import base64

    return base64.b64decode(b64)


_robot_default: Llm | None = None


def robot_default() -> Llm:
    """The model the robot is set to — set once by the skills server from its launch parameters."""
    global _robot_default
    if _robot_default is None:
        _robot_default = Llm(
            base_url=os.environ.get("LLM_BASE_URL", ""), extra_body=os.environ.get("LLM_EXTRA_BODY", "")
        )
    return _robot_default


def set_robot_default(llm: Llm) -> None:
    global _robot_default
    _robot_default = llm
