# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Prompt -> recipe with any chat model: ask, check the recipe, feed the error back, retry.

``chat`` is any ``Callable[[list[dict[str, str]]], str]`` (OpenAI-style messages in, reply text out),
so the same loop runs against the brain's LLM on the robot, the 5090 planner, or a test double.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from brain_client.expressive import presets
from brain_client.expressive.dsl import check, variants
from brain_client.expressive.motion import Clip
from brain_client.expressive.plan import Plan
from brain_client.expressive.prompt import messages

logger = logging.getLogger(__name__)
Chat = Callable[[list[dict[str, str]]], str]
__all__ = ["Chat", "PlannerError", "Written", "clip_for", "parse_reply", "recipes_to_plans", "variants", "write"]


class PlannerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Written:
    prompt: str
    idea: str
    recipe: str
    attempts: int


def parse_reply(reply: str) -> tuple[str, str]:
    """The ``(idea, recipe)`` in a planner reply; tolerates code fences and prose around the JSON."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise ValueError('reply with JSON only: {"idea": "<one sentence>", "recipe": "<recipe>"}')
    try:
        data = json.loads(reply[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"your JSON does not parse ({e.msg}); reply with JSON only") from None
    if not isinstance(data, dict) or not isinstance(data.get("recipe"), str):
        raise ValueError('the JSON needs a "recipe" string')
    idea = data.get("idea", "")
    return (idea if isinstance(idea, str) else ""), data["recipe"].strip()


def write(prompt: str, chat: Chat, retries: int = 2) -> Written:
    """Ask ``chat`` for a recipe, repairing it up to ``retries`` times from the checker's errors."""
    conversation = messages(prompt)
    error = "no reply"
    for attempt in range(1, retries + 2):
        reply = chat(conversation)
        try:
            idea, recipe = parse_reply(reply)
        except ValueError as e:
            idea, recipe, error = "", "", str(e)
        else:
            error = check(recipe) or ""
        if not error:
            return Written(prompt=prompt, idea=idea, recipe=recipe, attempts=attempt)
        logger.info("planner attempt %d for %r invalid: %s", attempt, prompt, error)
        conversation = [
            *conversation,
            {"role": "assistant", "content": reply},
            {"role": "user", "content": f"That recipe is invalid: {error}. Reply with the corrected JSON only."},
        ]
    raise PlannerError(f"no valid recipe for {prompt!r} after {retries + 1} attempts: {error}")


def recipes_to_plans(recipes: Sequence[str], n: int = 1, seed: int = 0) -> list[list[Plan]]:
    """``n`` randomised serving plans per recipe (recipe ``i`` is seeded ``seed + i``)."""
    return [variants(recipe, n, seed + i) for i, recipe in enumerate(recipes)]


def clip_for(prompt: str, chat: Chat | None = None, seed: int = 0, retries: int = 2) -> Clip:
    """A playable clip for ``prompt``, never failing: the chat planner, else the closest built-in preset."""
    if chat is not None:
        try:
            written = write(prompt, chat, retries)
            return Clip.from_recipe(written.recipe, name=prompt, seed=seed, prompt=prompt, idea=written.idea)
        except Exception as e:  # noqa: BLE001 — a dead or confused LLM must still leave the robot a gesture
            logger.warning("planner failed for %r, using a preset: %s", prompt, e)
    return presets.clip(presets.match(prompt), seed=seed, prompt=prompt)
