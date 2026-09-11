# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's bounded conversation, as Chat Completions messages.

A streamed reply is assembled into one assistant message — stored and replayed
verbatim, extras included, because that is how a server's own thought
signatures survive the next turn — and distilled into a plain :class:`Decision`
the agent loop acts on. Thoughts are whatever the server streams as
``reasoning_content`` / ``reasoning`` (vLLM, NIM); Google's compatible layer
sends none. The bounded history is compacted in chunks — old camera frames
masked, oldest turns evicted — so requests stay small while consecutive
requests keep the shared byte prefix a server's implicit prompt cache needs
(see :meth:`ChatContext._prune`).

Threading contract: :meth:`ChatContext.generate` is the only blocking network
call and only *reads* the history, so the agent awaits it on a worker thread
(``asyncio.to_thread``). All history mutation (:meth:`absorb`,
:meth:`add_tool_outcomes`) happens in the agent's coroutine, strictly between
generate calls, and :meth:`clear` only runs while the loop task is stopped —
there is no concurrent access to a context by construction, not by lock.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain_client.brain.transport import ChatTransport, Chunks

_FRAME_REMOVED = {"type": "text", "text": "[older camera frame removed]"}
_WRIST_FRAME_REMOVED = {"type": "text", "text": "[older wrist camera frame removed]"}
_FINISHED = ("stop", "tool_calls")


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


