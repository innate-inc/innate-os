# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The model a skill asks about what it sees, declared like any interface::

    class PickAnyObject(Skill):
        llm: Llm                                   # the model the robot is set to
        llm: Llm = Llm(thinking=Thinking.MINIMAL)  # that model, at this skill's reasoning effort
        llm: Llm = Llm("google:gemini-3.5-flash")  # a skill tuned to one model pins it

The robot's default is what the skills server was launched with (the brain's
``llm_model`` setting), else the environment; a pinned one is reached the
same way — the Innate proxy, a vendor key, or an ``llm_base_url`` server.

Reasoning effort is per skill, because the calls differ: locating a box wants
none of it, judging a grasp can afford some. Naming a level inherits the
robot's route and changes only the effort.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from innate_llm import Image, Message, Request, Role, Text, configure
from innate_llm.configure import DEFAULT_MODEL, KEY_ENVS
from innate_llm.types import Thinking
from mars_bringup.config_loader import keys_env_path, parse_key_value_env

from brain_client.skills.types import cancellable_sleep

if TYPE_CHECKING:
    from innate_llm import Llm as Route
    from innate_llm import Provider

_TIMEOUT_SECS = 60.0
# The qwen-family chat templates reason unless the template itself is told not to:
# reasoning_effort alone only shortens the thinking (152 tokens vs 62 measured on a
# detection call). Every OpenAI-compatible server that ships those templates —
# llama.cpp, vLLM, Ollama, NIM — takes this knob, and only such a server is ever
# addressed by base_url, so it rides along with the lowest rung rather than going
# to a vendor API that would reject the field.
_TEMPLATE_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}


class Llm:
    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str = "",
        extra_body: str = "",
        thinking: Thinking = Thinking.DEFAULT,
    ):
        self._model = model
        self._base_url = base_url
        self._extra_body = extra_body
        self._thinking = thinking
        self._route: Route | None = None  # configured on first use: a pinned class default must not dial at import

    @property
    def model(self) -> str:
        return self._route_of()[0]

    @property
    def available(self) -> bool:
        """Whether there is a way to reach the model — a skill that pins one checks this itself."""
        return self._provider() is not None

    def _route_of(self) -> tuple[str, str, str]:
        """(model, base_url, extra_body). A skill that names no model rides the robot's
        route — resolved at first use, since the skills server sets it after import."""
        default = robot_default()
        if self._model is not None or self is default:
            return (self._model or os.environ.get("LLM_MODEL", DEFAULT_MODEL), self._base_url, self._extra_body)
        return (default.model, default._base_url, self._extra_body or default._extra_body)

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
        request = Request(system="", messages=(message,), temperature=0.0, thinking=self._thinking)
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

            refresh_keys()
            model, base_url, extra_body = self._route_of()
            self._route = configure(
                model, ProxyClient(), base_url=base_url, extra_body=self._knobs(base_url, extra_body)
            )
        return self._route.provider

    def _knobs(self, base_url: str, extra_body: str) -> str:
        """The server knobs for this route, with the template's own reasoning switched
        off at the lowest rung (see _TEMPLATE_NO_THINK)."""
        if self._thinking != Thinking.MINIMAL or not base_url:
            return extra_body
        try:
            extra = json.loads(extra_body) if extra_body else {}
        except json.JSONDecodeError:
            extra = {}
        return json.dumps({**_TEMPLATE_NO_THINK, **extra})


_governed: set[str] = set()  # the key names the keys file has held: those a clear there removes here


def refresh_keys() -> None:
    """The vendor keys as the keys file holds them now, so one saved or cleared in Settings after boot
    reaches the next ``configure()``. A key the file never held — passed as environment, as the public
    demo does — is left alone."""
    fresh = parse_key_value_env(keys_env_path())
    for name in KEY_ENVS:
        value = fresh.get(name, "").strip()
        if value:
            os.environ[name] = value
            _governed.add(name)
        elif name in _governed:
            os.environ.pop(name, None)


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
