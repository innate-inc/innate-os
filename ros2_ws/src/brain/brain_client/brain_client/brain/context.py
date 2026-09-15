# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's bounded conversation, as a tuple of frozen messages.

One :meth:`ChatContext.generate` per agent turn: the reply streams in — text
deltas feed the speaker as they arrive — and is distilled into a plain
:class:`Decision` the loop acts on. Vendors' thought summaries surface as
:class:`~brain_client.llm.Thought` parts and reach the app as robot thoughts.
The bounded history is compacted in chunks — old camera frames masked,
oldest turns evicted — so requests stay small while consecutive requests keep
the shared prefix every vendor's prompt cache needs (see :meth:`_prune`).
Every edit replaces the history tuple; nothing is mutated in place.

Claude signs each thinking block against everything before it — system
prompt, tool set, earlier turns — and rejects the block once that prefix
changes (Gemini's signatures and OpenAI's encrypted reasoning are not bound
this way). So any edit — a masked frame, an evicted turn, a system prompt or
tool list that differs from the last request's — strips the stored thoughts
first; dropping them is always accepted, replaying them stale is a 400.

Threading contract: :meth:`generate` is the only blocking network call and
only *reads* the history, so the agent awaits it on a worker thread
(``asyncio.to_thread``). All history replacement (:meth:`absorb`,
:meth:`add_tool_outcomes`) happens in the agent's coroutine, strictly between
generate calls, and :meth:`clear` only runs while the loop task is stopped —
there is no concurrent access to a context by construction, not by lock.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from brain_client.llm import (
    Image,
    Json,
    Message,
    Provider,
    Reply,
    Request,
    Role,
    Text,
    Thinking,
    Thought,
    Tool,
    ToolCall,
    ToolResult,
)
from brain_client.llm.policy import (
    History,
    evict_to,
    image_turns,
    mask_latest_only,
    pin_prefix,
    strip_thoughts,
    window_images,
)

if TYPE_CHECKING:
    from brain_client.llm.types import Part

Prefix = tuple[str, tuple[Tool, ...]]
"""What a request puts before the history: the system prompt and the tool set."""


@dataclass
class Decision:
    """What the model wants the robot to do this turn."""

    speech: str | None = None
    thoughts: str | None = None
    calls: list[ToolCall] = field(default_factory=list)