class ChatContext:
    """Bounded model context: one generate() per agent turn."""

    def __init__(
        self,
        transport: ChatTransport,
        *,
        model: str,
        thinking: str,
        max_history: int,
        max_image_turns: int,
        reference: list[dict] | None = None,
    ):
        self._transport = transport
        self._model = model
        self._thinking = thinking
        self._max_history = max_history
        self._max_image_turns = max_image_turns
        # Pinned turns (the robot's self-portrait) prepended to every request
        # but never stored in history — immune to pruning and clear().
        self._reference = reference or []
        self._history: list[dict] = []
        # The one history turn still carrying latest-only frames (wrist camera),
        # as (message, content part indexes) — absorbing a newer set prunes these.
        self._latest_only_turn: tuple[dict, list[int]] | None = None
        # Observability tap: called with the exact request body just before it
        # goes on the wire (from generate's thread). The body must be treated
        # as read-only — it shares structure with the live history.
        self.on_request: Callable[[dict], None] | None = None
        # Token counts of the newest response — prompt/cached/output, surfaced
        # on the trace snapshot (cache-hit observability).
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
        return sum(1 for message in self._history if _is_frame_turn(message))

    @staticmethod
    def user_message(text: str, images: list[bytes]) -> dict:
        content: list[dict] = [{"type": "text", "text": text}]
        for jpeg in images:
            url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": "user", "content": content}

    def generate(
        self,
        user_message: dict,
        tools: list[dict],
        system: str,
        on_speech: Callable[[str], None] | None = None,
        *,
        latest_only_images: list[int] | None = None,
    ) -> dict:
        """Blocking network call — safe on a worker thread (history is only read).

        The reply streams in; every plain-text delta is handed to ``on_speech``
        as it arrives, which is what lets the robot start talking at the first
        sentence boundary. Returns the assembled reply for :meth:`absorb`.

        ``latest_only_images`` mirrors :meth:`absorb`'s: when this message
        carries wrist frames, the previous turn's copies are masked out of the
        request here — absorb's durable prune runs only after the response, so
        without this every request would ship two wrist frames. The masking
        HERE never mutates history — shallow copies only. Durable mutation
        (absorb, _prune) runs on the loop thread strictly between generate
        calls, by which point an abandoned turn's orphaned request has already
        serialized its body.
        """
        # Merged here, not left to the transport, so the observability tap below
        # sees the body that actually goes out (the transport's merge is a no-op on it).
        body = self._request(user_message, tools, system, latest_only_images) | self._transport.extra_body
        if self.on_request is not None:
            self.on_request(body)
        # Usage rides the reply and is committed by absorb, on the loop thread:
        # writing self.last_usage here would let an abandoned turn's orphaned
        # request overwrite the committed turn's counts.
        return _assemble(self._transport.stream(body), on_speech)

    def absorb(self, user_message: dict, response: dict, *, latest_only_images: list[int] | None = None) -> Decision:
        """Commit the exchange to history and distill the model's Decision.

        The assistant message is stored exactly as it was assembled — content,
        tool calls with their ids, and any ``extra_content`` a server attached
        (Gemini 3's thought signature rides there, and is accepted back only
        verbatim).

        ``latest_only_images`` names positions in this message's image list
        (order given to :meth:`user_message`) that must only ever appear in the
        newest turn — the wrist camera: a stale gripper close-up reads as
        current grasp state and misleads the model, so absorbing a new one
        prunes the previous turn's copy on the spot.
        """
        decision = _decision_from(response)
        usage = response.get("usage") or {}
        self.last_usage = {
            "prompt": usage.get("prompt_tokens", 0),
            "cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "output": usage.get("completion_tokens", 0),
        }
        self._history.append(user_message)
        if latest_only_images:
            if self._latest_only_turn is not None:
                stale, indexes = self._latest_only_turn
                stale["content"] = _masked(_parts(stale), indexes, _WRIST_FRAME_REMOVED)
            image_parts = [i for i, p in enumerate(_parts(user_message)) if _is_image(p)]
            self._latest_only_turn = (
                user_message,
                [image_parts[i] for i in latest_only_images if i < len(image_parts)],
            )
        self._history.append(response["message"])
        self._prune()
        return decision

    def add_tool_outcomes(self, outcomes: list[tuple[ToolCall, str]]) -> None:
        """Answer the model's tool calls (a server requires one result per call)."""
        for call, outcome in outcomes:
            self._history.append({"role": "tool", "tool_call_id": call.id, "content": outcome})

    def _request(
        self, user_message: dict, tools: list[dict], system: str, latest_only_images: list[int] | None
    ) -> dict:
        messages = [{"role": "system", "content": system}, *self._reference, *self._history, user_message]
        if latest_only_images and self._latest_only_turn is not None:
            stale, indexes = self._latest_only_turn
            masked = {**stale, "content": _masked(_parts(stale), indexes, _WRIST_FRAME_REMOVED)}
            messages = [masked if message is stale else message for message in messages]
        body: dict = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
        if self._thinking:
            body["reasoning_effort"] = self._thinking
        return body

    def _prune(self) -> None:
        """Compact the history in chunks, never one entry per turn.

        An implicit prompt cache only hits when a prior request is a byte
        prefix of the new one, so evicting or masking anything every turn
        forfeits the cache on every request. The history therefore grows
        append-only until a budget trips, and one compaction then evicts down
        to half the cap and masks old frames in the same step — a single cache
        miss per window instead of one per turn.
        """
        history = self._history
        keep = max(self._max_image_turns, 0)
        if len(history) > self._max_history:
            del history[: len(history) - self._max_history // 2]
            # The history must start with a plain user turn: a leading assistant
            # message or an orphaned tool result (whose call was just evicted) is rejected.
            while history and history[0].get("role") != "user":
                history.pop(0)
            # An evicted turn must not stay pinned as the latest-only holder: absorb
            # would "prune" an orphan dict nothing reads, and the reference would
            # keep its base64 wrist frame alive for as long as the arm feed is stale.
            if self._latest_only_turn is not None and not any(m is self._latest_only_turn[0] for m in history):
                self._latest_only_turn = None
        elif self.image_turn_count <= 2 * keep:
            return  # under both budgets: stay append-only, the cache is warm
        # Mask down to the newest few frame turns (none at all if the keep-count
        # is zero or nonsensical). Until a compaction, older frames ride the
        # cached prefix at a tenth of the input price — cheap to carry.
        frame_turns = [message for message in history if _is_frame_turn(message)]
        for message in frame_turns[:-keep] if keep else frame_turns:
            message["content"] = [dict(_FRAME_REMOVED) if _is_image(p) else p for p in _parts(message)]


def _assemble(chunks: Chunks, on_speech: Callable[[str], None] | None) -> dict:
    """Stream -> the assistant message to replay, plus thoughts and usage."""
    speech: list[str] = []
    thoughts: list[str] = []
    calls: dict[int, dict] = {}
    extra: dict = {}
    usage: dict = {}
    finish_reason = ""
    for chunk in chunks:
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            if delta.get("content"):
                speech.append(delta["content"])
                if on_speech is not None:
                    on_speech(delta["content"])
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                thoughts.append(reasoning)
            for fragment in delta.get("tool_calls") or []:
                _merge_call(calls, fragment)
            if delta.get("extra_content"):
                extra = delta["extra_content"]
    if calls and finish_reason not in _FINISHED:
        # A cut-off stream (length cap, filter, dropped connection) may hold half a
        # tool call: committing it would replay a call the model never finished
        # asking for. Text alone is committed as it stands — on_speech has already
        # voiced it, and failing the turn would drop it from history and say it
        # again on the retry.
        raise RuntimeError(f"the model stopped early: finish_reason={finish_reason or 'missing'}")
    message: dict = {"role": "assistant", "content": "".join(speech)}
    if calls:
        message["tool_calls"] = [_with_id(calls[index]) for index in sorted(calls)]
    if extra:
        message["extra_content"] = extra
    if not message["content"] and not calls:
        # A 200 stream with nothing in it: committing it would record a silent,
        # answerless exchange — raise instead, so the turn's retry path keeps
        # the events queued and the failure is visible.
        raise RuntimeError(f"the model returned no content: finish_reason={finish_reason}")
    return {"message": message, "thoughts": "".join(thoughts), "usage": usage, "finish_reason": finish_reason}


def _merge_call(calls: dict[int, dict], fragment: dict) -> None:
    """Fold one streamed tool-call fragment into the call at its index."""
    call = calls.setdefault(
        fragment.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    )
    if fragment.get("id"):
        call["id"] = fragment["id"]
    function = fragment.get("function") or {}
    if function.get("name"):
        call["function"]["name"] = function["name"]
    if function.get("arguments"):
        call["function"]["arguments"] += function["arguments"]
    if fragment.get("extra_content"):
        call["extra_content"] = fragment["extra_content"]


def _with_id(call: dict) -> dict:
    if not call["id"]:
        call["id"] = f"call_{uuid.uuid4().hex[:12]}"  # the tool result must quote an id back
    return call


def _parts(message: dict) -> list[dict]:
    """A message's content parts ([] for the plain-string content of a reply or a tool result)."""
    content = message.get("content")
    return content if isinstance(content, list) else []


def _is_image(part: dict) -> bool:
    return part.get("type") == "image_url"


def _is_frame_turn(message: dict) -> bool:
    return message.get("role") == "user" and any(_is_image(p) for p in _parts(message))


def _masked(parts: list[dict], indexes: list[int], placeholder: dict) -> list[dict]:
    return [dict(placeholder) if i in indexes and _is_image(p) else p for i, p in enumerate(parts)]


def _decision_from(response: dict) -> Decision:
    message = response.get("message") or {}
    calls = [
        ToolCall(
            name=(call.get("function") or {}).get("name") or "",
            args=_parse_args((call.get("function") or {}).get("arguments")),
            id=call.get("id") or "",
        )
        for call in message.get("tool_calls") or []
    ]
    return Decision(
        speech=_clean_speech(message.get("content")),
        thoughts=(response.get("thoughts") or "").strip() or None,
        calls=calls,
    )


def _parse_args(arguments: object) -> dict:
    if isinstance(arguments, dict):
        return arguments  # a few servers hand back an object instead of a JSON string
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    speech, _ = split_tool_narration(speech.strip())
    return speech if re.search(r"[a-zA-Z0-9]", speech) else None
