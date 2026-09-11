# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's bounded OpenAI conversation, over the Responses API.

Responses rather than Chat Completions because Chat Completions is stateless
and drops reasoning items, which OpenAI documents as costing both tool-call
quality and extra reasoning tokens in loops like this one.

OpenAI's headline advice for agent loops — ``store: true`` plus
``previous_response_id`` — is deliberately NOT followed. Server-held state
cannot be rewritten, and this conversation's whole job is rewriting its own
history: masking old camera frames, evicting old turns. So the input list is
managed here, which is the branch OpenAI supports for exactly this case, and
``store: false`` keeps the robot's frames off their servers (reasoning items
come back carrying ``encrypted_content``, which is what makes replay work).

Threading contract: see :class:`~brain_client.brain.llm.types.Conversation`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.brain.llm.openai import wire
from brain_client.brain.llm.types import Decision, ToolCall, Usage, clean_speech, parse_arguments
from brain_client.brain.utils import FrameLabel

if TYPE_CHECKING:
    from collections.abc import Callable

    from brain_client.brain.llm.openai.transport import Transport
    from brain_client.brain.llm.types import ToolSpec
    from brain_client.brain.utils import Frame

_TERMINAL = {"response.completed", "response.failed", "response.incomplete"}


class OpenAIConversation:
    """Bounded model context for OpenAI: one generate() per agent turn."""

    def __init__(
        self,
        transport: Transport,
        *,
        model: str,
        effort: str,
        max_history: int,
        max_image_turns: int,
        cache_key: str,
        reference: list[dict] | None = None,
    ):
        self._transport = transport
        self._model = model
        self._effort = effort
        self._max_history = max_history
        self._max_image_turns = max_image_turns
        # Routing hint: requests sharing it land on the same backend, which is
        # what makes a prefix this large actually hit the cache.
        self._cache_key = cache_key
        # Pinned items (the robot's self-portrait) prepended to every request
        # but never stored in history — immune to pruning and clear().
        self._reference = reference or []
        self._history: list[dict] = []
        # The one history turn still carrying latest-only frames (wrist camera),
        # as (item, content indexes) — committing a newer set prunes these.
        self._latest_only_turn: tuple[dict, list[int]] | None = None
        self._pending_wrist: list[int] = []
        self._last_usage: Usage = {}
        self.on_request: Callable[[dict], None] | None = None

    def clear(self) -> None:
        self._history = []
        self._latest_only_turn = None
        self._pending_wrist = []
        self._last_usage = {}

    @property
    def history_len(self) -> int:
        return len(self._history)

    @property
    def last_usage(self) -> Usage:
        return self._last_usage

    @property
    def image_turn_count(self) -> int:
        return sum(1 for item in self._history if any(wire.is_image(p) for p in _content(item)))

    def open_turn(self, text: str, frames: list[Frame]) -> dict:
        self._pending_wrist = [i for i, (label, _) in enumerate(frames) if label == FrameLabel.WRIST]
        return wire.user_content(text, [jpeg for _, jpeg in frames])

    def generate(
        self,
        turn: dict,
        tools: list[ToolSpec],
        system: str,
        on_speech: Callable[[str], None] | None = None,
    ) -> dict:
        """Blocking network call — safe on a worker thread (history is only read).

        When this turn carries wrist frames, the previous turn's copies are
        masked out of the request here; the masking never mutates history.
        """
        items = [*self._reference, *self._history, turn]
        if self._pending_wrist and self._latest_only_turn is not None:
            stale, indexes = self._latest_only_turn
            masked = {**stale, "content": _masked_content(_content(stale), indexes, wire.WRIST_FRAME_REMOVED)}
            items = [masked if item is stale else item for item in items]
        body: dict = {
            "model": self._model,
            "instructions": system,
            "input": items,
            "stream": True,
            "store": False,
            "prompt_cache_key": self._cache_key,
        }
        if tools:
            body["tools"] = wire.function_tools(tools)
        if self._effort:
            # Summaries are opt-in and are what the app chat shows as thoughts.
            body["reasoning"] = {"effort": self._effort, "summary": "auto"}
        if self.on_request is not None:
            self.on_request(body)
        response = self._stream(body, on_speech)
        if not (response.get("output") or []):
            raise RuntimeError(f"openai returned no content: {_empty_reason(response)}")
        # Usage rides the response and is committed by commit(), on the loop
        # thread: writing self._last_usage here would let an abandoned turn's
        # orphaned request overwrite the committed turn's counts.
        return response

    def commit(self, turn: dict, response: dict) -> Decision:
        """Commit the exchange to history and distil the model's Decision.

        Output items are stored verbatim — a reasoning item's encrypted content
        and a function call's id both have to come back unchanged on the next
        request. Stale reasoning items are NOT stripped per turn even though
        only the newest ones earn their keep: rewriting the tail every turn
        would forfeit the prompt-cache prefix on every request, which costs far
        more than carrying them until the next compaction evicts them.
        """
        output = response.get("output") or []
        decision = _decision_from(output)
        usage = response.get("usage") or {}
        self._last_usage = {
            "prompt": usage.get("input_tokens", 0),
            "cached": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
            "output": usage.get("output_tokens", 0),
        }
        self._history.append(turn)
        if self._pending_wrist:
            if self._latest_only_turn is not None:
                item, indexes = self._latest_only_turn
                item["content"] = _masked_content(_content(item), indexes, wire.WRIST_FRAME_REMOVED)
            images = [i for i, part in enumerate(_content(turn)) if wire.is_image(part)]
            self._latest_only_turn = (turn, [images[i] for i in self._pending_wrist if i < len(images)])
        self._history.extend(output)
        self._prune()
        return decision

    def add_tool_outcomes(self, outcomes: list[tuple[ToolCall, str]]) -> None:
        """Answer the model's function calls (the API requires one response per call)."""
        self._history.extend(wire.tool_outcome(call.id, outcome) for call, outcome in outcomes)

    def _stream(self, body: dict, on_speech: Callable[[str], None] | None) -> dict:
        """Consume the event stream, speaking text deltas as they land.

        The terminal event carries the authoritative output items and usage, so
        nothing is reassembled from deltas — they only drive the voice.
        """
        for event in self._transport(body):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                if on_speech and event.get("delta"):
                    on_speech(str(event["delta"]))
            elif kind == "error":
                raise RuntimeError(f"openai stream error: {event.get('message') or event}")
            elif kind in _TERMINAL:
                response = event.get("response")
                if not isinstance(response, dict):
                    raise RuntimeError(f"openai sent {kind} without a response body")
                if kind != "response.completed":
                    # A truncated or filtered generation is a failed turn, not a
                    # short one: committing it would consume the queued events
                    # and could dispatch a call whose arguments were cut
                    # mid-JSON (and so parse as no arguments at all).
                    raise RuntimeError(f"openai response did not complete: {_empty_reason(response)}")
                return response
        raise RuntimeError("openai stream ended without a terminal event")

    def _prune(self) -> None:
        """Compact the history in chunks, never one entry per turn.

        OpenAI's prompt cache only hits on a shared prefix, so evicting or
        masking anything every turn forfeits the cache on every request. The
        history grows append-only until a budget trips, and one compaction then
        evicts down to half the cap and masks old frames in the same step — a
        single cache miss per window instead of one per turn.
        """
        history = self._history
        keep = max(self._max_image_turns, 0)
        if len(history) > self._max_history:
            del history[: len(history) - self._max_history // 2]
            # Cutting at a user message is what keeps every retained turn whole:
            # a reasoning item, a function call, or a call output left without
            # the items it belongs to is rejected on the next request.
            while history and history[0].get("role") != "user":
                history.pop(0)
            # An evicted turn must not stay pinned as the latest-only holder: commit
            # would "prune" an orphan dict nothing reads, and the reference would
            # keep its base64 wrist frame alive for as long as the arm feed is stale.
            if self._latest_only_turn is not None and not any(item is self._latest_only_turn[0] for item in history):
                self._latest_only_turn = None
        elif self.image_turn_count <= 2 * keep:
            return  # under both budgets: stay append-only, the cache is warm
        image_turns = [item for item in history if any(wire.is_image(p) for p in _content(item))]
        for item in image_turns[:-keep] if keep else image_turns:
            item["content"] = [dict(wire.FRAME_REMOVED) if wire.is_image(p) else p for p in _content(item)]


def _content(item: dict) -> list[dict]:
    """The content parts of an input message; empty for every other item kind
    (and for the reference turn, whose assistant content is a plain string)."""
    content = item.get("content")
    return content if isinstance(content, list) else []


def _masked_content(content: list[dict], indexes: list[int], placeholder: dict) -> list[dict]:
    return [dict(placeholder) if i in indexes and wire.is_image(p) else p for i, p in enumerate(content)]


def _empty_reason(response: dict) -> str:
    """Why a response carried no output, from whatever the API did say."""
    error = response.get("error") or {}
    details = response.get("incomplete_details") or {}
    reason = {"status": response.get("status"), "error": error.get("message"), "reason": details.get("reason")}
    return ", ".join(f"{k}={v}" for k, v in reason.items() if v) or "empty response"


def _decision_from(output: list[dict]) -> Decision:
    decision = Decision()
    speech: list[str] = []
    thoughts: list[str] = []
    for item in output:
        kind = item.get("type")
        if kind == "function_call":
            decision.calls.append(
                ToolCall(
                    name=str(item.get("name") or ""),
                    args=parse_arguments(item.get("arguments")),
                    id=str(item.get("call_id") or ""),
                )
            )
        elif kind == "reasoning":
            thoughts += [str(part.get("text") or "") for part in item.get("summary") or []]
        elif kind == "message":
            speech += [str(part.get("text") or "") for part in _content(item) if part.get("type") == "output_text"]
    decision.speech = clean_speech("".join(speech).strip())
    decision.thoughts = "\n".join(t for t in thoughts if t).strip() or None
    return decision