class ChatContext:
    """Bounded model context: one generate() per agent turn, on any provider."""

    def __init__(
        self,
        provider: Provider,
        *,
        thinking: Thinking,
        max_history: int,
        max_image_turns: int,
        reference: Sequence[Message] | None = None,
    ):
        self._provider = provider
        self._thinking = thinking
        self._max_history = max_history
        self._max_image_turns = max_image_turns
        # Pinned turns (the robot's self-portrait) prepended to every request
        # but never stored in history — immune to pruning and clear().
        self._reference: tuple[Message, ...] = tuple(reference or ())
        self._history: History = ()
        # The one history turn still carrying latest-only frames (wrist camera),
        # as (message, part indexes) — absorbing a newer set masks these.
        self._latest_only: tuple[Message, tuple[int, ...]] | None = None
        # The prefix the committed history's thoughts were signed against, and
        # the one the request in flight was built with (see the module docstring).
        self._prefix: Prefix | None = None
        self._sent: Prefix | None = None
        # Observability tap: called with the request as the monitor renders it
        # (see :func:`trace_body`) just before it goes on the wire, from
        # generate's thread.
        self.on_request: Callable[[Json], None] | None = None
        # Token counts of the newest response — prompt/cached/output — surfaced
        # on the trace snapshot (cache-hit observability).
        self.last_usage: dict[str, int] = {}

    def clear(self) -> None:
        self._history = ()
        self._latest_only = None
        self._prefix = self._sent = None
        self.last_usage = {}

    @property
    def history(self) -> History:
        return self._history

    @property
    def history_len(self) -> int:
        return len(self._history)

    @property
    def image_turn_count(self) -> int:
        """History turns still carrying camera frames (pruning keeps the newest few)."""
        return len(image_turns(self._history))

    @staticmethod
    def user_message(text: str, images: Sequence[bytes]) -> Message:
        return Message(Role.USER, (Text(text), *(Image(jpeg) for jpeg in images)))

    def generate(
        self,
        message: Message,
        tools: Sequence[Tool],
        system: str,
        on_speech: Callable[[str], None] | None = None,
        *,
        latest_only_images: Sequence[int] | None = None,
    ) -> Reply:
        """Blocking network call — safe on a worker thread (history is only read).

        The reply streams in; every plain-text delta is handed to ``on_speech``
        as it arrives, which is what lets the robot start talking at the first
        sentence boundary. Returns the fully assembled reply.

        ``latest_only_images`` mirrors :meth:`absorb`'s: when this message
        carries wrist frames, the previous turn's copies are masked out of the
        request here — absorb's durable mask runs only after the response, so
        without this every request would ship two wrist frames. Masking HERE
        never touches the stored history. Durable replacement (absorb, _prune)
        runs on the loop thread strictly between generate calls, by which point
        an abandoned turn's orphaned request has already been sent.
        """
        prefix: Prefix = (system, tuple(tools))
        # The one write on this thread, made before the blocking call: turns run
        # one at a time, so an abandoned turn's orphan can never record after a newer one.
        self._sent = prefix
        history = self._history
        stale_wrist = self._latest_only if latest_only_images else None
        if stale_wrist is not None:
            history = mask_latest_only(history, *stale_wrist)
        if stale_wrist is not None or prefix != self._prefix:
            history = strip_thoughts(history)
        request = pin_prefix(
            Request(
                system=system,
                messages=(*self._reference, *history, message),
                tools=tuple(tools),
                thinking=self._thinking,
                thought_summaries=True,
            )
        )
        if self.on_request is not None:
            self.on_request(trace_body(request))
        reply = self._provider.run(request, on_text=on_speech)
        if not _has_content(reply.message):
            # An empty reply (a safety block, a malformed function call, an
            # empty candidate): committing it would record a silent, answerless
            # exchange — raise instead, so the turn's retry path keeps the
            # events queued and the failure is visible.
            why = f"{reply.finish}" + (f" ({reply.detail})" if reply.detail else "")
            raise RuntimeError(f"{self._provider.model.name} returned no content: finish={why}")
        # Usage rides the reply and is committed by absorb, on the loop
        # thread: writing self.last_usage here would let an abandoned turn's
        # orphaned request overwrite the committed turn's counts.
        return reply

    def absorb(self, message: Message, reply: Reply, *, latest_only_images: Sequence[int] | None = None) -> Decision:
        """Commit the exchange to history and distill the model's Decision.

        The model turn is stored whole — its parts carry the signatures vendors
        demand back on the next request (Gemini 3, Claude, OpenAI).

        ``latest_only_images`` names positions in this message's image list
        (order given to :meth:`user_message`) that must only ever appear in the
        newest turn — the wrist camera: a stale gripper close-up reads as
        current grasp state and misleads the model, so absorbing a new one
        masks the previous turn's copy on the spot.
        """
        decision = decision_from(reply.message)
        self.last_usage = {"prompt": reply.usage.prompt, "cached": reply.usage.cached, "output": reply.usage.output}
        history: History = (*self._history, message)
        stale_wrist = self._latest_only if latest_only_images else None
        if stale_wrist is not None:
            history = mask_latest_only(history, *stale_wrist)
        if stale_wrist is not None or self._sent != self._prefix:
            history = strip_thoughts(history)  # what the request replayed is what the history keeps
        self._prefix = self._sent
        if latest_only_images:
            images = message.image_indexes()
            self._latest_only = (message, tuple(images[i] for i in latest_only_images if i < len(images)))
        self._history = self._prune((*history, reply.message))
        return decision

    def add_tool_outcomes(self, outcomes: Sequence[tuple[ToolCall, str]]) -> None:
        """Answer the model's tool calls (every vendor requires one result per call)."""
        if not outcomes:
            return
        results = tuple(ToolResult(call.id, call.name, outcome) for call, outcome in outcomes)
        self._history = (*self._history, Message(Role.TOOL, results))

    def _prune(self, history: History) -> History:
        """Compact the history in chunks, never one entry per turn.

        Prompt caches (Gemini's and OpenAI's implicit, Claude's breakpoints)
        only hit when a prior request is a prefix of the new one, so evicting
        or masking anything every turn forfeits the cache on every request.
        The history therefore grows append-only until a budget trips, and one
        compaction then evicts down to half the cap and masks old frames in
        the same step — a single cache miss per window instead of one per turn.
        """
        keep = max(self._max_image_turns, 0)
        if len(history) > self._max_history:
            history = evict_to(history, self._max_history // 2)
        elif len(image_turns(history)) <= 2 * keep:
            return history  # under both budgets: stay append-only, the cache is warm
        # Mask down to the newest few frame turns (none at all if the keep-count
        # is zero or nonsensical). Until a compaction, older frames ride the
        # cached prefix at a fraction of the input price — cheap to carry.
        history = window_images(history, keep)
        # A turn evicted or masked here must not stay pinned as the latest-only
        # holder: the reference would keep its wrist frame alive for as long as
        # the arm feed is stale.
        if self._latest_only is not None and not any(m is self._latest_only[0] for m in history):
            self._latest_only = None
        return strip_thoughts(history)


def _has_content(message: Message) -> bool:
    """Speech or a call — a thought alone is not a reply, and replaying one dangling is a 400 on OpenAI."""
    return any(isinstance(p, ToolCall) or (isinstance(p, Text) and p.text) for p in message.parts)


def decision_from(message: Message) -> Decision:
    decision = Decision(calls=message.calls())
    decision.thoughts = message.thoughts().strip() or None
    decision.speech = _clean_speech(message.text().strip())
    return decision


def trace_body(request: Request) -> Json:
    """The request in the shape the webapp's brain monitor renders — a
    ``role``/``parts`` transcript with inline images, calls and results. It is
    the monitor's dialect, not any vendor's wire."""
    return {
        "systemInstruction": {"parts": [{"text": request.system}]},
        "contents": [_trace_content(m) for m in request.messages],
        "generationConfig": {"thinking": request.thinking} if request.thinking else {},
        "tools": [
            {
                "functionDeclarations": [
                    {"name": t.name, "description": t.description, "parameters": t.parameters} for t in request.tools
                ]
            }
        ],
    }


def _trace_content(message: Message) -> Json:
    role = "model" if message.role == Role.ASSISTANT else "user"
    return {"role": role, "parts": [_trace_part(part) for part in message.parts]}


def _trace_part(part: Part) -> Json:
    if isinstance(part, Text):
        return {"text": part.text}
    if isinstance(part, Thought):
        return {"text": part.text, "thought": True}
    if isinstance(part, Image):
        return {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(part.jpeg).decode()}}
    if isinstance(part, ToolCall):
        return {"functionCall": {"name": part.name, "args": part.args}}
    if isinstance(part, ToolResult):
        return {"functionResponse": {"name": part.name, "response": {"outcome": part.text}}}
    return {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(part.wav).decode()}}


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
