# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Who the robot has met, on disk under ``data/people/``.

One directory per person: ``profile.json`` for the name and the stamps,
``vectors.npz`` for the face gallery and today's outfit. Writes are
tmp-then-replace, so a robot losing power mid-write keeps the previous version
of that person rather than a half-file; a directory that will not parse is
skipped and the rest of the roster still loads.

Ids are never reused, and forgetting deletes the directory outright — the
product promise is that "forget me" leaves nothing behind.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from brain_client.common.script_paths import get_innate_os_root

MAX_FACES = 12
"""Face vectors kept per person, newest first: the gallery is for recognizing
them tomorrow, not for a record of every look."""

OUTFIT_TTL_SEC = 12 * 3600.0
"""How long an outfit stands for its person. Clothes change overnight; a stale
outfit would carry a name onto whoever wore something similar the next day."""

_SAVE_EVERY_SEC = 30.0
"""While a person is in view their stamps reach disk this often, so a restart
costs at most this much of their recency — not a whole day of outfit life."""

_REDUNDANT = 0.9
"""A vector this close to one already on file says nothing new. The gallery
wants different looks at a person, not twelve copies of the frame they happened
to stand still in — and skipping it is also what keeps a person in view from
costing a file write every tick."""


@dataclass
class Person:
    id: str
    name: str | None = None
    created: float = 0.0
    last_seen: float = 0.0
    faces: list[np.ndarray] = field(default_factory=list)
    outfit: np.ndarray | None = None
    outfit_stamp: float = 0.0
    saved_at: float = 0.0  # in memory only


class Roster:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._people: dict[str, Person] = {}
        self._load()

    def __len__(self) -> int:
        return len(self._people)

    @property
    def named(self) -> int:
        return sum(1 for person in self._people.values() if person.name)

    def name_of(self, person_id: str) -> str | None:
        person = self._people.get(person_id)
        return person.name if person is not None else None

    def faces(self) -> list[tuple[str, np.ndarray]]:
        return [(person.id, face) for person in self._people.values() for face in person.faces]

    def outfits(self, now: float) -> list[tuple[str, np.ndarray]]:
        return [
            (person.id, person.outfit)
            for person in self._people.values()
            if person.outfit is not None and now - person.outfit_stamp < OUTFIT_TTL_SEC
        ]

    def resolve(self, who: str) -> str | None:
        """A person id, or the single person answering to that name."""
        if who in self._people:
            return who
        matches = [person.id for person in self._people.values() if (person.name or "").lower() == who.lower()]
        return matches[0] if len(matches) == 1 else None

    def create(self, faces: list[np.ndarray], now: float) -> str:
        person = Person(id=f"person_{uuid.uuid4().hex[:8]}", created=now, last_seen=now, faces=list(faces[-MAX_FACES:]))
        self._people[person.id] = person
        self._save(person, now)
        return person.id

    def add_face(self, person_id: str, face: np.ndarray, now: float) -> None:
        person = self._people.get(person_id)
        if person is None:
            return
        new = not any(float(face @ stored) >= _REDUNDANT for stored in person.faces)
        if new:
            person.faces = [*person.faces, face][-MAX_FACES:]
        self._touch(person, now, changed=new)

    def set_outfit(self, person_id: str, outfit: np.ndarray, now: float) -> None:
        person = self._people.get(person_id)
        if person is None:
            return
        changed = person.outfit is None or float(person.outfit @ outfit) < _REDUNDANT
        person.outfit, person.outfit_stamp = outfit, now
        self._touch(person, now, changed=changed)

    def seen(self, person_id: str, now: float) -> None:
        person = self._people.get(person_id)
        if person is not None:
            self._touch(person, now, changed=False)

    def rename(self, person_id: str, name: str, now: float) -> bool:
        person = self._people.get(person_id)
        if person is None:
            return False
        person.name = name.strip() or None
        self._save(person, now)
        return True

    def forget(self, person_id: str) -> bool:
        """False when their files could not be removed — and then they stay on
        the roster too: a person whose gallery is still on disk has not been
        forgotten, whatever the reply says."""
        if person_id not in self._people:
            return False
        directory = self._dir / person_id
        try:
            if directory.exists():
                shutil.rmtree(directory)
        except OSError:
            return False
        del self._people[person_id]
        return True

    def _touch(self, person: Person, now: float, *, changed: bool) -> None:
        """A sighting: the stamps move now, the file follows when something
        changed or when it has not been written for _SAVE_EVERY_SEC."""
        person.last_seen = now
        if changed or now - person.saved_at >= _SAVE_EVERY_SEC:
            self._save(person, now)

    def _load(self) -> None:
        for directory in sorted(self._dir.glob("person_*")):
            person = _read(directory)
            if person is not None:
                self._people[person.id] = person

    def _save(self, person: Person, now: float) -> None:
        person.saved_at = now
        directory = self._dir / person.id
        directory.mkdir(parents=True, exist_ok=True)
        vectors: dict[str, np.ndarray] = {"faces": np.stack(person.faces)} if person.faces else {}
        if person.outfit is not None:
            vectors["outfit"] = person.outfit
        _replace(directory / "vectors.npz", lambda path: _savez(path, vectors))
        profile = {
            "id": person.id,
            "name": person.name,
            "created": person.created,
            "last_seen": person.last_seen,
            "outfit_stamp": person.outfit_stamp,
        }
        _replace(directory / "profile.json", lambda path: path.write_text(json.dumps(profile, indent=2)))


def _read(directory: Path) -> Person | None:
    path = directory / "vectors.npz"
    try:
        profile = json.loads((directory / "profile.json").read_text())
        faces: list[np.ndarray] = []
        outfit: np.ndarray | None = None
        if path.exists():
            with np.load(path) as vectors:
                faces = list(vectors["faces"]) if "faces" in vectors else []
                outfit = vectors["outfit"] if "outfit" in vectors else None
        return Person(
            id=str(profile["id"]),
            name=profile.get("name") or None,
            created=float(profile.get("created") or 0.0),
            last_seen=float(profile.get("last_seen") or 0.0),
            faces=faces,
            outfit=outfit,
            outfit_stamp=float(profile.get("outfit_stamp") or 0.0),
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _savez(path: Path, vectors: dict[str, np.ndarray]) -> None:
    """Through the file object: ``np.savez`` appends ``.npz`` to a name that
    lacks it, which would leave the tmp file under a name nothing replaces."""
    with path.open("wb") as handle:
        np.savez(handle, **vectors)


def _replace(path: Path, write: Callable[[Path], object]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    write(tmp)
    os.replace(tmp, path)


def default_dir(*, simulator: bool) -> Path:
    """Sim sightings never mix with the ones from a real room."""
    return get_innate_os_root() / "data" / ("people_sim" if simulator else "people")
