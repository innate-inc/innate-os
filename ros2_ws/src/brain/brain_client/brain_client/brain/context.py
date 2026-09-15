# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's bounded conversation, in pydantic-ai's message model.

One :meth:`ChatContext.generate` per agent turn: the reply streams in — text
deltas feed the speaker as they arrive — and is distilled into a plain
:class:`Decision` the loop acts on. Vendors' thought summaries (Gemini's
``thought`` parts, Claude's summarized thinking) surface as ``ThinkingPart``s
and reach the app as robot thoughts. The bounded history is compacted in
chunks — old camera frames masked, oldest turns evicted — so requests stay
small while consecutive requests keep the shared prefix every vendor's prompt
cache needs (see :meth:`ChatContext._prune`).

Threading contract: :meth:`generate` is the only blocking network call and
only *reads* the history, so the agent awaits it on a worker thread
(``asyncio.to_thread``). All history mutation (:meth:`absorb`,
:meth:`add_tool_outcomes`) happens in the agent's coroutine, strictly between
generate calls, and :meth:`clear` only runs while the loop task is stopped —
there is no concurrent access to a context by construction, not by lock.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from pydantic_ai.direct import model_request_stream_sync
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

if TYPE_CHECKING:
    from pydantic_ai.models import Model
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.tools import ToolDefinition

