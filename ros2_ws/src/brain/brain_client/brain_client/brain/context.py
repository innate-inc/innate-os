# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's bounded Gemini conversation, over the native REST API.

The native API — unlike the OpenAI-compatible layer — returns *thought
summaries* (parts flagged ``thought: true``), which the agent surfaces in the
app chat as robot thoughts. A response is distilled into a plain
:class:`Decision` the agent loop acts on, and the bounded content history is
compacted in chunks — old camera frames masked, oldest turns evicted — so
requests stay small while consecutive requests keep the shared byte prefix
Gemini's implicit prompt cache needs (see :meth:`GeminiContext._prune`).

Threading contract: :meth:`GeminiContext.generate` is the only blocking network
call and only *reads* the history, so the agent awaits it on a worker thread
(``asyncio.to_thread``). All history mutation (:meth:`absorb`,
:meth:`add_tool_outcomes`) happens in the agent's coroutine, strictly between
generate calls, and :meth:`clear` only runs while the loop task is stopped —
there is no concurrent access to a context by construction, not by lock.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from brain_client.brain.transport import Transport

_FRAME_REMOVED = {"text": "[older camera frame removed]"}
_WRIST_FRAME_REMOVED = {"text": "[older wrist camera frame removed]"}


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str = ""


@dataclass
class Decision:
    """What the model wants the robot to do this turn."""

    speech: str | None = None
    thoughts: str | None = None
    calls: list[ToolCall] = field(default_factory=list)


