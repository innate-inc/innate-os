# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Persistent per-map spatial memory: a JSON index plus one JPEG per memory.

Lives under ``data/spatial_memory/<map>/`` beside the maps it describes and
survives restarts. Memories only mean anything in the coordinate frame of the
map they were recorded on, so each map gets its own directory, and the index
carries a fingerprint of the map file — re-mapping under the same name yields
a new frame, and the stale memories are wiped rather than trusted.

Memories recorded *during* mapping have no saved map yet: they stage under
``spatial_memory/.mapping/`` (a name save_map's validation can never produce)
and are copied into the saved map's directory when the save lands. The stage
outlives the promotion — a re-saved map re-promotes the whole tour — and its
index remembers which SLAM session built it (mode_manager's start stamp): the
same session re-adopts it across a brain restart, any other session wipes it,
because its coordinates only meant anything in the frame that made them.

Thread contract: mutations come from the recorder's timer (executor thread);
:meth:`snapshot` serves readers on the agent loop and search threads. Every
index access takes the store lock; image files are read without it, so a
concurrently evicted file reads as missing — callers tolerate that.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock

_INDEX_VERSION = 1
MAPPING_SESSION = ".mapping"
# Promotion scratch dirs — dot-prefixed like the stage, so no saved map name
# can collide; leftovers from an interrupted promotion are landed or swept at
# startup and again on the next session's entry.
_PROMOTE_TMP = f"{MAPPING_SESSION}.promote"
_DISPLACED_TMP = f"{MAPPING_SESSION}.displaced"


class StaleStageError(Exception):
    """The stage on disk was built by a different mapping session than the one
    the save came from; promoting it would plant a dead frame's memories in
    the new map."""


@dataclass(frozen=True)
class Memory:
    """One remembered viewpoint: where the robot stood and when it looked."""

    id: int
    x: float
    y: float
    theta: float
    stamp: float  # epoch seconds at capture
    label: str = ""  # optional authored description; recorded frames need none


@dataclass(frozen=True)
class MemorySnapshot:
    """An immutable view of the store for readers on other threads."""

    map_name: str | None
    revision: int
    memories: tuple[Memory, ...]
    # Same-name remaps reset memory ids, so name equality alone cannot prove
    # two snapshots share a coordinate frame — the fingerprint can.
    fingerprint: str = ""

    def positions(self) -> list[dict]:
        """The webapp mirror payload: one ``{id, x, y, theta, stamp}`` per
        memory, rounded to display precision — full floats bloat the JSON by
        half."""
        return [
            {
                "id": m.id,
                "x": round(m.x, 3),
                "y": round(m.y, 3),
                "theta": round(m.theta, 4),
                "stamp": round(m.stamp, 1),
            }
            for m in self.memories
        ]


class MemoryStore:
    def __init__(self, data_dir: Path, *, seed_dir: Path | None = None):
        self._maps_dir = data_dir / "maps"
        self._root = data_dir / "spatial_memory"
        self._seed_dir = seed_dir
        self._lock = Lock()
        self._map_name: str | None = None
        self._dir: Path | None = None
        self._memories: list[Memory] = []
        self._next_id = 1
        self._fingerprint = ""
        self._session: float | None = None
        self._stat_sig: tuple[int, int] | None = None
        self._revision = 0
        self.last_change_monotonic = 0.0
        self._finish_interrupted_promotion()

    def switch_map(self, map_name: str | None) -> None:
        """Point the store at a map's memories, loading them from disk.

        Called every tick: same name + unchanged file stat is a cheap no-op,
        and hashing runs only when the name or the map file changes — so
        re-mapping over the active map's own name is caught within a tick.
        """
        stat_sig = _map_stat(self._maps_dir, map_name) if map_name else None
        if map_name == self._map_name and stat_sig == self._stat_sig:
            return
        fingerprint = _map_fingerprint(self._maps_dir, map_name) if map_name else ""
        if map_name is not None and not fingerprint:
            # The map file is unreadable this tick (being rewritten, transient
            # IO error). "Can't verify" must never wipe — retry next tick.
            return
        with self._lock:
            self._stat_sig = stat_sig
            if map_name == self._map_name and fingerprint == self._fingerprint:
                return  # the file was touched, not re-made
            self._map_name = map_name
            self._dir = self._root / Path(map_name).stem if map_name else None
            self._memories = []
            self._next_id = 1
            self._fingerprint = fingerprint
            self._session = None
            self._revision += 1
            self.last_change_monotonic = time.monotonic()
            if self._dir is not None:
                self._load_locked()

    def use_mapping_session(self, session_started: float | None = None) -> None:
        """Point the store at the mapping-session stage. Idempotent per tick;
        actually entering wipes what a previous session left behind and mints a
        fresh fingerprint — every consumer tells coordinate frames apart by
        map+fingerprint, and two SLAM sessions are two frames despite sharing
        the ``.mapping`` name. ``session_started`` (mode_manager's stamp for
        the live slam_toolbox activation) names the frame: a stage this same
        session already built is adopted instead of wiped, so a brain restart
        mid-tour keeps the half-tour."""
        with self._lock:
            if self._map_name == MAPPING_SESSION and self._session == session_started:
                return
            self._map_name = MAPPING_SESSION
            self._dir = self._root / MAPPING_SESSION
            self._stat_sig = None
            self._session = session_started
            if session_started is None or not self._adopt_stage_locked(session_started):
                self._fingerprint = os.urandom(16).hex()
                self._wipe_locked()
            self._finish_interrupted_promotion()  # a stamped leftover is a map's only memories — land it
            self._drop_scratch(self._root / _PROMOTE_TMP, self._root / _DISPLACED_TMP)
            self._revision += 1
            self.last_change_monotonic = time.monotonic()

    def promote_mapping_session(self, map_name: str, mapping_started: float | None = None) -> int | None:
        """Hand the staged mapping-session memories to the just-saved map:
        copy the stage into the map's directory with its fingerprint stamped,
        replacing whatever the name held before (the room was just re-mapped,
        so its old memories lie). Works from the stage on disk, not the live
        attachment — the save announcement races the mode-driven switch_map
        through independent topics, and a lost race must not lose the tour.
        The stage itself stays behind: a re-save — the webapp retrying after
        a failed mode switch — promotes the whole tour again. Returns how
        many were promoted (0 when nothing is staged); None when the saved
        map can't be fingerprinted, with the stage kept. ``mapping_started``
        is the save's session identity: a stage another session built raises
        :class:`StaleStageError` — it must never land in a foreign map.
        """
        stage = self._root / MAPPING_SESSION
        index = _staged_index(stage)
        count = len(index.get("memories") or []) if index is not None else 0
        if index is None or count == 0:
            return 0
        if mapping_started is not None and index.get("session") != mapping_started:
            raise StaleStageError(f"stage session {index.get('session')!r} != save's {mapping_started!r}")
        fingerprint = _map_fingerprint(self._maps_dir, map_name)
        if not fingerprint:
            return None
        # The map hash and the stage copy run without the lock (switch_map
        # hashes outside it too): every stage mutation runs on this same
        # executor thread, so the stage cannot change under the copy.
        tmp = self._root / _PROMOTE_TMP
        if tmp.is_dir():
            shutil.rmtree(tmp)
        # Built aside and swapped in whole (the proxy reads without the lock),
        # via hardlinks: a byte copy of a long tour would stall the node's
        # only spin thread for seconds.
        shutil.copytree(stage, tmp, copy_function=os.link)
        stamped = json.loads((tmp / "index.json").read_text())
        stamped["map"] = map_name
        stamped["fingerprint"] = fingerprint
        # Not write_text: stage files are only ever replaced, never edited in
        # place -- that is what keeps every hardlinked file frozen, this one
        # included.
        stamped_tmp = tmp / "index.json.tmp"
        stamped_tmp.write_text(json.dumps(stamped))
        os.replace(stamped_tmp, tmp / "index.json")
        with self._lock:
            # Displace-then-swap, never delete-then-swap: destroying the
            # target before its replacement is in place would leave the map
            # memoryless if a crash lands between the two.
            target = self._root / Path(map_name).stem
            displaced = self._root / _DISPLACED_TMP
            self._clear_displaced_slot()
            if target.is_dir():
                os.replace(target, displaced)
            try:
                os.replace(tmp, target)
            except OSError:
                # A failed landing must not leave the map memoryless while the
                # process lives on: put the displaced memories back. The stage
                # still holds the tour, so a re-save retries the promotion.
                self._restore_displaced(target)
                raise
            self._drop_scratch(displaced)  # this map's old set is spent; another map's is not
            if self._map_name == map_name:
                # The switch won the race and loaded an empty store; adopt the
                # promoted memories now — no later tick would reload them.
                self._fingerprint = fingerprint
                self._load_locked()
                self._revision += 1
                self.last_change_monotonic = time.monotonic()
            return count

    def _finish_interrupted_promotion(self) -> None:
        """Land a promotion an interruption cut short. Between promote's two
        os.replace calls the map's directory is gone — a crash there (or a
        failed swap whose in-line restore also failed) leaves the map
        memoryless, with both sets in scratch dirs. The stamped copy names its
        map, so finish the swap; when it will not land, give the map back the
        set it had instead. An unstamped copy names ``.mapping``, whose dir
        exists: a no-op. Runs at startup and on session entry, before the
        entry's sweep destroys what it could have landed.
        """
        tmp = self._root / _PROMOTE_TMP
        target = _scratch_map_dir(self._root, tmp)
        if target is not None and not target.is_dir() and not _land(tmp, target):
            self._restore_displaced(target)
        self._clear_displaced_slot()

    def _restore_displaced(self, target: Path) -> None:
        """Give a map back the set displaced from it. Best effort, and only
        when the scratch dir is that map's: an earlier failed promotion can
        have stranded another map's memories there, and those are a foreign
        coordinate frame here — landing them would plant lies in this map and
        strip the map they belong to. A restore that cannot run leaves both
        sets in scratch for recovery; raising would bury the original error.
        """
        displaced = self._root / _DISPLACED_TMP
        if _scratch_map_dir(self._root, displaced) == target:
            _land(displaced, target)

    def _clear_displaced_slot(self) -> None:
        """Empty the displaced scratch slot. A set owed to a map whose
        directory is absent is that map's only memories: land it back home --
        left in the slot it would fail every later displace with ENOTEMPTY --
        and sweep whatever remains as spent. A set that will not land (the
        fault persists) stays put for the next recovery."""
        displaced = self._root / _DISPLACED_TMP
        owed = _scratch_map_dir(self._root, displaced)
        if owed is not None and not owed.is_dir():
            _land(displaced, owed)
        self._drop_scratch(displaced)

    def _drop_scratch(self, *scratch: Path) -> None:
        """Remove promotion scratch no map is waiting on. One still owed to a
        map whose directory is absent stays put: it holds that map's only
        memories, and the next recovery gets another chance to land it —
        deleting it there is the one way to lose the map's memories for good.
        """
        for path in scratch:
            owed = _scratch_map_dir(self._root, path)
            if owed is None or owed.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def snapshot(self) -> MemorySnapshot:
        with self._lock:
            return MemorySnapshot(self._map_name, self._revision, tuple(self._memories), self._fingerprint)

    def image_path(self, memory_id: int) -> Path | None:
        with self._lock:
            return self._dir / f"{memory_id}.jpg" if self._dir is not None else None

    def files_index_path(self) -> Path | None:
        """Where the current map's server-side-upload registry lives (brain/frame_files.py)."""
        with self._lock:
            return self._dir / "files.json" if self._dir is not None else None

    def add(self, x: float, y: float, theta: float, stamp: float, jpeg: bytes) -> Memory | None:
        """Record a new memory; None when no map is loaded."""
        with self._lock:
            if self._dir is None:
                return None
            memory = Memory(self._next_id, x, y, theta, stamp)
            self._next_id += 1
            self._write_image_locked(memory.id, jpeg)
            self._memories.append(memory)
            self._commit_locked()
            return memory

    def replace(self, old: Memory, x: float, y: float, theta: float, stamp: float, jpeg: bytes) -> None:
        """Overwrite a memory in place — same slot, fresh view of the same spot."""
        with self._lock:
            if self._dir is None or all(m.id != old.id for m in self._memories):
                return
            memory = Memory(old.id, x, y, theta, stamp)
            self._write_image_locked(memory.id, jpeg)
            self._memories = [memory if m.id == old.id else m for m in self._memories]
            self._commit_locked()

    def clear(self) -> int:
        """Forget every memory on the current map — images, index, and upload
        registry — returning how many were forgotten."""
        with self._lock:
            if self._dir is None or not self._memories:
                return 0
            cleared = len(self._memories)
            self._wipe_locked()
            self._commit_locked()
            return cleared

    def evict(self, memory: Memory) -> None:
        self.forget(memory.id)

    def forget(self, memory_id: int, fingerprint: str = "") -> Memory | None:
        """Evict one memory by id, returning it — None when the id is unknown
        or the caller's fingerprint is stale (a re-map restarts ids, so a stale
        client must not delete the new map's memories; empty skips the check).
        Prefix-matched: the positions payload publishes a truncated digest."""
        with self._lock:
            if self._dir is None or (fingerprint and not self._fingerprint.startswith(fingerprint)):
                return None
            memory = next((m for m in self._memories if m.id == memory_id), None)
            if memory is None:
                return None
            self._memories = [m for m in self._memories if m.id != memory_id]
            (self._dir / f"{memory_id}.jpg").unlink(missing_ok=True)
            self._commit_locked()
            return memory

    # --- locked internals ---
    def _load_locked(self) -> None:
        assert self._dir is not None
        try:
            index = json.loads((self._dir / "index.json").read_text())
            fresh = (
                isinstance(index, dict)
                and index.get("version") == _INDEX_VERSION
                and index.get("fingerprint") == self._fingerprint
            )
            if fresh:
                self._restore_locked(index)
                return
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass  # a wrong-shaped index is as stale as a wrong fingerprint
        self._wipe_locked()
        self._seed_locked()

    def _seed_locked(self) -> None:
        """Bootstrap a new map from an authored, fingerprint-matched image pack.

        Existing indexes, including deliberately cleared memories, never enter
        this path. A remapped environment cannot inherit old coordinates.
        """
        if self._seed_dir is None or self._map_name is None or self._dir is None:
            return
        source = self._seed_dir / Path(self._map_name).stem
        try:
            index = json.loads((source / "index.json").read_text())
            if (
                not isinstance(index, dict)
                or index.get("version") != _INDEX_VERSION
                or index.get("fingerprint") != self._fingerprint
            ):
                return
            memories = [Memory(**entry) for entry in index["memories"]]
            if len(memories) > 100 or any(
                type(memory.id) is not int
                or memory.id < 1
                or not all(math.isfinite(value) for value in (memory.x, memory.y, memory.theta, memory.stamp))
                or not isinstance(memory.label, str)
                or len(memory.label) > 200
                for memory in memories
            ):
                return
            if len({memory.id for memory in memories}) != len(memories):
                return
            images = [(source / f"{memory.id}.jpg").read_bytes() for memory in memories]
            if any(not image.startswith(b"\xff\xd8") for image in images):
                return
            for memory, jpeg in zip(memories, images, strict=True):
                self._write_image_locked(memory.id, jpeg)
            self._memories = memories
            self._next_id = max((memory.id for memory in memories), default=0) + 1
            self._commit_locked()
        except (OSError, ValueError, TypeError, KeyError):
            return  # a missing/broken optional pack must not prevent recording

    def _adopt_stage_locked(self, session_started: float) -> bool:
        """Re-attach to a stage this same SLAM session built (a brain restart
        mid-tour): the frame is still live, so the half-tour — memories,
        fingerprint, ids — survives. False means the stage is another
        session's (or unreadable) and the caller wipes it."""
        assert self._dir is not None
        try:
            index = json.loads((self._dir / "index.json").read_text())
            if not isinstance(index, dict) or index.get("version") != _INDEX_VERSION:
                return False
            if index.get("session") != session_started or not index.get("fingerprint"):
                return False
            self._restore_locked(index)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False
        self._fingerprint = str(index["fingerprint"])
        return True

    def _restore_locked(self, index: dict) -> None:
        self._memories = [Memory(**entry) for entry in index.get("memories", [])]
        self._next_id = int(index.get("next_id", 1))

    def _wipe_locked(self) -> None:
        """The map was re-made (or the index is unreadable): the coordinates lie."""
        assert self._dir is not None
        if self._dir.is_dir():
            for stale in self._dir.glob("*.jpg*"):  # images and any crash-orphaned .jpg.tmp
                stale.unlink(missing_ok=True)
            (self._dir / "index.json").unlink(missing_ok=True)
            (self._dir / "files.json").unlink(missing_ok=True)
        self._memories = []
        self._next_id = 1

    def _write_image_locked(self, memory_id: int, jpeg: bytes) -> None:
        # tmp + replace like the index: the proxy and upload threads read these
        # files without the lock and must never see a torn frame.
        assert self._dir is not None
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / f"{memory_id}.jpg.tmp"
        tmp.write_bytes(jpeg)
        os.replace(tmp, self._dir / f"{memory_id}.jpg")

    def _commit_locked(self) -> None:
        assert self._dir is not None
        index = {
            "version": _INDEX_VERSION,
            "map": self._map_name,
            "fingerprint": self._fingerprint,
            "session": self._session,
            "next_id": self._next_id,
            "memories": [asdict(m) for m in self._memories],
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / "index.json.tmp"
        tmp.write_text(json.dumps(index))
        os.replace(tmp, self._dir / "index.json")
        self._revision += 1
        self.last_change_monotonic = time.monotonic()


def _scratch_map_dir(root: Path, scratch: Path) -> Path | None:
    """The saved-map directory whose memories a promotion scratch dir is
    holding, or None when it holds none a map could be waiting on — an
    unreadable index, or a copy of the stage, which the stage itself still has."""
    try:
        map_name = str(json.loads((scratch / "index.json").read_text())["map"])
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        return None
    return None if map_name == MAPPING_SESSION else root / Path(map_name).stem


def _land(scratch: Path, target: Path) -> bool:
    """Move a scratch copy into place, reporting whether it got there. False
    leaves it where it stands for the next recovery to retry — never for a
    caller to treat as spent."""
    if not scratch.is_dir():
        return False
    try:
        os.replace(scratch, target)
    except OSError:
        return False
    return True


def _staged_index(stage: Path) -> dict | None:
    """The on-disk stage's index; None when absent or unreadable. Every
    mutation commits inside the store lock, so the disk index is never
    behind the live session."""
    try:
        index = json.loads((stage / "index.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(index, dict) or index.get("version") != _INDEX_VERSION or index.get("map") != MAPPING_SESSION:
        return None
    return index


def _map_source(maps_dir: Path, map_name: str) -> Path:
    pgm = maps_dir / (Path(map_name).stem + ".pgm")
    return pgm if pgm.exists() else maps_dir / map_name


def _map_stat(maps_dir: Path, map_name: str) -> tuple[int, int] | None:
    try:
        stat = _map_source(maps_dir, map_name).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _map_fingerprint(maps_dir: Path, map_name: str) -> str:
    """Identity of the map's content, not its name — a re-mapped room must not match."""
    try:
        return hashlib.sha256(_map_source(maps_dir, map_name).read_bytes()).hexdigest()
    except OSError:
        return ""
