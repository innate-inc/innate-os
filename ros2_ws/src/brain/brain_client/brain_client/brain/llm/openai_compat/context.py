# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's bounded conversation over the Chat Completions wire.

Chat Completions is the one request shape every OpenAI-compatible server
answers — NVIDIA NIM, vLLM, Ollama, llama.cpp — which is exactly why this
provider uses it rather than the richer Responses API the OpenAI provider is
built on. The trade is thoughts: a server only reports them when it runs a
reasoning parser (``delta.reasoning_content`` on vLLM and NIM, ``delta.reasoning``
elsewhere); without one the model's thinking is invisible or, worse, arrives
inline as text — so run reasoning models with their parser or with thinking
off (the ``off`` thinking level).

Threading contract: see :class:`~brain_client.brain.llm.types.Conversation`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from brain_client.brain.llm.openai_compat import wire
from brain_client.brain.llm.types import Decision, ToolCall, Usage, clean_speech, parse_arguments
from brain_client.brain.utils import FrameLabel

if TYPE_CHECKING:
    from collections.abc import Callable

    from brain_client.brain.llm.openai_compat.transport import Transport
    from brain_client.brain.llm.types import ToolSpec
    from brain_client.brain.utils import Frame


class OpenAICompatConversation:
    """Bounded model context for a Chat Completions server: one generate() per agent turn."""

    def __init__(
        self,
        transport: Transport,
        *,
        model: str,
        thinking: dict,
        max_history: int,
        max_image_turns: int,
        reference: list[dict] | None = None,
    ):
        self._transport = transport
        self._model = model
        self._thinking = thinking  # request fields that set the thinking level (see build())
        self._max_history = max_history
        self._max_image_turns = max_image_turns
        # Pinned messages (the robot's self-portrait) prepended to every request
        # but never stored in history — immune to pruning and clear().
        self._reference = reference or []
        self._history: list[dict] = []
        # The one history turn still carrying latest-only frames (wrist camera),
        # as (message, content indexes) — committing a newer set prunes these.
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
        return sum(1 for message in self._history if any(wire.is_image(p) for p in _content(message)))

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
        messages = [*self._reference, *self._history, turn]
        if self._pending_wrist and self._latest_only_turn is not None:
            stale, indexes = self._latest_only_turn
            masked = {**stale, "content": _masked_content(_content(stale), indexes, wire.WRIST_FRAME_REMOVED)}
            messages = [masked if message is stale else message for message in messages]
        body: dict = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            **self._thinking,
        }
        if tools:
            body["tools"] = wire.function_tools(tools)
        if self.on_request is not None:
            self.on_request(body)
        response = self._stream(body, on_speech)
        if not response["speech"] and not response["calls"]:
            raise RuntimeError(f"openai_compat returned no content: finish_reason={response['finish_reason']}")
        # Usage rides the response and is committed by commit(), on the loop
        # thread: writing self._last_usage here would let an abandoned turn's
        # orphaned request overwrite the committed turn's counts.
        return response

    def commit(self, turn: dict, response: dict) -> Decision:
        """Commit the exchange to history and distil the model's Decision.

        Thoughts are display-only and never stored; the assistant message is
        stored with its tool calls verbatim, ids included, because the tool
        results that follow have to quote them on the next request.
        """
        usage = response["usage"]
        self._last_usage = {
            "prompt": usage.get("prompt_tokens", 0),
            "cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "output": usage.get("completion_tokens", 0),
        }
        self._history.append(turn)
        if self._pending_wrist:
            if self._latest_only_turn is not None:
                message, indexes = self._latest_only_turn
                message["content"] = _masked_content(_content(message), indexes, wire.WRIST_FRAME_REMOVED)
            images = [i for i, part in enumerate(_content(turn)) if wire.is_image(part)]
            self._latest_only_turn = (turn, [images[i] for i in self._pending_wrist if i < len(images)])
        self._history.append(wire.assistant_message(response["speech"], response["calls"]))
        self._prune()
        return Decision(
            speech=clean_speech(response["speech"]),
            thoughts=response["thoughts"].strip() or None,
            calls=[
                ToolCall(call["name"], parse_arguments(call["arguments"]), call["id"]) for call in response["calls"]
            ],
        )

    def add_tool_outcomes(self, outcomes: list[tuple[ToolCall, str]]) -> None:
        """Answer the model's function calls (the API requires one response per call)."""
        self._history.extend(wire.tool_outcome(call.id, outcome) for call, outcome in outcomes)

    def _stream(self, body: dict, on_speech: Callable[[str], None] | None) -> dict:
        """Assemble the reply from its deltas, speaking text as it lands.

        Tool calls arrive in fragments keyed by ``index`` — the id and name
        first, the JSON arguments in pieces — and are only whole at the end.
        A stream that stops before ``finish_reason`` (a dropped connection)
        or ends on anything but ``stop``/``tool_calls`` is a failed turn:
        committing it would consume the queued events and could dispatch a
        call whose arguments were cut mid-JSON.
        """
        speech: list[str] = []
        thoughts: list[str] = []
        calls: dict[int, dict] = {}
        finish_reason: str | None = None
        usage: dict = {}
        for chunk in self._transport(body):
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    speech.append(str(delta["content"]))
                    if on_speech:
                        on_speech(str(delta["content"]))
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    thoughts.append(str(reasoning))
                for piece in delta.get("tool_calls") or []:
                    _absorb_call_piece(calls, piece)
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
        if finish_reason is None:
            raise RuntimeError("openai_compat stream ended without a finish_reason")
        if finish_reason not in ("stop", "tool_calls"):
            raise RuntimeError(f"openai_compat response did not complete: finish_reason={finish_reason}")
        return {
            "speech": "".join(speech),
            "thoughts": "".join(thoughts),
            "calls": [calls[index] for index in sorted(calls)],
            "finish_reason": finish_reason,
            "usage": usage,
        }

    def _prune(self) -> None:
        """Compact the history in chunks, never one entry per turn.

        A server's prefix cache only hits on a shared prefix, so evicting or
        masking anything every turn forfeits it on every request. The history
        grows append-only until a budget trips, and one compaction then evicts
        down to half the cap and masks old frames in the same step — a single
        cache miss per window instead of one per turn.
        """
        history = self._history
        keep = max(self._max_image_turns, 0)
        if len(history) > self._max_history:
            del history[: len(history) - self._max_history // 2]
            # Cutting at a user message keeps every retained turn whole: an
            # assistant message or tool result left without its partner is
            # rejected on the next request.
            while history and history[0].get("role") != "user":
                history.pop(0)
            # An evicted turn must not stay pinned as the latest-only holder: commit
            # would "prune" an orphan dict nothing reads, and the reference would
            # keep its base64 wrist frame alive for as long as the arm feed is stale.
            if self._latest_only_turn is not None and not any(m is self._latest_only_turn[0] for m in history):
                self._latest_only_turn = None
        elif self.image_turn_count <= 2 * keep:
            return  # under both budgets: stay append-only, the cache is warm
        image_turns = [message for message in history if any(wire.is_image(p) for p in _content(message))]
        for message in image_turns[:-keep] if keep else image_turns:
            message["content"] = [dict(wire.FRAME_REMOVED) if wire.is_image(p) else p for p in _content(message)]


def _absorb_call_piece(calls: dict[int, dict], piece: dict) -> None:
    index = int(piece.get("index") or 0)
    call = calls.get(index)
    if call is None:
        # A server that omits ids still needs one echoed back with the result.
        call = calls[index] = {
            "id": str(piece.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
            "name": "",
            "arguments": "",
        }
    function = piece.get("function") or {}
    if function.get("name"):
        call["name"] = str(function["name"])
    if function.get("arguments"):
        call["arguments"] += str(function["arguments"])


def _content(message: dict) -> list[dict]:
    """The content parts of a user message; empty for string content (assistant
    and tool messages, and the reference turn's reply)."""
    content = message.get("content")
    return content if isinstance(content, list) else []


def _masked_content(content: list[dict], indexes: list[int], placeholder: dict) -> list[dict]:
    return [dict(placeholder) if i in indexes and wire.is_image(p) else p for i, p in enumerate(content)]
