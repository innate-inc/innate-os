# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pipeline latency marks: one small JSON object per stage boundary.

Deliberately not `/brain/trace` — that topic's heavy events carry every frame
the model was sent, so a monitor subscribing to it inflates the very latency it
came to measure. These marks stay under ~200 bytes and are always published.

Every producer runs on the robot's own clock (`input_manager_node` and
`brain_client_node` share a host), so the wall-clock stamps are directly
comparable and a consumer can stitch a waterfall from them without any clock
sync. The microphone's marks ride `/input_manager/telemetry` under
``kind: "latency"`` rather than a second publisher — it is an input device with
no ROS of its own.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from brain_client.common.enums import StrEnum

LATENCY_TOPIC = "/brain/latency"


class Stage(StrEnum):
    """Stage boundaries, in pipeline order — the monitor keys off these values."""

    UTTERANCE_CLOSED = "utterance_closed"  # local VAD called the end of speech (batch backends only)
    STT_REQUEST = "stt_request"
    STT_DONE = "stt_done"
    # Realtime backends only: the vendor endpoints inside its own session, so
    # these are the only boundaries it hands back before the transcript.
    STT_PARTIAL = "stt_partial"  # first interim result of this utterance
    VENDOR_SPEECH_END = "vendor_speech_end"  # the vendor's VAD called the end of speech
    CHAT_IN = "chat_in"  # transcript handed to the brain (every backend)
    EVENT_QUEUED = "event_queued"
    TURN_START = "turn_start"
    REQUEST_SENT = "request_sent"
    FIRST_TEXT = "first_text"  # first non-thought delta: when speech *could* start
    MODEL_CALL = "model_call"  # the model emitted a functionCall part (not yet dispatched)
    STREAM_DONE = "stream_done"
    DECISION = "decision"  # what the turn actually resolved to: words, calls, or neither
    TOOL_CALL = "tool_call"  # a tool/skill dispatch begins
    TOOL_DONE = "tool_done"  # ...and returns; for a skill that is the goal sent, not run
    TURN_DROPPED = "turn_dropped"  # preempted or deactivated: the chain below is broken
    SPEECH_QUEUED = "speech_queued"
    TTS_START = "tts_start"
    TTS_REQUEST = "tts_request"
    TTS_FIRST_BYTE = "tts_first_byte"
    TTS_PLAY_DONE = "tts_play_done"


LatencySink = Callable[[dict], None]
"""Publishes one mark. Wall clock and stage name are already in the dict."""


class Mark(Protocol):
    def __call__(self, stage: Stage, **fields: object) -> None: ...


def _noop(stage: Stage, **fields: object) -> None:
    pass


def marker(sink: LatencySink | None) -> Mark:
    """A `mark(stage, **fields)` that stamps the wall clock, or a no-op when unwired."""
    if sink is None:
        return _noop

    def mark(stage: Stage, **fields: object) -> None:
        sink({"t": time.time(), "stage": stage, **fields})

    return mark
