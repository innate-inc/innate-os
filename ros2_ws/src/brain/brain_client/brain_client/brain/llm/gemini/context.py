# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's bounded Gemini conversation, over the native REST API.

The native API — unlike the OpenAI-compatible layer — returns *thought
summaries* (parts flagged ``thought: true``), which the agent surfaces in the
app chat as robot thoughts. A response is distilled into a plain
:class:`~brain_client.brain.llm.types.Decision` the agent loop acts on, and the
bounded content history is compacted in chunks — old camera frames masked,
oldest turns evicted — so requests stay small while consecutive requests keep
the shared byte prefix Gemini's implicit prompt cache needs (see
:meth:`GeminiConversation._prune`).

Threading contract: see :class:`~brain_client.brain.llm.types.Conversation`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.brain.llm.gemini import wire
from brain_client.brain.llm.types import Decision, ToolCall, Usage, clean_speech
from brain_client.brain.utils import FrameLabel

if TYPE_CHECKING:
    from collections.abc import Callable

    from brain_client.brain.llm.gemini.transport import Transport
    from brain_client.brain.llm.types import ToolSpec
    from brain_client.brain.utils import Frame


class GeminiConversation:
    """Bounded model context for Gemini: one generate() per agent turn."""

    def __init__(
        self,
        transport: Transport,
        *,
        model: str,
        thinking_level: str,
        max_history: int,
        max_image_turns: int,
        reference: list[dict] | None = None,
    ):
        self._transport = transport
        self._model = model
        self._thinking_level = thinking_level
        self._max_history = max_history
        self._max_image_turns = max_image_turns
        # Pinned turns (the robot's self-portrait) prepended to every request
        # but never stored in history — immune to pruning and clear().
        self._reference = reference or []
        self._history: list[dict] = []
        # The one history turn still carrying latest-only frames (wrist camera),
        # as (content, part indexes) — absorbing a newer set prunes these.
        self._latest_only_turn: tuple[dict, list[int]] | None = None
        # Image positions in the turn open_turn built last, read by generate and
        # commit. Only the loop thread writes it, and generate reads it while
        # serializing its body — the same window in which an abandoned turn's
        # orphaned request is already immune to later history mutation.
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
        return sum(1 for c in self._history if c.get("role") == "user" and any(wire.is_image(p) for p in _parts(c)))

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
        masked out of the request here — commit's durable prune runs only after
        the response, so without this every request would ship two wrist frames.
        The masking HERE never mutates history: shallow copies only.
        """
        contents = [*self._reference, *self._history, turn]
        if self._pending_wrist and self._latest_only_turn is not None:
            stale, indexes = self._latest_only_turn
            masked = {**stale, "parts": _masked_parts(_parts(stale), indexes, wire.WRIST_FRAME_REMOVED)}
            contents = [masked if content is stale else content for content in contents]
        thinking: dict = {"includeThoughts": True}
        if self._thinking_level:
            thinking["thinkingLevel"] = self._thinking_level
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"thinkingConfig": thinking},
        }
        tools_block = wire.tools_block(tools)
        if tools_block:
            body["tools"] = tools_block
        if self.on_request is not None:
            self.on_request(body)
        parts: list[dict] = []
        last_chunk: dict = {}
        usage: dict = {}
        for chunk in self._transport(self._model, body):
            last_chunk = chunk
            if "usageMetadata" in chunk:
                usage = chunk["usageMetadata"]
            content = _model_content(chunk) or {}
            for part in content.get("parts") or []:
                parts.append(part)
                if on_speech and part.get("text") and not part.get("thought"):
                    on_speech(part["text"])
        if not parts:
            # A 200 stream with no content at all: a safety block, a malformed
            # function call, or an empty candidate. Committing it would record
            # a silent, answerless exchange — raise instead, so the turn's
            # retry path keeps the events queued and the failure is visible.
            raise RuntimeError(f"gemini returned no content: {_empty_stream_reason(last_chunk)}")
        # Usage rides the response and is committed by commit(), on the loop
        # thread: writing self._last_usage here would let an abandoned turn's
        # orphaned request overwrite the committed turn's counts.
        return {
            "candidates": [{"content": {"role": "model", "parts": _merged(parts)}}],
            "usageMetadata": usage,
        }

    def commit(self, turn: dict, response: dict) -> Decision:
        """Commit the exchange to history and distil the model's Decision.

        Thought-summary parts are dropped from the stored model turn (they are
        display-only); everything else — including any thoughtSignature the
        model attached to its parts — is kept verbatim for the next request.

        A stale wrist frame reads as current grasp state and misleads the model,
        so committing a new one prunes the previous turn's copy on the spot.
        """
        decision = _decision_from(response)
        usage = response.get("usageMetadata") or {}
        self._last_usage = {
            "prompt": usage.get("promptTokenCount", 0),
            "cached": usage.get("cachedContentTokenCount", 0),
            "output": usage.get("candidatesTokenCount", 0),
        }
        self._history.append(turn)
        if self._pending_wrist:
            if self._latest_only_turn is not None:
                content, indexes = self._latest_only_turn
                content["parts"] = _masked_parts(_parts(content), indexes, wire.WRIST_FRAME_REMOVED)
            image_parts = [i for i, p in enumerate(_parts(turn)) if wire.is_image(p)]
            self._latest_only_turn = (turn, [image_parts[i] for i in self._pending_wrist if i < len(image_parts)])
        model_content = _model_content(response)
        if model_content is not None:
            kept = [p for p in model_content.get("parts") or [] if not p.get("thought")]
            self._history.append({**model_content, "parts": kept or [{"text": ""}]})
        self._prune()
        return decision

    def add_tool_outcomes(self, outcomes: list[tuple[ToolCall, str]]) -> None:
        """Answer the model's function calls (the API requires one response per call)."""
        if not outcomes:
            return
        parts = []
        for call, outcome in outcomes:
            function_response: dict = {"name": call.name, "response": {"outcome": outcome}}
            if call.id:
                function_response["id"] = call.id
            parts.append({"functionResponse": function_response})
        self._history.append({"role": "user", "parts": parts})

    def _prune(self) -> None:
        """Compact the history in chunks, never one entry per turn.

        Gemini's implicit prompt cache only hits when a prior request is a
        byte prefix of the new one, so evicting or masking anything every turn
        forfeits the cache on every request. The history therefore grows
        append-only until a budget trips, and one compaction then evicts down
        to half the cap and masks old frames in the same step — a single cache
        miss per window instead of one per turn.
        """
        history = self._history
        keep = max(self._max_image_turns, 0)
        if len(history) > self._max_history:
            del history[: len(history) - self._max_history // 2]
            # The history must start with a plain user turn: a leading model turn or
            # an orphaned function response (whose call was just evicted) is rejected.
            while history and (
                history[0].get("role") != "user" or any("functionResponse" in p for p in _parts(history[0]))
            ):
                history.pop(0)
            # An evicted turn must not stay pinned as the latest-only holder: commit
            # would "prune" an orphan dict nothing reads, and the reference would
            # keep its base64 wrist frame alive for as long as the arm feed is stale.
            if self._latest_only_turn is not None and not any(c is self._latest_only_turn[0] for c in history):
                self._latest_only_turn = None
        elif self.image_turn_count <= 2 * keep:
            return  # under both budgets: stay append-only, the cache is warm
        # Mask down to the newest few frame turns (none at all if the keep-count
        # is zero or nonsensical). Until a compaction, older frames ride the
        # cached prefix at a tenth of the input price — cheap to carry.
        image_turns = [c for c in history if c.get("role") == "user" and any(wire.is_image(p) for p in _parts(c))]
        for content in image_turns[:-keep] if keep else image_turns:
            content["parts"] = [dict(wire.FRAME_REMOVED) if wire.is_image(p) else p for p in _parts(content)]


def _parts(content: dict) -> list[dict]:
    return content.get("parts") or []


def _masked_parts(parts: list[dict], indexes: list[int], placeholder: dict) -> list[dict]:
    return [dict(placeholder) if i in indexes and wire.is_image(p) else p for i, p in enumerate(parts)]


def _merged(parts: list[dict]) -> list[dict]:
    """Collapse adjacent plain-text deltas; anything carrying more than text
    (thoughts, signatures, calls) is kept verbatim for the stored history."""
    merged: list[dict] = []
    for part in parts:
        previous = merged[-1] if merged else None
        if previous is not None and set(previous) == {"text"} and set(part) == {"text"}:
            previous["text"] += part["text"]
        else:
            merged.append(dict(part))
    return merged


def _model_content(response: dict) -> dict | None:
    candidates = response.get("candidates") or []
    return candidates[0].get("content") if candidates else None


def _empty_stream_reason(last_chunk: dict) -> str:
    """Why a stream carried no parts, from whatever the API did say."""
    candidates = last_chunk.get("candidates") or [{}]
    reason = {
        "finishReason": candidates[0].get("finishReason"),
        "blockReason": (last_chunk.get("promptFeedback") or {}).get("blockReason"),
    }
    details = ", ".join(f"{k}={v}" for k, v in reason.items() if v)
    return details or "empty stream"


def _decision_from(response: dict) -> Decision:
    decision = Decision()
    content = _model_content(response) or {}
    for part in content.get("parts") or []:
        call = part.get("functionCall")
        if call is not None:
            args = call.get("args") or {}
            decision.calls.append(
                ToolCall(
                    name=call.get("name") or "", args=args if isinstance(args, dict) else {}, id=call.get("id") or ""
                )
            )
        elif part.get("text"):
            if part.get("thought"):
                decision.thoughts = f"{decision.thoughts or ''}{part['text']}".strip()
            else:
                decision.speech = f"{decision.speech or ''}{part['text']}".strip()
    decision.speech = clean_speech(decision.speech)
    return decision
