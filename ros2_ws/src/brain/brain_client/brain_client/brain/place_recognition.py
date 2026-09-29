# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Spatial memory in reverse: which remembered views show where the robot is now.

Memory search asks "which frame shows the kitchen?"; a lost robot asks "which
frames show what my camera sees?". Every remembered frame goes to the model
labeled with its id and map pose, followed by the robot's recent views; the
model names the frames showing the same place, with a calibrated confidence,
and proposes a move toward a more recognizable view. Deciding the pose is not
the model's job — the relocalize skill checks every named place against the
lidar (see ``brain_client.relocalization``).
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from innate_llm import Image, Message, Request, Role, Text, Thinking

if TYPE_CHECKING:
    from collections.abc import Sequence

    from innate_llm import Json, Provider
    from innate_llm.types import Part
    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.memory.store import Memory, MemoryStore

_TIMEOUT_SEC = 120.0  # a blocking call carrying every remembered frame
_BUSY_WAIT_SEC = 5.0

_SYSTEM = (
    "You are the place-recognition module of a small indoor robot (camera 26 cm above the floor) "
    "that has lost track of where it is on its map. You are given REFERENCE snapshots it recorded "
    "earlier while driving around, each labeled with an id and the map pose it was taken from (x, y "
    "in metres; heading in degrees, 0 = +x, 90 = +y), then the robot's CURRENT camera view(s). Find "
    "the reference frames showing the same place as a current view: overlapping scenery seen from a "
    "similar vantage point (the robot may be nearer, further or a bit to the side, and turned up to "
    "~60 degrees). Match on specific, distinctive evidence — a particular piece of furniture, a "
    "fixture, an appliance, an artwork, a window, a room layout — never on generic surfaces: plain "
    "walls, doors, floors, ceilings and lights repeat all over a building. Calibrate confidence "
    "honestly: 0.9+ only when several distinctive elements agree; 0.5 plausible but could be "
    "elsewhere; below 0.3 a guess. When similar-looking places exist, list each of them. If nothing "
    "distinctive is visible, return no candidates. Always propose the robot's next move to get a "
    "more recognizable view (turn toward open space, furniture or a room rather than a wall or door)."
)

_RESPONSE_SCHEMA: Json = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "view": {"type": "integer", "description": "which current view matched (1-based)"},
                    "frame": {"type": "integer", "description": "reference frame id"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string", "description": "the distinctive elements both images share"},
                },
                "required": ["view", "frame", "confidence", "evidence"],
            },
        },
        "move": {
            "type": "object",
            "properties": {
                "turn_deg": {"type": "number", "description": "rotate in place first, + = left (CCW)"},
                "forward_m": {"type": "number", "description": "then drive this far straight ahead (0-1.5)"},
                "why": {"type": "string"},
            },
            "required": ["turn_deg", "forward_m", "why"],
        },
    },
    "required": ["candidates", "move"],
}


@dataclass(frozen=True)
class View:
    """One camera view of the lost robot, labeled with where it was taken relative to now."""

    jpeg: bytes
    label: str


@dataclass(frozen=True)
class PlaceMatch:
    view: int  # index into the views asked about
    memory: Memory
    confidence: float
    evidence: str


@dataclass(frozen=True)
class PlaceVerdict:
    """``error`` non-empty means recognition itself failed; no matches with no
    error is an honest "nothing distinctive in view"."""

    matches: tuple[PlaceMatch, ...] = ()
    turn_deg: float = 0.0
    forward_m: float = 0.0
    move_reason: str = ""
    error: str = ""
    latency_sec: float = 0.0


class PlaceRecognition:
    def __init__(self, store: MemoryStore, provider: Provider, *, logger: RcutilsLogger):
        self._store = store
        self._provider = provider
        self._logger = logger
        self._flight = threading.Lock()

    def use_provider(self, provider: Provider) -> None:
        self._provider = provider

    def recognize(self, views: Sequence[View], context: str = "") -> PlaceVerdict:
        """Blocking; never raises — failures come back as an ``error`` verdict."""
        if not views:
            return PlaceVerdict(error="no views to recognize")
        if not self._flight.acquire(timeout=_BUSY_WAIT_SEC):
            return PlaceVerdict(error="another place recognition is still running")
        started = time.monotonic()
        try:
            return self._recognize_locked(views, context, started)
        except Exception as error:  # noqa: BLE001 — transport failures become a typed error verdict
            self._logger.error(f"[Memory] place recognition failed: {error!r}")
            return PlaceVerdict(error=str(error), latency_sec=round(time.monotonic() - started, 2))
        finally:
            self._flight.release()

    def _recognize_locked(self, views: Sequence[View], context: str, started: float) -> PlaceVerdict:
        snapshot = self._store.snapshot()
        frames = [(memory, jpeg) for memory in snapshot.memories if (jpeg := self._read_image(memory))]
        if not frames:
            return PlaceVerdict(error="the robot has no memories of this map to recognize places from")
        request = Request(
            system=_SYSTEM,
            messages=(Message(Role.USER, (*_references(frames), *_question(views, context))),),
            thinking=Thinking.LOW,
            json_schema=_RESPONSE_SCHEMA,
            temperature=0.0,
        )
        reply = self._provider.run(request, timeout=_TIMEOUT_SEC)
        latency = round(time.monotonic() - started, 2)
        try:
            data = json.loads(reply.message.text())
            by_id = {memory.id: memory for memory, _ in frames}
            matches = tuple(
                PlaceMatch(int(c["view"]) - 1, by_id[int(c["frame"])], float(c["confidence"]), str(c["evidence"]))
                for c in data["candidates"]
                if int(c["frame"]) in by_id and 1 <= int(c["view"]) <= len(views)
            )
            move = data["move"]
            return PlaceVerdict(
                matches, float(move["turn_deg"]), float(move["forward_m"]), str(move["why"]), latency_sec=latency
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return PlaceVerdict(error="unreadable answer", latency_sec=latency)

    def _read_image(self, memory: Memory) -> bytes | None:
        path = self._store.image_path(memory.id)
        try:
            return path.read_bytes() if path is not None else None
        except OSError:
            return None


def _references(frames: list[tuple[Memory, bytes]]) -> tuple[Part, ...]:
    parts: list[Part] = [Text(f"REFERENCE snapshots ({len(frames)}):")]
    for memory, jpeg in frames:
        parts += [
            Image(jpeg),
            Text(
                f"Reference frame {memory.id} — from x={memory.x:.2f} y={memory.y:.2f} heading={math.degrees(memory.theta):.0f}°"
            ),
        ]
    return tuple(parts)


def _question(views: Sequence[View], context: str) -> tuple[Part, ...]:
    parts: list[Part] = [Text(f"CURRENT views ({len(views)}), all taken by the robot in the last minute:")]
    for i, view in enumerate(views, 1):
        parts += [Image(view.jpeg), Text(f"Current view {i} — {view.label}.")]
    if context:
        parts.append(Text(context))
    parts.append(Text("Which reference frames show the same place as a current view? Then propose the next move."))
    return tuple(parts)
