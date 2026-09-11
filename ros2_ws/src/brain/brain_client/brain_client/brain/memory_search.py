# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Recall over the spatial memory: which remembered view answers a request.

Mobility-VLA-style retrieval: every remembered frame rides one blocking Chat
Completions call, labeled with its id, capture time, and map pose, and the
model picks the one that best serves the query — directly ("the kitchen") or
by reasoning ("I am hungry").

Every search concludes in a :class:`SearchVerdict` — one structured outcome
for every consumer: the ``/brain/search_memory`` action server that skills
call, and the webapp mirror. :func:`verdict_text` renders the one
model-facing sentence for all of them.
"""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.brain.transport import ChatTransport
    from brain_client.memory.store import Memory, MemorySnapshot, MemoryStore

# How long a new search waits for the lock. An abandoned goal (the skill's
# timeout fired; cancels are rejected) can hold it for the transport's full
# timeout — parking every executor thread behind it would wedge the server.
_BUSY_WAIT_SEC = 5.0

_SYSTEM = (
    "You are the spatial memory of a small home robot. You hold snapshots the robot remembered "
    "while driving around its current map; each frame is labeled with its id number, when it was "
    "recorded, and the map pose it was taken from. Given what the robot needs, pick the single "
    "frame whose view best serves it — the place itself, or where the needed thing was last "
    'seen. Needs may be indirect: "I am hungry" points at food or the kitchen, "exit the room" '
    "at a doorway. Prefer the most recent frame among equals. If no remembered view plausibly "
    "helps, be honest and report no match."
)

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "memory_verdict",
        "schema": {
            "type": "object",
            "properties": {
                "found": {"type": "boolean"},
                "frame": {"type": "integer", "description": "id number of the best frame, 0 when found is false"},
                "explanation": {
                    "type": "string",
                    "description": "one sentence: what the frame shows and why it serves the need",
                },
            },
            "required": ["found", "frame", "explanation"],
        },
    },
}


@dataclass(frozen=True)
class SearchVerdict:
    """One search's structured outcome. ``error`` non-empty means the search
    itself failed (transport, unreadable answer) — distinct from a clean
    no-match, which is ``found=False`` with an explanation."""

    query: str
    found: bool
    explanation: str = ""
    error: str = ""
    memory: Memory | None = None
    image: bytes | None = None
    latency_sec: float = 0.0


class MemorySearch:
    def __init__(
        self, store: MemoryStore, transport: ChatTransport, *, model: str, thinking: str, logger: RcutilsLogger
    ):
        self._store = store
        self._chat = transport
        self._model = model
        self._thinking = thinking
        self._logger = logger
        self._flight = threading.Lock()  # searches run one at a time
        # UI mirror, set by the composition root: every finished search's verdict
        # as a JSON-able dict (query, found, pose, explanation, latency).
        self.on_result: Callable[[dict], None] | None = None

    def search(self, query: str) -> SearchVerdict:
        """Blocking: ask the model which remembered frame serves the query.

        Never raises — failures come back as an ``error`` verdict every
        consumer (action result, webapp card) can render.
        """
        if not self._flight.acquire(timeout=_BUSY_WAIT_SEC):
            verdict = SearchVerdict(query=query, found=False, error="another memory search is still running")
        else:
            started = time.monotonic()
            try:
                verdict = self._search_locked(query, started)
            except Exception as error:  # noqa: BLE001 — transport failures become a typed error verdict
                self._logger.error(f"[Memory] search failed: {error!r}")
                verdict = SearchVerdict(query=query, found=False, error=str(error))
            finally:
                self._flight.release()
        self._report(verdict)
        return verdict

    def _search_locked(self, query: str, started: float) -> SearchVerdict:
        snapshot = self._store.snapshot()
        if not snapshot.memories:
            return SearchVerdict(
                query=query,
                found=False,
                explanation="The robot has no memories of this map yet — drive around in navigation mode to build them.",
            )
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": [*self._frame_content(snapshot.memories), _text(_question(query))]},
            ],
            "response_format": _RESPONSE_FORMAT,
            "temperature": 0,
        }
        if self._thinking:
            # Measured for this task on Gemini: a search is a pick among labeled
            # frames, and full thinking only slowed it. A blank llm_thinking means
            # the server takes no reasoning knob, so this one goes with it.
            body["reasoning_effort"] = "low"
        return self._conclude(query, self._chat.complete(body, None), snapshot, started)

    def _conclude(self, query: str, response: dict, snapshot: MemorySnapshot, started: float) -> SearchVerdict:
        latency = round(time.monotonic() - started, 2)
        parsed = _parse_verdict(response)
        if parsed is None:
            return SearchVerdict(query=query, found=False, error="unreadable answer", latency_sec=latency)
        found, frame_id, explanation = parsed
        # The coordinates only mean anything on the map the frames came from —
        # a map switched (or remapped under its own name) mid-flight voids the
        # verdict rather than steering the robot toward another map's frame.
        live = self._store.snapshot()
        if live.map_name != snapshot.map_name or live.fingerprint != snapshot.fingerprint:
            return SearchVerdict(
                query=query, found=False, error="the active map changed during the search", latency_sec=latency
            )
        # Frames are labeled by stable store id, so the answer resolves against
        # the LIVE snapshot — an id evicted mid-search cleanly misses, a
        # refreshed one serves its newest pose.
        memory = next((m for m in live.memories if m.id == frame_id), None)
        if not found or memory is None:
            return SearchVerdict(query=query, found=False, explanation=explanation, latency_sec=latency)
        return SearchVerdict(
            query=query,
            found=True,
            explanation=explanation,
            memory=memory,
            image=self._read_image(memory),
            latency_sec=latency,
        )

    def _report(self, verdict: SearchVerdict) -> None:
        """Mirror a verdict to the UI topic; best-effort — it must never break a search."""
        if self.on_result is None:
            return
        payload: dict = {"query": verdict.query, "found": verdict.found, "stamp": time.time()}
        if verdict.error:
            payload["error"] = verdict.error
        else:
            payload |= {"explanation": verdict.explanation, "latency_sec": verdict.latency_sec}
        if verdict.memory is not None:
            memory = verdict.memory
            payload |= {
                "id": memory.id,
                "x": round(memory.x, 3),
                "y": round(memory.y, 3),
                "theta": round(memory.theta, 4),
                "seen_stamp": memory.stamp,
            }
        try:
            self.on_result(payload)
        except Exception as error:  # noqa: BLE001 — the UI mirror must not break the search
            self._logger.warn(f"[Memory] search result mirror failed: {error!r}")

    def _frame_content(self, memories: tuple[Memory, ...]) -> list[dict]:
        """Every frame as its label followed by its pixels (a frame evicted
        between the snapshot and the read just drops out)."""
        content: list[dict] = []
        for memory in memories:
            jpeg = self._read_image(memory)
            if not jpeg:
                continue
            content.append(_text(_frame_label(memory)))
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()},
                }
            )
        return content

    def _read_image(self, memory: Memory) -> bytes | None:
        path = self._store.image_path(memory.id)
        try:
            return path.read_bytes() if path is not None else None
        except OSError:
            return None


def verdict_text(verdict: SearchVerdict) -> str:
    """The one model-facing sentence for a verdict — shared by the agent's
    event path and the search skill, so the model reads the same thing
    whichever door the search came through."""
    if verdict.error:
        return f'Memory search for "{verdict.query}" failed: {verdict.error}'
    if verdict.memory is None:
        return f'Memory search for "{verdict.query}": nothing in the remembered views matches. {verdict.explanation}'
    memory = verdict.memory
    return (
        f'Memory search for "{verdict.query}": found a match, recorded {_age_text(time.time() - memory.stamp)} ago '
        f"at map position x={memory.x:.2f}m y={memory.y:.2f}m heading={math.degrees(memory.theta):.0f}°. "
        f"{verdict.explanation} "
        + ("The attached image is that memory. " if verdict.image else "")
        + f"To go there: navigate_to_position(x={memory.x:.2f}, y={memory.y:.2f}, "
        f"theta_degrees={math.degrees(memory.theta):.0f}, local_frame=false)."
    )


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _frame_label(memory: Memory) -> str:
    # Labeled by the stable store id (not enumeration position), so verdicts
    # stay valid across additions, refreshes, and evictions.
    when = datetime.fromtimestamp(memory.stamp).strftime("%Y-%m-%d %H:%M")
    return (
        f"Frame {memory.id} — recorded {when}, from map position x={memory.x:.2f}m y={memory.y:.2f}m "
        f"heading={math.degrees(memory.theta):.0f}°" + (f" — {memory.label}" if memory.label else "")
    )


def _question(query: str) -> str:
    return f'The robot needs: "{query}". Which frame best serves this?'


def _parse_verdict(response: dict) -> tuple[bool, int, str] | None:
    try:
        data = json.loads(response["choices"][0]["message"]["content"])
        return bool(data["found"]), int(data["frame"]), str(data.get("explanation", ""))
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{max(round(seconds), 0)}s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)}min"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)}h"
    return f"{round(seconds / 86400)}d"