class GeminiContext:
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
        # Observability tap: called with the exact request body just before it
        # goes on the wire (from generate's thread). The body must be treated
        # as read-only — it shares structure with the live history.
        self.on_request: Callable[[dict], None] | None = None
        # usageMetadata of the newest response — prompt/cached/output token
        # counts, surfaced on the trace snapshot (cache-hit observability).
        self.last_usage: dict[str, int] = {}

    def clear(self) -> None:
        self._history = []
        self._latest_only_turn = None
        self.last_usage = {}

    @property
    def history_len(self) -> int:
        return len(self._history)

    @property
    def image_turn_count(self) -> int:
        """History turns still carrying camera frames (pruning keeps the newest few)."""
        return sum(
            1 for c in self._history if c.get("role") == "user" and any("inlineData" in p for p in c.get("parts") or [])
        )

    @staticmethod
    def user_message(text: str, images: list[bytes]) -> dict:
        parts: list[dict] = [{"text": text}]
        for jpeg in images:
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}})
        return {"role": "user", "parts": parts}

    def generate(
        self,
        user_message: dict,
        tools: list[dict],
        system: str,
        on_speech: Callable[[str], None] | None = None,
        *,
        latest_only_images: list[int] | None = None,
        live_context: dict | None = None,
    ) -> dict:
        """Blocking network call — safe on a worker thread (history is only read).

        The reply streams in; every plain-text delta is handed to ``on_speech``
        as it arrives, which is what lets the robot start talking at the first
        sentence boundary. Returns the fully assembled response.

        ``latest_only_images`` mirrors :meth:`absorb`'s: when this message
        carries wrist frames, the previous turn's copies are masked out of the
        request here — absorb's durable prune runs only after the response, so
        without this every request would ship two wrist frames. The masking
        HERE never mutates history — shallow copies only. Durable mutation
        (absorb, _prune) runs on the loop thread strictly between generate
        calls, by which point an abandoned turn's orphaned request has already
        serialized its body.
        """
        contents = [*self._reference, *self._history]
        history_length = len(contents)
        contents.extend([*([live_context] if live_context else []), user_message])
        if latest_only_images and self._latest_only_turn is not None:
            stale, indexes = self._latest_only_turn
            masked = {
                **stale,
                "parts": [
                    dict(_WRIST_FRAME_REMOVED) if i in indexes and "inlineData" in p else p
                    for i, p in enumerate(stale["parts"])
                ],
            }
            contents = [masked if content is stale else content for content in contents]
        thinking: dict = {"includeThoughts": True}
        if self._thinking_level:
            thinking["thinkingLevel"] = self._thinking_level
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"thinkingConfig": thinking},
        }
        if tools:
            body["tools"] = tools
        if self.on_request is not None:
            self.on_request(body)
        parts: list[dict] = []
        last_chunk: dict = {}
        usage: dict = {}
        for chunk in self._stream_response(body, history_length=history_length):
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
        # Usage rides the response and is committed by absorb, on the loop
        # thread: writing self.last_usage here would let an abandoned turn's
        # orphaned request overwrite the committed turn's counts.
        return {
            "candidates": [{"content": {"role": "model", "parts": _merged(parts)}}],
            "usageMetadata": usage,
        }

    def _stream_response(self, body: dict, *, history_length: int):
        """Provider hook with the boundary before ephemeral context and fresh input."""
        return self._transport(self._model, body)

    def absorb(self, user_message: dict, response: dict, *, latest_only_images: list[int] | None = None) -> Decision:
        """Commit the exchange to history and distill the model's Decision.

        Thought-summary parts are dropped from the stored model turn (they are
        display-only); everything else — including any thoughtSignature the
        model attached to its parts — is kept verbatim for the next request.

        ``latest_only_images`` names positions in this message's image list
        (order given to :meth:`user_message`) that must only ever appear in the
        newest turn — the wrist camera: a stale gripper close-up reads as
        current grasp state and misleads the model, so absorbing a new one
        prunes the previous turn's copy on the spot.
        """
        decision = _decision_from(response)
        usage = response.get("usageMetadata") or {}
        self.last_usage = {
            "prompt": usage.get("promptTokenCount", 0),
            "cached": usage.get("cachedContentTokenCount", 0),
            "output": usage.get("candidatesTokenCount", 0),
        }
        if "cacheWriteTokenCount" in usage:
            self.last_usage["cache_write"] = usage["cacheWriteTokenCount"]
        self._history.append(user_message)
        if latest_only_images:
            if self._latest_only_turn is not None:
                content, indexes = self._latest_only_turn
                content["parts"] = [
                    dict(_WRIST_FRAME_REMOVED) if i in indexes and "inlineData" in p else p
                    for i, p in enumerate(content["parts"])
                ]
            image_parts = [i for i, p in enumerate(user_message["parts"]) if "inlineData" in p]
            self._latest_only_turn = (
                user_message,
                [image_parts[i] for i in latest_only_images if i < len(image_parts)],
            )
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
                history[0].get("role") != "user" or any("functionResponse" in p for p in history[0].get("parts") or [])
            ):
                history.pop(0)
            # An evicted turn must not stay pinned as the latest-only holder: absorb
            # would "prune" an orphan dict nothing reads, and the reference would
            # keep its base64 wrist frame alive for as long as the arm feed is stale.
            if self._latest_only_turn is not None and not any(c is self._latest_only_turn[0] for c in history):
                self._latest_only_turn = None
        elif self.image_turn_count <= 2 * keep:
            return  # under both budgets: stay append-only, the cache is warm
        # Mask down to the newest few frame turns (none at all if the keep-count
        # is zero or nonsensical). Until a compaction, older frames ride the
        # cached prefix at a tenth of the input price — cheap to carry.
        image_turns = [
            c for c in history if c.get("role") == "user" and any("inlineData" in p for p in c.get("parts") or [])
        ]
        for content in image_turns[:-keep] if keep else image_turns:
            content["parts"] = [dict(_FRAME_REMOVED) if "inlineData" in p else p for p in content["parts"]]


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
    decision.speech = _clean_speech(decision.speech)
    return decision


_TOOL_NARRATION = re.compile(r"Calling tool\b")


def split_tool_narration(text: str) -> tuple[str, bool]:
    """Cut leaked tool-call narration ("Calling tool ..." to end of text).

    gemini-3 preview sometimes appends it to its reply, without a sentence
    boundary. Returns ``(clean text, whether narration was found)`` — the one
    scrub both the chat transcript (:func:`_clean_speech`) and the audio path
    (``SpeechStreamer._say``) apply, so the two can never diverge.
    """
    match = _TOOL_NARRATION.search(text)
    if match is None:
        return text, False
    return text[: match.start()].rstrip(), True


def _clean_speech(speech: str | None) -> str | None:
    """Drop unspeakable output: placeholders and leaked tool-call narration."""
    if not speech:
        return None
    speech, _ = split_tool_narration(speech)
    return speech if re.search(r"[a-zA-Z0-9]", speech) else None