_FRAME_REMOVED = "[older camera frame removed]"
_WRIST_FRAME_REMOVED = "[older wrist camera frame removed]"


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
    """Bounded model context: one generate() per agent turn, on any pydantic-ai model."""

    def __init__(
        self,
        model: Model,
        *,
        settings: ModelSettings,
        max_history: int,
        max_image_turns: int,
        reference: Sequence[ModelMessage] | None = None,
    ):
        self._model = model
        self._settings = settings
        self._max_history = max_history
        self._max_image_turns = max_image_turns
        # Pinned turns (the robot's self-portrait) prepended to every request
        # but never stored in history — immune to pruning and clear().
        self._reference: list[ModelMessage] = list(reference or [])
        self._history: list[ModelMessage] = []
        # The one history turn still carrying latest-only frames (wrist camera),
        # as (request, content indexes) — absorbing a newer set prunes these.
        self._latest_only_turn: tuple[ModelRequest, list[int]] | None = None
        # Observability tap: called with the request as the monitor renders it
        # (see :func:`trace_body`) just before it goes on the wire, from
        # generate's thread.
        self.on_request: Callable[[dict], None] | None = None
        # Token counts of the newest response — prompt/cached/output — surfaced
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
        return sum(1 for m in self._history if isinstance(m, ModelRequest) and _image_indexes(m))

    @staticmethod
    def user_message(text: str, images: Sequence[bytes]) -> ModelRequest:
        content: list[UserContent] = [text, *(BinaryContent(jpeg, media_type="image/jpeg") for jpeg in images)]
        return ModelRequest(parts=[UserPromptPart(content=content)])

    def generate(
        self,
        message: ModelRequest,
        tools: Sequence[ToolDefinition],
        system: str,
        on_speech: Callable[[str], None] | None = None,
        *,
        latest_only_images: Sequence[int] | None = None,
    ) -> ModelResponse:
        """Blocking network call — safe on a worker thread (history is only read).

        The reply streams in; every plain-text delta is handed to ``on_speech``
        as it arrives, which is what lets the robot start talking at the first
        sentence boundary. Returns the fully assembled response.

        ``latest_only_images`` mirrors :meth:`absorb`'s: when this message
        carries wrist frames, the previous turn's copies are masked out of the
        request here — absorb's durable prune runs only after the response, so
        without this every request would ship two wrist frames. The masking
        HERE never mutates history — copies only. Durable mutation (absorb,
        _prune) runs on the loop thread strictly between generate calls, by
        which point an abandoned turn's orphaned request has already been sent.
        """
        message.instructions = system  # pydantic-ai sends the newest request's instructions as the system prompt
        messages: list[ModelMessage] = [*self._reference, *self._history, message]
        if latest_only_images and self._latest_only_turn is not None:
            stale, indexes = self._latest_only_turn
            masked = _masked(stale, indexes, _WRIST_FRAME_REMOVED)
            messages = [masked if m is stale else m for m in messages]
        if self.on_request is not None:
            self.on_request(trace_body(system, messages, tools, self._settings))
        parameters = ModelRequestParameters(function_tools=list(tools))
        with model_request_stream_sync(
            self._model, messages, model_settings=self._settings, model_request_parameters=parameters
        ) as stream:
            for event in stream:
                if on_speech is not None and (spoken := _text_delta(event)):
                    on_speech(spoken)
            response = stream.response
        if not _has_content(response):
            # An empty reply (a safety block, a malformed function call, an
            # empty candidate): committing it would record a silent, answerless
            # exchange — raise instead, so the turn's retry path keeps the
            # events queued and the failure is visible.
            raise RuntimeError(f"{self._model.model_name} returned no content: finish_reason={response.finish_reason}")
        # Usage rides the response and is committed by absorb, on the loop
        # thread: writing self.last_usage here would let an abandoned turn's
        # orphaned request overwrite the committed turn's counts.
        return response

    def absorb(
        self, message: ModelRequest, response: ModelResponse, *, latest_only_images: Sequence[int] | None = None
    ) -> Decision:
        """Commit the exchange to history and distill the model's Decision.

        The model turn is stored verbatim — thinking parts carry the signatures
        vendors demand back on the next request (Gemini 3, Claude), so nothing
        is dropped.

        ``latest_only_images`` names positions in this message's image list
        (order given to :meth:`user_message`) that must only ever appear in the
        newest turn — the wrist camera: a stale gripper close-up reads as
        current grasp state and misleads the model, so absorbing a new one
        prunes the previous turn's copy on the spot.
        """
        decision = decision_from(response)
        usage = response.usage
        self.last_usage = {
            "prompt": usage.input_tokens,
            "cached": usage.cache_read_tokens,
            "output": usage.output_tokens,
        }
        self._history.append(message)
        if latest_only_images:
            if self._latest_only_turn is not None:
                stale, indexes = self._latest_only_turn
                stale.parts = _masked(stale, indexes, _WRIST_FRAME_REMOVED).parts
            images = _image_indexes(message)
            self._latest_only_turn = (message, [images[i] for i in latest_only_images if i < len(images)])
        self._history.append(response)
        self._prune()
        return decision

    def add_tool_outcomes(self, outcomes: Sequence[tuple[ToolCall, str]]) -> None:
        """Answer the model's tool calls (every vendor requires one result per call)."""
        if not outcomes:
            return
        self._history.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name=call.name, content={"outcome": outcome}, tool_call_id=call.id)
                    for call, outcome in outcomes
                ]
            )
        )

    def _prune(self) -> None:
        """Compact the history in chunks, never one entry per turn.

        Prompt caches (Gemini's and OpenAI's implicit, Claude's breakpoints)
        only hit when a prior request is a prefix of the new one, so evicting
        or masking anything every turn forfeits the cache on every request.
        The history therefore grows append-only until a budget trips, and one
        compaction then evicts down to half the cap and masks old frames in
        the same step — a single cache miss per window instead of one per turn.
        """
        history = self._history
        keep = max(self._max_image_turns, 0)
        if len(history) > self._max_history:
            del history[: len(history) - self._max_history // 2]
            # The history must start with a plain user turn: a leading model turn or
            # an orphaned tool result (whose call was just evicted) is rejected.
            while history and not _plain_user_turn(history[0]):
                history.pop(0)
            # An evicted turn must not stay pinned as the latest-only holder: absorb
            # would "prune" an orphan nothing reads, and the reference would keep
            # its wrist frame alive for as long as the arm feed is stale.
            if self._latest_only_turn is not None and not any(m is self._latest_only_turn[0] for m in history):
                self._latest_only_turn = None
        elif self.image_turn_count <= 2 * keep:
            return  # under both budgets: stay append-only, the cache is warm
        # Mask down to the newest few frame turns (none at all if the keep-count
        # is zero or nonsensical). Until a compaction, older frames ride the
        # cached prefix at a fraction of the input price — cheap to carry.
        image_turns = [m for m in history if isinstance(m, ModelRequest) and _image_indexes(m)]
        for request in image_turns[:-keep] if keep else image_turns:
            request.parts = _masked(request, _image_indexes(request), _FRAME_REMOVED).parts


def _image_indexes(request: ModelRequest) -> list[int]:
    """Positions of the camera frames in a user turn's content list."""
    for part in request.parts:
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
            return [i for i, item in enumerate(part.content) if isinstance(item, BinaryContent)]
    return []


def _masked(request: ModelRequest, indexes: Sequence[int], placeholder: str) -> ModelRequest:
    """A copy of the user turn with the frames at ``indexes`` replaced by ``placeholder``."""
    parts = []
    for part in request.parts:
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
            content: list[UserContent] = [
                placeholder if i in indexes and isinstance(item, BinaryContent) else item
                for i, item in enumerate(part.content)
            ]
            part = replace(part, content=content)
        parts.append(part)
    return replace(request, parts=parts)


def _has_content(response: ModelResponse) -> bool:
    return any(
        isinstance(p, (ToolCallPart, ThinkingPart)) or (isinstance(p, TextPart) and p.content) for p in response.parts
    )


def _plain_user_turn(message: ModelMessage) -> bool:
    return isinstance(message, ModelRequest) and not any(isinstance(p, ToolReturnPart) for p in message.parts)


def _text_delta(event: object) -> str:
    """The spoken text an event carries: a text part's first chunk or a later delta."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    return ""


def decision_from(response: ModelResponse) -> Decision:
    decision = Decision()
    for part in response.parts:
        if isinstance(part, ToolCallPart):
            decision.calls.append(ToolCall(name=part.tool_name, args=part.args_as_dict(), id=part.tool_call_id))
        elif isinstance(part, ThinkingPart) and part.content:
            decision.thoughts = f"{decision.thoughts or ''}{part.content}".strip()
        elif isinstance(part, TextPart) and part.content:
            decision.speech = f"{decision.speech or ''}{part.content}".strip()
    decision.speech = _clean_speech(decision.speech)
    return decision


def trace_body(
    system: str, messages: Sequence[ModelMessage], tools: Sequence[ToolDefinition], settings: ModelSettings
) -> dict:
    """The request in the shape the webapp's brain monitor renders — a
    ``role``/``parts`` transcript with inline images, calls and results. It is
    the monitor's dialect, not any vendor's wire."""
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [_trace_content(m) for m in messages],
        "generationConfig": {k: v for k, v in settings.items() if k in ("thinking", "extra_body")},
        "tools": [
            {
                "functionDeclarations": [
                    {"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
                    for t in tools
                ]
            }
        ],
    }


def _trace_content(message: ModelMessage) -> dict:
    parts: list[dict] = []
    if isinstance(message, ModelResponse):
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                parts.append({"functionCall": {"name": part.tool_name, "args": part.args_as_dict()}})
            elif isinstance(part, ThinkingPart):
                parts.append({"text": part.content, "thought": True})
            elif isinstance(part, TextPart):
                parts.append({"text": part.content})
        return {"role": "model", "parts": parts}
    for part in message.parts:
        if isinstance(part, ToolReturnPart):
            parts.append({"functionResponse": {"name": part.tool_name, "response": part.content}})
        elif isinstance(part, UserPromptPart):
            for item in [part.content] if isinstance(part.content, str) else part.content:
                if isinstance(item, BinaryContent):
                    parts.append(
                        {"inlineData": {"mimeType": item.media_type, "data": base64.b64encode(item.data).decode()}}
                    )
                else:
                    parts.append({"text": str(item)})
    return {"role": "user", "parts": parts}


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
