# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Prompt -> clip, down the fallback chain: the LAN planner server, then the brain's own LLM writing
a recipe for the procedural generator, then the nearest built-in preset — never nothing. A spoken
sentence asks the server alone (``perform``): the brain's LLM takes seconds, the sentence is gone by then.

No ROS. ``ClipMaker.make`` and ``perform`` block (network), so callers run them off the executor thread.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from innate_llm import Message, Request, Role, Text, Thinking

from brain_client.common.enums import StrEnum
from brain_client.expressive import planner, presets
from brain_client.expressive.motion import Clip

if TYPE_CHECKING:
    from innate_llm import Provider

    from brain_client.expressive.planner import Chat

SERVER_TIMEOUT_S = 1.5
# The server answers one request at a time, so a sentence can queue behind others; its clip is made
# while earlier sentences play, so waiting costs little.
SPEECH_TIMEOUT_S = 3.0
# Probed off the prompt path: resolving an absent .local host alone takes ~2 s.
SERVER_PROBE_S = 30.0
LLM_TIMEOUT_S = 10.0


class ClipSource(StrEnum):
    """Which link of the chain made a clip — wire-visible on /brain/express/state and generate_res."""

    SERVER = "server"
    LLM = "llm"
    PRESET = "preset"
    STAND_IN = "preset-stand-in"  # the keyword preset playing while the generated clip is on its way
    PLAYED = "played"  # handed over whole on /brain/express/play


@dataclass(frozen=True)
class Made:
    clip: Clip
    source: ClipSource


class ClipMaker:
    def __init__(self, server_url: str, provider: Callable[[], Provider | None], logger) -> None:
        """``provider`` is read per prompt, so a live model switch reaches the next one."""
        self._server_url = server_url.rstrip("/")
        self._provider = provider
        self._logger = logger
        self._server_up = False
        self._seeds = itertools.count()  # the same prompt twice must not move identically
        if self._server_url:
            threading.Thread(target=self._watch_server, name="expressive-server-probe", daemon=True).start()

    @property
    def server_up(self) -> bool:
        return self._server_up

    def make(self, prompt: str) -> Made:
        seed = next(self._seeds)
        clip = self._from_server(prompt, seed)
        if clip is not None:
            return Made(clip, ClipSource.SERVER)
        clip = self._from_llm(prompt, seed)
        if clip is not None:
            return Made(clip, ClipSource.LLM)
        return Made(presets.clip(presets.match(prompt), seed=seed, prompt=prompt), ClipSource.PRESET)

    def perform(self, prompt: str, effort: str) -> Clip | None:
        """The planner server's clip on its ``effort`` tier, None when the server is down or fails."""
        return self._from_server(prompt, next(self._seeds), effort, SPEECH_TIMEOUT_S)

    def _from_server(
        self, prompt: str, seed: int, effort: str = "medium", timeout: float = SERVER_TIMEOUT_S
    ) -> Clip | None:
        if not self._server_up:
            return None
        body = json.dumps({"prompt": prompt, "n": 1, "seed": seed, "effort": effort}).encode()
        request = urllib.request.Request(
            f"{self._server_url}/generate-dense", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Clip.load(json.loads(response.read())["clips"][0])
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            self._logger.warning(f"[Expressive] planner server failed on '{prompt}': {error}")
            return None

    def _watch_server(self) -> None:
        while True:
            try:
                with urllib.request.urlopen(f"{self._server_url}/health", timeout=SERVER_TIMEOUT_S) as response:
                    up = response.status == 200
            except OSError:
                up = False
            if up != self._server_up:
                self._logger.info(f"[Expressive] planner server {self._server_url} {'up' if up else 'down'}")
            self._server_up = up
            time.sleep(SERVER_PROBE_S)

    def _from_llm(self, prompt: str, seed: int) -> Clip | None:
        provider = self._provider()
        if provider is None:
            return None
        try:
            written = planner.write(prompt, _chat_through(provider))
            return Clip.from_recipe(written.recipe, name=prompt, seed=seed, prompt=prompt, idea=written.idea)
        except Exception as error:  # noqa: BLE001 — any planner or transport failure falls through to the presets
            self._logger.warning(f"[Expressive] the brain's LLM could not plan '{prompt}': {error!r}")
            return None


def _chat_through(provider: Provider) -> Chat:
    """The planner's ``[{role, content}] -> reply text`` chat, on the brain's model."""

    def chat(messages: list[dict[str, str]]) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = tuple(
            Message(Role.ASSISTANT if m["role"] == "assistant" else Role.USER, (Text(m["content"]),))
            for m in messages
            if m["role"] != "system"
        )
        request = Request(system=system, messages=turns, thinking=Thinking.MINIMAL)
        return provider.run(request, timeout=LLM_TIMEOUT_S).message.text()

    return chat
