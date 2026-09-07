# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Recall over the spatial memory: which remembered view answers a request.

Mobility-VLA-style retrieval: every remembered frame goes to Gemini, labeled
with its id, capture time, and map pose, and the model picks the one that
best serves the query — directly ("the kitchen") or by reasoning ("I am
hungry").

Bandwidth is tiered, and every tier is a pure optimization that degrades
without changing answers. Each frame's bytes cross the network once, at
record time — a background chore (:mod:`frame_files`) uploads them to the
Gemini Files API and requests reference them as ``fileData``. On top rides an
explicit context cache: fresh → the search sends only the question; stale →
the search *still* uses it and appends the delta (added/refreshed frames plus
short supersede/retire notes) — a search is always exactly one round trip and
never waits for maintenance (cache builds happen only in :meth:`warm`'s
background thread). A frame not yet uploaded rides ``inlineData``; a backend
without an endpoint latches that tier off, bottoming out at today's
all-inline behavior.

Every search concludes in a :class:`SearchVerdict` — one structured outcome
for every consumer: the ``/brain/search_memory`` action server that skills
call, and the webapp mirror. :func:`verdict_text` renders the one
model-facing sentence for all of them.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from brain_client.brain.frame_files import FrameFiles
from brain_client.brain.transport import (
    CACHED_CONTENTS_PATH,
    GENERATE_PATH,
    UNSUPPORTED_ENDPOINT_STATUSES,
    GeminiHttpError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclpy.impl.rcutils_logger import RcutilsLogger

    from brain_client.brain.transport import GeminiRest
    from brain_client.memory.store import Memory, MemorySnapshot, MemoryStore

_TTL_SEC = 12 * 3600  # ~20k-token cache: 12h of storage costs ~$0.24, cheap insurance against cold starts
_TTL_SAFETY_SEC = 120  # treat the cache as gone this long before Gemini actually expires it
_MIN_FRAMES_TO_CACHE = 6  # fewer frames sit under the API's minimum cache size (and upload fast anyway)
_WARM_AFTER_QUIET_SEC = 30.0  # let a recording burst settle before rebuilding the cache
_MAX_STALE_FRAMES = 10  # a delta this big rides every search — rebuild even mid-burst
_WARM_RETRY_SEC = 30.0  # after a transient cache-build failure — warm() rides a 1 Hz tick
# How long a new search waits for the lock. An abandoned goal (the skill's
# timeout fired; cancels are rejected) can hold it for the transport's full
# timeout — parking every executor thread behind it would wedge the server.
_BUSY_WAIT_SEC = 5.0

_SYSTEM = (
    "You are the spatial memory of a small home robot. You hold snapshots the robot remembered "
    "while driving around its current map; each frame is labeled with its id number, when it was "
    "recorded, and the map pose it was taken from. Later messages may add frames, supersede one "
    "with a newer view, or retire one — always honor the latest state. Given what the robot "
    "needs, pick the single frame whose view best serves it — the place itself, or where the "
    'needed thing was last seen. Needs may be indirect: "I am hungry" points at food or the '
    'kitchen, "exit the room" at a doorway. Prefer the most recent frame among equals. If no '
    "remembered view plausibly helps, be honest and report no match."
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "found": {"type": "BOOLEAN"},
        "frame": {"type": "INTEGER", "description": "id number of the best frame, 0 when found is false"},
        "explanation": {
            "type": "STRING",
            "description": "one sentence: what the frame shows and why it serves the need",
        },
    },
    "required": ["found", "frame", "explanation"],
}

_GENERATION_CONFIG = {
    "responseMimeType": "application/json",
    "responseSchema": _RESPONSE_SCHEMA,
    "thinkingConfig": {"thinkingLevel": "low"},
}


@dataclass(frozen=True)
class _CacheHandle:
    name: str  # "cachedContents/…"
    revision: int
    map_name: str | None
    fingerprint: str
    memories: tuple[Memory, ...]  # frame numbers resolve against what was cached, not the live store
    expires_monotonic: float


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
    cached: bool = False


class MemorySearch:
    def __init__(self, store: MemoryStore, rest: GeminiRest, *, model: str, logger: RcutilsLogger):
        self._store = store
        self._rest = rest
        self._model = model
        self._logger = logger
        self._files = FrameFiles(store, rest, logger)
        self._cache: _CacheHandle | None = None
        self._cache_unsupported = False
        self._failed_revision: int | None = None  # the backend refused exactly this content (4xx); don't hammer
        self._retry_at = 0.0  # monotonic; transient build failures back off until then
        self._flight = threading.Lock()  # searches run one at a time
        # Maintenance never holds _flight: a search must proceed mid-rebuild
        # (it rides the old handle — the swap is a single reference write).
        self._warming = threading.Lock()  # at most one cache rebuild
        # UI mirror, set by the composition root: every finished search's verdict
        # as a JSON-able dict (query, found, pose, explanation, latency, cached).
        self.on_result: Callable[[dict], None] | None = None

    def cache_state(self) -> str:
        """How the next search will run: warm | cold | inline | unsupported.

        A stale-but-usable cache reports warm — the search rides it with a
        delta, so recall is still instant (rebuilds are warm()'s business).
        """
        if self._cache_unsupported:
            return "unsupported"
        snapshot = self._store.snapshot()
        if len(snapshot.memories) < _MIN_FRAMES_TO_CACHE:
            return "inline"  # under the cache floor — always answered from frames, still quick
        return "warm" if self._usable_cache(snapshot) is not None else "cold"

    def search(self, query: str) -> SearchVerdict:
        """Blocking: ask Gemini which remembered frame serves the query.

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
        """One round trip from the best context available NOW — a search never
        builds a cache and never waits for an upload (that's warm()'s job)."""
        snapshot = self._store.snapshot()
        if not snapshot.memories:
            return SearchVerdict(
                query=query,
                found=False,
                explanation="The robot has no memories of this map yet — drive around in navigation mode to build them.",
            )
        cache = self._usable_cache(snapshot)
        if cache is not None:
            # A stale cache still serves: the delta (new/refreshed frames plus
            # supersede/retire notes) rides alongside the question.
            body = {
                "cachedContent": cache.name,
                "contents": [_user([*self._delta_parts(cache, snapshot), {"text": _question(query)}])],
                "generationConfig": _GENERATION_CONFIG,
            }
            try:
                response = self._rest.post(GENERATE_PATH.format(model=self._model), body)
                return self._conclude(query, response, snapshot, started, cached=True)
            except GeminiHttpError as error:
                if error.status >= 500:
                    raise
                # The cache died server-side (expired early, deleted — including
                # by warm() swapping in a successor and deleting this handle
                # mid-flight): forget the handle that failed, never a newer one.
                if self._cache is cache:
                    self._cache = None
                self._logger.warn(f"[Memory] cached search failed ({error}); retrying without it")
        parts, _included = self._frame_parts(snapshot.memories)
        body = {
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [_user([*parts, {"text": _question(query)}])],
            "generationConfig": _GENERATION_CONFIG,
        }
        response = self._rest.post(GENERATE_PATH.format(model=self._model), body)
        return self._conclude(query, response, snapshot, started, cached=False)

    def warm(self) -> None:
        """All background maintenance, driven by the recorder's 1 Hz tick:
        upload new/aging frames server-side, and rebuild the context cache once
        the memory has settled. Returns immediately; searches never wait on it.
        """
        self._files.maintain()
        snapshot = self._store.snapshot()
        if not self._should_warm(snapshot):
            return
        if (
            time.monotonic() - self._store.last_change_monotonic < _WARM_AFTER_QUIET_SEC
            and self._staleness(snapshot) < _MAX_STALE_FRAMES
        ):
            return  # let the burst settle — unless the delta already rides every search too heavy
        if not self._warming.acquire(blocking=False):
            return  # a rebuild is already on the wire

        def run() -> None:
            try:
                fresh = self._store.snapshot()
                if self._should_warm(fresh):
                    self._create_cache(fresh)
            finally:
                self._warming.release()

        threading.Thread(target=run, name="memory-warm", daemon=True).start()

    def forget(self) -> None:
        """The user forgot this map's memories: drop the cache handle and the
        uploaded frames so the forgotten views stop being served — server-side
        deletion is best effort, in the background."""
        cache, self._cache = self._cache, None
        self._failed_revision = None
        self._files.purge()
        if cache is None:
            return

        def run() -> None:
            with contextlib.suppress(Exception):
                self._rest.delete(f"/v1beta/{cache.name}")

        threading.Thread(target=run, name="memory-cache-forget", daemon=True).start()

    def forget_frame(self, memory: Memory) -> None:
        """The user forgot one memory: delete its server-side upload and drop
        the context cache embedding the frame — a retire note only shadows it,
        and the handle would otherwise serve the view until its 12 h TTL. The
        other uploads stay, so the next warm() rebuild references them cheaply."""
        self._files.forget(memory.id)
        cache = self._cache
        if cache is None or all(m.id != memory.id for m in cache.memories):
            return
        if self._cache is cache:
            self._cache = None

        def run() -> None:
            with contextlib.suppress(Exception):
                self._rest.delete(f"/v1beta/{cache.name}")

        threading.Thread(target=run, name="memory-cache-forget", daemon=True).start()

    # --- cache management (under _flight, except the read-only probes) ---
    def _cache_matches(self, snapshot: MemorySnapshot) -> bool:
        cache = self._cache
        return (
            cache is not None
            and cache.revision == snapshot.revision
            and cache.map_name == snapshot.map_name
            and time.monotonic() < cache.expires_monotonic
        )

    def _usable_cache(self, snapshot: MemorySnapshot) -> _CacheHandle | None:
        """The handle a search may ride right now — fresh OR stale (the delta
        covers staleness); only the wrong map or expiry disqualifies it. The
        delta's supersede/retire notes trust stable ids, which a same-name
        remap resets — so the map is matched by fingerprint, not just name."""
        cache = self._cache
        if cache is None or cache.map_name != snapshot.map_name or cache.fingerprint != snapshot.fingerprint:
            return None
        return cache if time.monotonic() < cache.expires_monotonic else None

    def _staleness(self, snapshot: MemorySnapshot) -> int:
        """How many frames the next search's delta must carry (0 = fresh, or no
        cache to be stale against)."""
        cache = self._usable_cache(snapshot)
        if cache is None:
            return 0
        cached = {memory.id: memory.stamp for memory in cache.memories}
        return sum(1 for memory in snapshot.memories if cached.get(memory.id) != memory.stamp)

    def _should_warm(self, snapshot: MemorySnapshot) -> bool:
        """The one gate for cache rebuilds — warm() checks it before spawning
        the thread and again on the fresh snapshot inside it."""
        if self._cache_unsupported or len(snapshot.memories) < _MIN_FRAMES_TO_CACHE:
            return False
        if self._failed_revision == snapshot.revision or time.monotonic() < self._retry_at:
            return False
        return not self._cache_matches(snapshot)

    def _create_cache(self, snapshot: MemorySnapshot) -> _CacheHandle | None:
        started = time.monotonic()
        # A cache built from fileData must not outlive the uploads it embeds
        # (Gemini deletes them at 48 h): cap its life at the earliest
        # referenced file's usable deadline, so warm() rebuilds in time — and
        # when that deadline is already at hand, don't build at all (the handle
        # would arrive expired and every warm would pay for another).
        expires = started + _TTL_SEC - _TTL_SAFETY_SEC
        for memory in snapshot.memories:
            until = self._files.usable_until(memory)
            if until is not None:
                expires = min(expires, started + (until - time.time()) - _TTL_SAFETY_SEC)
        if expires <= started:
            return None
        parts, included = self._frame_parts(snapshot.memories)
        if len(included) < _MIN_FRAMES_TO_CACHE:
            return None
        body = {
            "model": f"models/{self._model}",
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [_user(parts)],
            "ttl": f"{_TTL_SEC}s",
            "displayName": f"mars-spatial-memory-{snapshot.map_name or 'unknown'}",
        }
        try:
            name = str(self._rest.post(CACHED_CONTENTS_PATH, body).get("name") or "")
        except GeminiHttpError as error:
            if error.status in UNSUPPORTED_ENDPOINT_STATUSES:
                self._cache_unsupported = True
                self._logger.warn(
                    f"[Memory] backend has no context-cache endpoint (HTTP {error.status}); searches run uncached"
                )
            elif error.status < 500:
                # The backend refused this content — identical retries can't
                # succeed; wait for the memory to change.
                self._failed_revision = snapshot.revision
                self._logger.warn(f"[Memory] context cache creation failed: {error}")
            else:
                self._retry_at = time.monotonic() + _WARM_RETRY_SEC
                self._logger.warn(
                    f"[Memory] context cache creation failed ({error}); retrying in {_WARM_RETRY_SEC:.0f}s"
                )
            return None
        except Exception as error:  # noqa: BLE001 — network trouble backs off; warm() rides a 1 Hz tick
            self._retry_at = time.monotonic() + _WARM_RETRY_SEC
            self._logger.warn(f"[Memory] context cache creation failed ({error!r}); retrying in {_WARM_RETRY_SEC:.0f}s")
            return None
        if not name:
            self._failed_revision = snapshot.revision
            return None
        old, self._cache = (
            self._cache,
            _CacheHandle(
                name=name,
                revision=snapshot.revision,
                map_name=snapshot.map_name,
                fingerprint=snapshot.fingerprint,
                memories=included,
                expires_monotonic=expires,
            ),
        )
        if old is not None:
            with contextlib.suppress(Exception):
                self._rest.delete(f"/v1beta/{old.name}")
        self._logger.info(f"[Memory] cached {len(included)} frames for search in {time.monotonic() - started:.1f}s")
        return self._cache

    # --- request assembly / response handling ---
    def _conclude(
        self, query: str, response: dict, snapshot: MemorySnapshot, started: float, *, cached: bool
    ) -> SearchVerdict:
        latency = round(time.monotonic() - started, 2)
        parsed = _parse_verdict(response)
        if parsed is None:
            return SearchVerdict(
                query=query, found=False, error="unreadable answer", latency_sec=latency, cached=cached
            )
        found, frame_id, explanation = parsed
        # The coordinates only mean anything on the map the frames came from —
        # a map switched (or remapped under its own name) mid-flight voids the
        # verdict rather than steering the robot toward another map's frame.
        live = self._store.snapshot()
        if live.map_name != snapshot.map_name or live.fingerprint != snapshot.fingerprint:
            return SearchVerdict(
                query=query,
                found=False,
                error="the active map changed during the search",
                latency_sec=latency,
                cached=cached,
            )
        # Frames are labeled by stable store id, so the answer resolves against
        # the LIVE snapshot — an id evicted mid-search cleanly misses, a
        # refreshed one serves its newest pose.
        memory = next((m for m in live.memories if m.id == frame_id), None)
        if not found or memory is None:
            return SearchVerdict(query=query, found=False, explanation=explanation, latency_sec=latency, cached=cached)
        return SearchVerdict(
            query=query,
            found=True,
            explanation=explanation,
            memory=memory,
            image=self._read_image(memory),
            latency_sec=latency,
            cached=cached,
        )

    def _report(self, verdict: SearchVerdict) -> None:
        """Mirror a verdict to the UI topic; best-effort — it must never break a search."""
        if self.on_result is None:
            return
        payload: dict = {"query": verdict.query, "found": verdict.found, "stamp": time.time()}
        if verdict.error:
            payload["error"] = verdict.error
        else:
            payload |= {
                "explanation": verdict.explanation,
                "latency_sec": verdict.latency_sec,
                "cached": verdict.cached,
            }
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

    def _frame_parts(self, memories: tuple[Memory, ...]) -> tuple[list[dict], tuple[Memory, ...]]:
        """Request parts for these frames, and which memories made it in
        (an evicted-mid-read frame just drops out)."""
        parts: list[dict] = []
        included: list[Memory] = []
        for memory in memories:
            frame = self._frame_part(memory)
            if frame is None:
                continue
            parts.extend(frame)
            included.append(memory)
        return parts, tuple(included)

    def _frame_part(self, memory: Memory) -> list[dict] | None:
        """One frame as [image part, label part] — a server-side fileData
        reference when the upload tier has one, inline bytes otherwise."""
        uri = self._files.uri_for(memory)
        if uri is not None:
            image: dict = {"fileData": {"mimeType": "image/jpeg", "fileUri": uri}}
        else:
            jpeg = self._read_image(memory)
            if not jpeg:
                return None
            image = {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}
        return [image, {"text": _frame_label(memory)}]

    def _delta_parts(self, cache: _CacheHandle, snapshot: MemorySnapshot) -> list[dict]:
        """What changed since the cache was built: new frames, re-captured
        frames with a supersede note, and retire notes for evicted ids.
        Empty exactly when the cache is fresh."""
        cached = {memory.id: memory for memory in cache.memories}
        parts: list[dict] = []
        for memory in snapshot.memories:
            old = cached.get(memory.id)
            if old is not None and old.stamp == memory.stamp:
                continue
            frame = self._frame_part(memory)
            if frame is None:
                continue
            if old is not None:
                parts.append({"text": f"Frame {memory.id} was re-captured — this newer view supersedes it:"})
            parts.extend(frame)
        current_ids = {memory.id for memory in snapshot.memories}
        for gone in sorted(cached.keys() - current_ids):
            parts.append({"text": f"Frame {gone} no longer exists — ignore it."})
        return parts

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


def _user(parts: list[dict]) -> dict:
    return {"role": "user", "parts": parts}


def _frame_label(memory: Memory) -> str:
    # Labeled by the stable store id (not enumeration position), so delta
    # notes and verdicts stay valid across additions, refreshes, and evictions.
    when = datetime.fromtimestamp(memory.stamp).strftime("%Y-%m-%d %H:%M")
    return (
        f"Frame {memory.id} — recorded {when}, from map position x={memory.x:.2f}m y={memory.y:.2f}m "
        f"heading={math.degrees(memory.theta):.0f}°" + (f" — {memory.label}" if memory.label else "")
    )


def _question(query: str) -> str:
    return f'The robot needs: "{query}". Which frame best serves this?'


def _parse_verdict(response: dict) -> tuple[bool, int, str] | None:
    try:
        parts = response["candidates"][0]["content"]["parts"]
        text = next(part["text"] for part in parts if part.get("text") and not part.get("thought"))
        data = json.loads(text)
        return bool(data["found"]), int(data["frame"]), str(data.get("explanation", ""))
    except (KeyError, IndexError, TypeError, ValueError, StopIteration):
        return None


def _age_text(seconds: float) -> str:
    if seconds < 90:
        return f"{max(round(seconds), 0)}s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)}min"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)}h"
    return f"{round(seconds / 86400)}d"
