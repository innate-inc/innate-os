# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing recall over the robot's spatial memory.

A thin client of the brain's ``/brain/search_memory`` action — the Gemini
context cache, transport, and credentials all live server-side; a skill only
ever sees a typed :class:`RecallVerdict`. Declared like any interface::

    memory: SpatialMemory

    def execute(self, query: str):
        recall = self.memory.begin(query)
        verdict = self.wait_for(recall, timeout=60.0)

``begin`` returns a zero-arg reader that stays None until the verdict lands,
so the wait runs through ``self.wait_for`` — cancel-aware like every blocking
framework call. A skill that stops waiting simply abandons the goal; the
search concludes server-side and its verdict is dropped. ``recognize`` is the
reverse question, through ``/brain/recognize_place``, with the same contract.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from brain_messages.action import RecognizePlace, SearchMemory
from rclpy.action import ActionClient

from brain_client.skills.types import cancellable_sleep

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from rclpy.impl.rcutils_logger import RcutilsLogger
    from rclpy.node import Node
    from rclpy.task import Future

_SERVER_WAIT_SEC = 2.0  # how long a call waits for its action server to exist


@dataclass(frozen=True)
class RecallVerdict:
    """One memory search's outcome. ``error`` non-empty means the search
    itself failed — distinct from a clean no-match (``found=False`` with an
    explanation). ``message`` is the model-ready sentence; the pose fields
    chain directly into navigation."""

    found: bool
    message: str
    explanation: str = ""
    error: str = ""
    x: float = 0.0
    y: float = 0.0
    theta: float = 0.0
    seen_stamp: float = 0.0
    image: bytes | None = None
    latency_sec: float = 0.0
    cached: bool = False


@dataclass(frozen=True)
class PlaceMatch:
    """A remembered frame the vision model says one of the views shows."""

    frame: int
    view: int  # index into the views passed to recognize()
    confidence: float
    x: float  # map pose the frame was recorded from
    y: float
    theta: float
    evidence: str


@dataclass(frozen=True)
class PlaceVerdict:
    """``error`` non-empty means recognition itself failed; no matches with no
    error means nothing distinctive was in view. The move is the model's
    suggestion for a more recognizable view: turn first (+ = left), then drive."""

    matches: tuple[PlaceMatch, ...] = ()
    turn_deg: float = 0.0
    forward_m: float = 0.0
    move_reason: str = ""
    error: str = ""
    latency_sec: float = 0.0


_V = TypeVar("_V")


class SpatialMemory:
    def __init__(self, node: Node, logger: RcutilsLogger):
        self._logger = logger
        self._client = ActionClient(node, SearchMemory, "/brain/search_memory")
        self._recognize_client = ActionClient(node, RecognizePlace, "/brain/recognize_place")

    def begin(self, query: str) -> Callable[[], RecallVerdict | None]:
        """Start a search; hand the returned reader to ``self.wait_for``."""
        goal = SearchMemory.Goal()
        goal.query = str(query)
        return _start(self._client, goal, _from_result, _error_verdict, "memory search")

    def recognize(self, views: Sequence[tuple[bytes, str]], context: str = "") -> Callable[[], PlaceVerdict | None]:
        """Ask which remembered views show where the robot is: ``views`` are
        (JPEG, where it was taken relative to the robot now), oldest first.
        Hand the returned reader to ``self.wait_for``."""
        goal = RecognizePlace.Goal()
        goal.images_b64 = [base64.b64encode(jpeg).decode() for jpeg, _ in views]
        goal.labels = [label for _, label in views]
        goal.context = context
        return _start(
            self._recognize_client, goal, _from_recognize_result, lambda e: PlaceVerdict(error=e), "place recognition"
        )


def _start(
    client: ActionClient, goal: Any, convert: Callable[[Any], _V], failed: Callable[[str], _V], what: str
) -> Callable[[], _V | None]:
    """Send ``goal``; the returned reader stays None until the converted result (or a ``failed`` verdict) lands."""
    holder: list[_V | None] = [None]

    def conclude(verdict: _V) -> None:
        holder[0] = verdict

    # rclpy's wait_for_server is a time.sleep poll — sliced here so a Stop
    # pressed while the server is absent unwinds instead of blocking out
    # the whole timeout (cancellable_sleep raises SkillCancelled).
    deadline = time.monotonic() + _SERVER_WAIT_SEC
    while not client.wait_for_server(timeout_sec=0.0):
        if time.monotonic() >= deadline:
            conclude(failed(f"{what} unavailable — is the brain node running?"))
            return lambda: holder[0]
        cancellable_sleep(0.1)

    def on_result(future: Future) -> None:
        try:
            response = future.result()
            if response is None:
                raise RuntimeError("empty result")
            conclude(convert(response.result))
        except Exception as error:  # noqa: BLE001 — a lost result must still unblock the waiter
            conclude(failed(f"{what} result lost: {error}"))

    def on_goal(future: Future) -> None:
        try:
            handle = future.result()
        except Exception as error:  # noqa: BLE001 — same: the waiter needs an answer, not a hang
            conclude(failed(f"{what} goal failed: {error}"))
            return
        if handle is None or not handle.accepted:
            conclude(failed(f"{what} goal rejected"))
            return
        handle.get_result_async().add_done_callback(on_result)

    client.send_goal_async(goal).add_done_callback(on_goal)
    return lambda: holder[0]


def _error_verdict(error: str) -> RecallVerdict:
    return RecallVerdict(found=False, message=f"Memory search failed: {error}", error=error)


def _from_result(result: SearchMemory.Result) -> RecallVerdict:
    return RecallVerdict(
        found=result.found,
        message=result.message,
        explanation=result.explanation,
        error=result.error,
        x=result.x,
        y=result.y,
        theta=result.theta,
        seen_stamp=result.seen_stamp,
        image=base64.b64decode(result.image_b64) if result.image_b64 else None,
        latency_sec=result.latency_sec,
        cached=result.cached,
    )


def _from_recognize_result(result: RecognizePlace.Result) -> PlaceVerdict:
    return PlaceVerdict(
        matches=tuple(PlaceMatch(m.frame, m.view, m.confidence, m.x, m.y, m.theta, m.evidence) for m in result.matches),
        turn_deg=result.turn_deg,
        forward_m=result.forward_m,
        move_reason=result.move_reason,
        error=result.error,
        latency_sec=result.latency_sec,
    )
