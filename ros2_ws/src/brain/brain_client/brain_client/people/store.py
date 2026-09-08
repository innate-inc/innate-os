# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The people store: the roster (how the robot recognizes someone) and the
person memory (what it knows about them), under one directory and one lock.

Layout under ``data/people/`` (``data/people_sim/`` in the simulator, so sim
evidence never mixes with hardware evidence)::

    index.json              the roster summary, rewritten on change
    <person_id>/person.json profile, facts, episodes, open loops, consent
    <person_id>/templates.npz  face + outfit embeddings, height samples
    <person_id>/thumb_<k>.jpg  up to 3 face thumbnails
    audit.log               one JSON line per mutation

The JSON + npz pattern of :mod:`brain_client.memory.store`, for the same
reasons: one writer process, ten people, inspectable and backup-friendly.
Every write is tmp-file + ``os.replace``; readers on other threads take the
same lock and get plain tuples of frozen records back, so nothing they hold
can change under them.

Person ids are ``person_<8 hex>`` and are NEVER reused: a forgotten or expired
id is tombstoned, so a stale skill reference fails loudly instead of naming a
stranger. Templates carry their model id and are never compared across spaces.
Track tags are not the store's business — only the counter that keeps them
unique across restarts is persisted here.

PURE module: no rclpy. Stamps are epoch seconds.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

import numpy as np

from brain_client.common.enums import StrEnum
from brain_client.people.memory import (
    EPISODE_IDLE_SEC,
    FACT_TEXT_LIMIT,
    MAX_DIGEST_EPISODES,
    MAX_DIGEST_FACTS,
    Attribution,
    Consent,
    Description,
    Episode,
    Fact,
    FactKind,
    FactSource,
    LastSeen,
    NameCandidate,
    Names,
    OpenLoop,
    Profile,
    close_episode,
    latest_open,
    next_sequence_id,
    open_episode,
    profile_from_dict,
    profile_to_dict,
    rank_facts,
    supersede,
)
from brain_client.people.types import (
    EpisodeDict,
    FaceTemplate,
    FactDict,
    HeightEstimate,
    LastSeenDict,
    OpenLoopDict,
    OutfitTemplate,
    PersonDigestDict,
    RecentPersonDict,
    RosterEntryDict,
)

if TYPE_CHECKING:
    from brain_client.people.types import Pose

INDEX_VERSION = 1
MAX_NAMED = 10
MAX_UNNAMED = 20
MAX_FACE_TEMPLATES = 10
MAX_THUMBNAILS = 3
MAX_NAME_CANDIDATES = 5
OUTFIT_TTL_SEC = 48 * 3600.0
RETENTION_UNNAMED_DAYS = 14.0
RETENTION_NAMED_DAYS = 548.0  # 18 months unseen, the Amazon Astro Visual ID rule
_SIGHTING_COMMIT_SEC = 30.0  # the engine records a sighting per tick; disk sees one per half minute
_DIR_MODE = 0o700  # templates and thumbnails are special-category data (RFC section 10)


class AuditAction(StrEnum):
    """What an ``audit.log`` line records; wire-visible to the Settings page."""

    ENROLLED = "enrolled"
    RENAMED = "renamed"
    NAME_LEARNED = "name_learned"
    MERGED = "merged"
    FORGOTTEN = "forgotten"
    FACT_SUPERSEDED = "fact_superseded"
    EXPIRED = "expired"
    COLLECTION = "collection"
    CONSOLIDATED = "consolidated"


class PeopleStore:
    """Implements :class:`brain_client.people.types.RosterView`; every method
    is safe to call from the engine thread, the scribe thread and the service
    callbacks."""

    def __init__(
        self,
        root: Path,
        *,
        retention_unnamed_days: float = RETENTION_UNNAMED_DAYS,
        retention_named_days: float = RETENTION_NAMED_DAYS,
    ):
        self._root = root
        self._retention_unnamed_days = retention_unnamed_days
        self._retention_named_days = retention_named_days
        self._lock = Lock()
        self._collection_enabled = True
        self._next_tag = 1
        self._people: dict[str, Profile] = {}
        self._tombstones: list[str] = []
        self._faces: dict[str, list[FaceTemplate]] = {}
        self._outfits: dict[str, list[OutfitTemplate]] = {}
        self._heights: dict[str, list[tuple[float, float]]] = {}
        self._thumbs: dict[str, list[str]] = {}
        self._committed: dict[str, float] = {}  # person id -> stamp of its last disk write
        self._pending: set[str] = set()
        self._load()

    @property
    def audit_path(self) -> Path:
        return self._root / "audit.log"

    # ------------------------------------------------------------- RosterView
    def person_ids(self) -> list[str]:
        with self._lock:
            return list(self._people)

    def name_of(self, person_id: str) -> str | None:
        with self._lock:
            profile = self._people.get(person_id)
            return profile.names.preferred if profile is not None else None

    def face_templates(self, person_id: str, model: str) -> list[FaceTemplate]:
        with self._lock:
            return [template for template in self._faces.get(person_id, ()) if template.model == model]

    def outfits(self, person_id: str, model: str, now: float) -> list[OutfitTemplate]:
        with self._lock:
            return [
                outfit
                for outfit in self._outfits.get(person_id, ())
                if outfit.model == model and now - outfit.stamp <= OUTFIT_TTL_SEC
            ]

    def height(self, person_id: str) -> HeightEstimate | None:
        with self._lock:
            samples = self._heights.get(person_id, [])
            if not samples:
                return None
            values = np.array([value for value, _ in samples], dtype=np.float64)
            variances = np.array([variance for _, variance in samples], dtype=np.float64)
            spread = float(values.var()) if len(samples) > 1 else 0.0
            return HeightEstimate(
                mean_m=float(np.median(values)),
                variance=(spread + float(variances.mean())) / len(samples),
                samples=len(samples),
            )

    def collection_enabled(self) -> bool:
        with self._lock:
            return self._collection_enabled

    def can_enrol(self) -> bool:
        """Whether background enrolment may create another entry. Explicit
        owner actions (rename, merge) are never gated on this."""
        with self._lock:
            if not self._collection_enabled:
                return False
            unnamed = sum(1 for profile in self._people.values() if not profile.named)
            return unnamed < MAX_UNNAMED and len(self._people) < MAX_NAMED + MAX_UNNAMED

    def create_unnamed(self, faces: list[FaceTemplate], thumbnail: bytes | None, now: float) -> str:
        """A new familiar-but-unnamed person. Empty string when the roster is
        full or collection is off — the resolver checks :meth:`can_enrol` first,
        and a race must not silently exceed the capacity."""
        if not self.can_enrol():
            return ""
        with self._lock:
            person_id = self._mint_id()
            self._people[person_id] = Profile(id=person_id, created=now, last_seen=LastSeen(stamp=now), encounters=1)
            self._faces[person_id] = list(faces)[:MAX_FACE_TEMPLATES]
            self._outfits[person_id] = []
            self._heights[person_id] = []
            self._thumbs[person_id] = []
            self._person_dir(person_id).mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
            if thumbnail:
                self._add_thumbnail_locked(person_id, thumbnail)
            self._commit_person_locked(person_id, now)
            self._commit_templates_locked(person_id)
            self._commit_index_locked()
            self._audit_locked(AuditAction.ENROLLED, person_id, now, faces=len(faces))
            return person_id

    def add_face_template(self, person_id: str, template: FaceTemplate, thumbnail: bytes | None) -> None:
        with self._lock:
            if person_id not in self._people:
                return
            templates = [*self._faces.get(person_id, ()), template]
            self._faces[person_id] = _prune_faces(templates)
            if thumbnail:
                self._add_thumbnail_locked(person_id, thumbnail)
            self._commit_templates_locked(person_id)

    def add_outfit(self, person_id: str, outfit: OutfitTemplate) -> None:
        with self._lock:
            if person_id not in self._people:
                return
            kept = [
                existing
                for existing in self._outfits.get(person_id, ())
                if outfit.stamp - existing.stamp <= OUTFIT_TTL_SEC
            ]
            self._outfits[person_id] = [*kept, outfit]
            self._commit_templates_locked(person_id)

    def add_height_sample(self, person_id: str, height_m: float, variance: float) -> None:
        with self._lock:
            if person_id not in self._people:
                return
            samples = [*self._heights.get(person_id, ()), (height_m, variance)]
            self._heights[person_id] = samples[-64:]
            self._commit_templates_locked(person_id)

    def record_sighting(self, person_id: str, now: float, map_name: str | None, pose: Pose | None) -> None:
        """Called every tick a person is resolved: the encounter counter moves
        only across a gap, and disk sees one write per half minute."""
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None:
                return
            gap = now - profile.last_seen.stamp if profile.last_seen is not None else None
            new_encounter = gap is None or gap > EPISODE_IDLE_SEC
            self._people[person_id] = replace(
                profile,
                last_seen=LastSeen(
                    stamp=now,
                    map=map_name,
                    x=pose[0] if pose is not None else None,
                    y=pose[1] if pose is not None else None,
                ),
                encounters=profile.encounters + (1 if new_encounter else 0),
            )
            self._pending.add(person_id)
            if new_encounter or now - self._committed.get(person_id, 0.0) >= _SIGHTING_COMMIT_SEC:
                self._commit_person_locked(person_id, now)
                self._commit_index_locked()

    def flush(self) -> None:
        """Write out sightings deferred by the commit interval (shutdown, or a
        node timer)."""
        with self._lock:
            for person_id in list(self._pending):
                self._commit_person_locked(person_id, time.time())
            self._commit_index_locked()

    # --------------------------------------------------------- owner controls
    def rename(self, who: str, name: str, source: str, now: float | None = None) -> bool:
        """Bind or replace a person's name. Names are not unique and renaming
        never merges two records; the previous name is kept as an alias so a
        correction stays traceable. Not gated on capacity: an explicit naming
        is the owner's decision, not background enrolment."""
        stamp = _now(now)
        with self._lock:
            profile = self._people.get(who)
            if profile is None or not name.strip():
                return False
            previous = profile.names.preferred
            aliases = tuple(
                dict.fromkeys(alias for alias in (*profile.names.aliases, previous) if alias and alias != name.strip())
            )
            self._people[who] = replace(
                profile,
                names=Names(preferred=name.strip(), aliases=aliases),
                consent=Consent(how=source or None, stamp=stamp),
            )
            self._commit_person_locked(who, stamp)
            self._commit_index_locked()
            action = AuditAction.RENAMED if previous else AuditAction.NAME_LEARNED
            self._audit_locked(action, who, stamp, source=source, previous=previous, name=name.strip())
            return True

    def merge(self, source_id: str, target_id: str, now: float | None = None) -> bool:
        """Fold one record into another, keeping the target's id and name.
        Explicit and audited; the engine never merges on its own."""
        stamp = _now(now)
        with self._lock:
            source = self._people.get(source_id)
            target = self._people.get(target_id)
            if source is None or target is None or source_id == target_id:
                return False
            self._people[target_id] = _merged_profile(source, target)
            self._faces[target_id] = _prune_faces([*self._faces.get(target_id, ()), *self._faces.get(source_id, ())])
            self._outfits[target_id] = [*self._outfits.get(target_id, ()), *self._outfits.get(source_id, ())]
            self._heights[target_id] = [*self._heights.get(target_id, ()), *self._heights.get(source_id, ())][-64:]
            for thumb_id in list(self._thumbs.get(source_id, ())):
                jpeg = self._read_thumbnail_locked(source_id, thumb_id)
                if jpeg:
                    self._add_thumbnail_locked(target_id, jpeg)
            self._drop_locked(source_id)
            self._commit_person_locked(target_id, stamp)
            self._commit_templates_locked(target_id)
            self._commit_index_locked()
            self._audit_locked(AuditAction.MERGED, target_id, stamp, source_id=source_id)
            return True

    def forget(self, who: str, now: float | None = None) -> bool:
        """Delete everything about a person and tombstone the id."""
        stamp = _now(now)
        with self._lock:
            if who not in self._people:
                return False
            self._drop_locked(who)
            self._commit_index_locked()
            self._audit_locked(AuditAction.FORGOTTEN, who, stamp)
            return True

    def set_collection(self, enabled: bool, now: float | None = None) -> None:
        """The owner's "never collect" preference: a collection control, not a
        deletion. Tracking continues anonymously."""
        stamp = _now(now)
        with self._lock:
            if self._collection_enabled == enabled:
                return
            self._collection_enabled = enabled
            self._commit_index_locked()
            self._audit_locked(AuditAction.COLLECTION, "", stamp, enabled=enabled)

    def is_tombstoned(self, person_id: str) -> bool:
        with self._lock:
            return person_id in self._tombstones

    def next_tag(self) -> int:
        """The track-tag counter, persisted so ``P<n>`` never repeats across a
        restart. Tag policy itself belongs to the tracker."""
        with self._lock:
            return self._next_tag

    def set_next_tag(self, value: int) -> None:
        with self._lock:
            if value <= self._next_tag:
                return
            self._next_tag = value
            self._commit_index_locked()

    def counts(self) -> tuple[int, int]:
        """(named, unnamed) — what the Settings page compares against
        MAX_NAMED / MAX_UNNAMED to say the roster is full."""
        with self._lock:
            named = sum(1 for profile in self._people.values() if profile.named)
            return named, len(self._people) - named

    # -------------------------------------------------------------- the memory
    def profile(self, person_id: str) -> Profile | None:
        with self._lock:
            return self._people.get(person_id)

    def add_fact(
        self,
        person_id: str,
        text: str,
        kind: FactKind,
        *,
        now: float,
        attribution: Attribution = Attribution.UNCERTAIN,
        source: FactSource | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
    ) -> str | None:
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or not text.strip():
                return None
            fact = self._build_fact_locked(profile, text, kind, now, attribution, source, confidence, importance)
            self._people[person_id] = replace(profile, facts=(*profile.facts, fact))
            self._commit_person_locked(person_id, now)
            return fact.id

    def supersede_fact(
        self,
        person_id: str,
        fact_id: str,
        text: str,
        kind: FactKind,
        *,
        now: float,
        attribution: Attribution = Attribution.UNCERTAIN,
        source: FactSource | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
    ) -> str | None:
        """Replace an outdated fact, keeping the original's first_confirmed and
        an audit line. The old record stays, pointing at its replacement."""
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or not text.strip():
                return None
            fact = self._build_fact_locked(profile, text, kind, now, attribution, source, confidence, importance)
            self._people[person_id] = replace(profile, facts=supersede(profile.facts, fact_id, fact))
            self._commit_person_locked(person_id, now)
            self._audit_locked(AuditAction.FACT_SUPERSEDED, person_id, now, fact_id=fact_id, replacement=fact.id)
            return fact.id

    def add_open_loop(
        self, person_id: str, text: str, *, now: float, due: str | None = None, source: str = ""
    ) -> str | None:
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or not text.strip():
                return None
            loop = OpenLoop(
                id=next_sequence_id("o", (existing.id for existing in profile.open_loops)),
                text=text.strip()[:FACT_TEXT_LIMIT],
                created=now,
                due=due,
                source=source,
            )
            self._people[person_id] = replace(profile, open_loops=(*profile.open_loops, loop))
            self._commit_person_locked(person_id, now)
            return loop.id

    def complete_open_loop(self, person_id: str, loop_id: str, now: float | None = None) -> bool:
        stamp = _now(now)
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or all(loop.id != loop_id for loop in profile.open_loops):
                return False
            self._people[person_id] = replace(
                profile,
                open_loops=tuple(
                    replace(loop, done=True) if loop.id == loop_id else loop for loop in profile.open_loops
                ),
            )
            self._commit_person_locked(person_id, stamp)
            return True

    def open_episode(
        self,
        person_id: str,
        now: float,
        *,
        map_name: str | None = None,
        pose: Pose | None = None,
        present: tuple[str, ...] = (),
    ) -> str | None:
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None:
                return None
            episode = Episode(
                id=next_sequence_id("e", (existing.id for existing in profile.episodes)),
                start=now,
                map=map_name,
                x=pose[0] if pose is not None else None,
                y=pose[1] if pose is not None else None,
                present=present,
            )
            self._people[person_id] = replace(profile, episodes=open_episode(profile.episodes, episode))
            self._commit_person_locked(person_id, now)
            return episode.id

    def close_episode(self, person_id: str, now: float, summary: str = "") -> bool:
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or latest_open(profile.episodes) is None:
                return False
            self._people[person_id] = replace(profile, episodes=close_episode(profile.episodes, now, summary))
            self._commit_person_locked(person_id, now)
            return True

    def note_episode(self, person_id: str, summary: str, now: float | None = None) -> bool:
        """The scribe's episode note lands on the open episode; without one it
        is dropped rather than invented against an older encounter."""
        stamp = _now(now)
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or not summary.strip():
                return False
            episode = latest_open(profile.episodes)
            if episode is None:
                return False
            noted = replace(episode, summary=summary.strip())
            self._people[person_id] = replace(
                profile,
                episodes=tuple(noted if existing.id == episode.id else existing for existing in profile.episodes),
            )
            self._commit_person_locked(person_id, stamp)
            return True

    def add_name_candidate(
        self, person_id: str, name: str, *, now: float, quote: str = "", tag: str = "", confidence: float = 0.0
    ) -> bool:
        """A heard name that did not meet the commit rules (RFC 6.3). It waits
        for the conversation to resolve it and expires with the encounter."""
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or not name.strip():
                return False
            candidate = NameCandidate(name=name.strip(), stamp=now, quote=quote, tag=tag, confidence=confidence)
            others = tuple(existing for existing in profile.name_candidates if existing.name != candidate.name)
            self._people[person_id] = replace(profile, name_candidates=(*others, candidate)[-MAX_NAME_CANDIDATES:])
            self._commit_person_locked(person_id, now)
            return True

    def clear_name_candidates(self, person_id: str, now: float | None = None) -> None:
        stamp = _now(now)
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or not profile.name_candidates:
                return
            self._people[person_id] = replace(profile, name_candidates=())
            self._commit_person_locked(person_id, stamp)

    def set_description(self, person_id: str, text: str, *, now: float, thumbnail_id: str | None = None) -> bool:
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or not text.strip():
                return False
            self._people[person_id] = replace(
                profile, description=Description(text=text.strip(), stamp=now, thumbnail_id=thumbnail_id)
            )
            self._commit_person_locked(person_id, now)
            return True

    def description(self, person_id: str) -> str | None:
        with self._lock:
            profile = self._people.get(person_id)
            return profile.description.text if profile is not None and profile.description is not None else None

    # ------------------------------------------------------------- thumbnails
    def add_thumbnail(self, person_id: str, jpeg: bytes) -> str | None:
        with self._lock:
            if person_id not in self._people or not jpeg:
                return None
            return self._add_thumbnail_locked(person_id, jpeg)

    def thumbnail_ids(self, person_id: str) -> list[str]:
        with self._lock:
            return list(self._thumbs.get(person_id, ()))

    def thumbnails(self, person_id: str) -> list[bytes]:
        with self._lock:
            jpegs = [self._read_thumbnail_locked(person_id, thumb) for thumb in self._thumbs.get(person_id, ())]
            return [jpeg for jpeg in jpegs if jpeg]

    # ---------------------------------------------------------------- reading
    def digest(self, person_id: str, now: float) -> PersonDigestDict | None:
        """What the snapshot carries for a person in view: ranked facts, every
        open loop, the last episode summaries, last seen and the encounter
        count. Sensitive facts, appearance notes and unattributed facts stay
        out — they are deep recall only (RFC 6.3)."""
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None:
                return None
            facts: list[FactDict] = [
                {
                    "id": fact.id,
                    "text": fact.text,
                    "kind": str(fact.kind),
                    "importance": round(fact.importance, 3),
                    "last_confirmed": fact.last_confirmed,
                }
                for fact in rank_facts(profile.facts, now)[:MAX_DIGEST_FACTS]
            ]
            loops: list[OpenLoopDict] = [
                {"id": loop.id, "text": loop.text, "due": loop.due} for loop in profile.open_loops if not loop.done
            ]
            summarized = [episode for episode in profile.episodes if episode.summary]
            episodes: list[EpisodeDict] = [
                {"start": episode.start, "end": episode.end, "map": episode.map, "summary": episode.summary}
                for episode in summarized[-MAX_DIGEST_EPISODES:]
            ]
            return {
                "facts": facts,
                "open_loops": loops,
                "episodes": episodes,
                "last_seen": _last_seen_dict(profile),
                "encounters": profile.encounters,
            }

    def recent(self, since: float) -> list[RecentPersonDict]:
        """Everyone seen since a stamp, most recent first — the snapshot's
        "seen earlier today" line."""
        with self._lock:
            seen = [
                profile
                for profile in self._people.values()
                if profile.last_seen is not None and profile.last_seen.stamp >= since
            ]
            seen.sort(key=lambda profile: -(profile.last_seen.stamp if profile.last_seen else 0.0))
            return [
                {
                    "person_id": profile.id,
                    "name": profile.names.preferred,
                    "last_seen": profile.last_seen.stamp if profile.last_seen else 0.0,
                    "map": profile.last_seen.map if profile.last_seen else None,
                }
                for profile in seen
            ]

    def roster(self, *, include_thumbnails: bool = False) -> list[RosterEntryDict]:
        """The Settings page's list, newest sighting first. Thumbnails ride as
        base64 JPEG only when asked: they are the heaviest thing here."""
        with self._lock:
            profiles = sorted(
                self._people.values(),
                key=lambda profile: -(profile.last_seen.stamp if profile.last_seen else profile.created),
            )
            return [self._roster_entry_locked(profile, include_thumbnails) for profile in profiles]

    def _roster_entry_locked(self, profile: Profile, include_thumbnails: bool) -> RosterEntryDict:
        thumb_ids = self._thumbs.get(profile.id, ())
        jpeg = self._read_thumbnail_locked(profile.id, thumb_ids[-1]) if include_thumbnails and thumb_ids else None
        return {
            "person_id": profile.id,
            "name": profile.names.preferred,
            "unnamed": not profile.named,
            "encounters": profile.encounters,
            "last_seen": _last_seen_dict(profile),
            "description": profile.description.text if profile.description else None,
            "thumbnail": base64.b64encode(jpeg).decode() if jpeg else None,
        }

    # ------------------------------------------------------------- retention
    def expire(self, now: float) -> list[str]:
        """Drop outfits past 48 h and people past their retention window;
        returns the ids removed. Their ids stay tombstoned."""
        removed: list[str] = []
        with self._lock:
            for person_id in list(self._people):
                outfits = self._outfits.get(person_id, [])
                kept = [outfit for outfit in outfits if now - outfit.stamp <= OUTFIT_TTL_SEC]
                if len(kept) != len(outfits):
                    self._outfits[person_id] = kept
                    self._commit_templates_locked(person_id)
            for person_id, profile in list(self._people.items()):
                if now - self._last_activity(profile) <= self._retention_sec(profile):
                    continue
                self._drop_locked(person_id)
                self._audit_locked(AuditAction.EXPIRED, person_id, now, named=profile.named)
                removed.append(person_id)
            if removed:
                self._commit_index_locked()
        return removed

    def _retention_sec(self, profile: Profile) -> float:
        days = profile.retention_days
        if days is None:
            days = self._retention_named_days if profile.named else self._retention_unnamed_days
        return days * 86400.0

    @staticmethod
    def _last_activity(profile: Profile) -> float:
        return profile.last_seen.stamp if profile.last_seen is not None else profile.created

    # -------------------------------------------------------- locked internals
    def _build_fact_locked(
        self,
        profile: Profile,
        text: str,
        kind: FactKind,
        now: float,
        attribution: Attribution,
        source: FactSource | None,
        confidence: float,
        importance: float,
    ) -> Fact:
        return Fact(
            id=next_sequence_id("f", (fact.id for fact in profile.facts)),
            text=text.strip()[:FACT_TEXT_LIMIT],
            kind=kind,
            confidence=confidence,
            attribution=attribution,
            source=source or FactSource(stamp=now),
            first_confirmed=now,
            last_confirmed=now,
            importance=importance,
        )

    def _mint_id(self) -> str:
        while True:
            person_id = f"person_{os.urandom(4).hex()}"
            if person_id not in self._people and person_id not in self._tombstones:
                return person_id

    def _drop_locked(self, person_id: str) -> None:
        self._people.pop(person_id, None)
        self._faces.pop(person_id, None)
        self._outfits.pop(person_id, None)
        self._heights.pop(person_id, None)
        self._thumbs.pop(person_id, None)
        self._committed.pop(person_id, None)
        self._pending.discard(person_id)
        if person_id not in self._tombstones:
            self._tombstones.append(person_id)
        shutil.rmtree(self._person_dir(person_id), ignore_errors=True)

    def _add_thumbnail_locked(self, person_id: str, jpeg: bytes) -> str:
        thumbs = self._thumbs.setdefault(person_id, [])
        used = [_thumb_number(thumb) for thumb in thumbs]
        thumb_id = f"thumb_{max(used, default=-1) + 1}"
        directory = self._person_dir(person_id)
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        tmp = directory / f"{thumb_id}.jpg.tmp"
        tmp.write_bytes(jpeg)
        os.replace(tmp, directory / f"{thumb_id}.jpg")
        thumbs.append(thumb_id)
        while len(thumbs) > MAX_THUMBNAILS:
            (directory / f"{thumbs.pop(0)}.jpg").unlink(missing_ok=True)
        return thumb_id

    def _read_thumbnail_locked(self, person_id: str, thumb_id: str) -> bytes | None:
        try:
            return (self._person_dir(person_id) / f"{thumb_id}.jpg").read_bytes()
        except OSError:
            return None

    def _person_dir(self, person_id: str) -> Path:
        return self._root / person_id

    def _commit_person_locked(self, person_id: str, now: float) -> None:
        profile = self._people.get(person_id)
        if profile is None:
            return
        directory = self._person_dir(person_id)
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        _write_json(directory / "person.json", profile_to_dict(profile))
        self._committed[person_id] = now
        self._pending.discard(person_id)

    def _commit_templates_locked(self, person_id: str) -> None:
        directory = self._person_dir(person_id)
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        faces = self._faces.get(person_id, [])
        outfits = self._outfits.get(person_id, [])
        heights = self._heights.get(person_id, [])
        tmp = directory / "templates.npz.tmp"
        with tmp.open("wb") as handle:
            np.savez(
                handle,
                face_embeddings=_stack([template.embedding for template in faces]),
                face_meta=json.dumps(
                    [
                        {
                            "model": template.model,
                            "stamp": template.stamp,
                            "pose_bucket": template.pose_bucket,
                            "quality": template.quality,
                            "thumbnail_id": template.thumbnail_id,
                        }
                        for template in faces
                    ]
                ),
                outfit_embeddings=_stack([outfit.embedding for outfit in outfits]),
                outfit_meta=json.dumps(
                    [
                        {"model": outfit.model, "stamp": outfit.stamp, "thumbnail_id": outfit.thumbnail_id}
                        for outfit in outfits
                    ]
                ),
                height_samples=np.array(heights, dtype=np.float32).reshape(-1, 2),
            )
        os.replace(tmp, directory / "templates.npz")

    def _commit_index_locked(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        _write_json(
            self._root / "index.json",
            {
                "version": INDEX_VERSION,
                "collection_enabled": self._collection_enabled,
                "next_tag": self._next_tag,
                "people": [
                    {
                        "id": profile.id,
                        "name": profile.names.preferred,
                        "created": profile.created,
                        "last_seen": profile.last_seen.stamp if profile.last_seen else 0.0,
                        "encounters": profile.encounters,
                        "unnamed": not profile.named,
                    }
                    for profile in self._people.values()
                ],
                "tombstones": list(self._tombstones),
            },
        )

    def _audit_locked(self, action: AuditAction, person_id: str, stamp: float, **detail: object) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        line = json.dumps({"stamp": stamp, "action": str(action), "person_id": person_id, **detail})
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # -------------------------------------------------------------- loading
    def _load(self) -> None:
        index = _read_json(self._root / "index.json")
        if index is None:
            self._root.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
            return
        if index.get("version") != INDEX_VERSION:
            return  # a future/older index is not ours to interpret; the roster starts empty
        self._collection_enabled = bool(index.get("collection_enabled", True))
        self._next_tag = int(index.get("next_tag", 1) or 1)
        self._tombstones = [str(person_id) for person_id in index.get("tombstones", []) if person_id]
        for entry in index.get("people", []):
            person_id = str(entry.get("id", "")) if isinstance(entry, dict) else ""
            if not person_id or person_id in self._tombstones:
                continue
            profile = self._load_profile(person_id)
            if profile is None:
                continue
            self._people[person_id] = profile
            self._load_templates(person_id)
            self._thumbs[person_id] = _thumbnail_ids(self._person_dir(person_id))

    def _load_profile(self, person_id: str) -> Profile | None:
        data = _read_json(self._person_dir(person_id) / "person.json")
        if data is None or data.get("id") != person_id:
            return None
        return profile_from_dict(data)

    def _load_templates(self, person_id: str) -> None:
        self._faces[person_id] = []
        self._outfits[person_id] = []
        self._heights[person_id] = []
        path = self._person_dir(person_id) / "templates.npz"
        try:
            with np.load(path, allow_pickle=False) as data:
                faces = np.asarray(data["face_embeddings"], dtype=np.float32)
                face_meta = json.loads(str(data["face_meta"]))
                outfits = np.asarray(data["outfit_embeddings"], dtype=np.float32)
                outfit_meta = json.loads(str(data["outfit_meta"]))
                heights = np.asarray(data["height_samples"], dtype=np.float32)
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return  # unreadable templates cost recognition, never the memory
        self._faces[person_id] = [
            FaceTemplate(
                embedding=faces[row],
                model=str(meta.get("model", "")),
                stamp=float(meta.get("stamp", 0.0)),
                pose_bucket=str(meta.get("pose_bucket", "frontal")),
                quality=float(meta.get("quality", 0.0)),
                thumbnail_id=meta.get("thumbnail_id"),
            )
            for row, meta in enumerate(face_meta)
            if row < len(faces)
        ]
        self._outfits[person_id] = [
            OutfitTemplate(
                embedding=outfits[row],
                model=str(meta.get("model", "")),
                stamp=float(meta.get("stamp", 0.0)),
                thumbnail_id=meta.get("thumbnail_id"),
            )
            for row, meta in enumerate(outfit_meta)
            if row < len(outfits)
        ]
        self._heights[person_id] = [(float(value), float(variance)) for value, variance in heights.reshape(-1, 2)]


def _last_seen_dict(profile: Profile) -> LastSeenDict | None:
    last = profile.last_seen
    if last is None:
        return None
    return {"stamp": last.stamp, "map": last.map, "x": last.x, "y": last.y}


def _merged_profile(source: Profile, target: Profile) -> Profile:
    """Everything the source knew, under the target's identity. Ids are
    re-issued so two profiles' ``f_01`` do not collide."""
    facts = list(target.facts)
    for fact in source.facts:
        facts.append(replace(fact, id=next_sequence_id("f", (existing.id for existing in facts))))
    episodes = list(target.episodes)
    for episode in source.episodes:
        episodes.append(replace(episode, id=next_sequence_id("e", (existing.id for existing in episodes))))
    loops = list(target.open_loops)
    for loop in source.open_loops:
        loops.append(replace(loop, id=next_sequence_id("o", (existing.id for existing in loops))))
    preferred = target.names.preferred or source.names.preferred
    aliases = tuple(
        dict.fromkeys(
            alias
            for alias in (*target.names.aliases, *source.names.aliases, source.names.preferred)
            if alias and alias != preferred
        )
    )
    return replace(
        target,
        names=Names(preferred=preferred, aliases=aliases),
        description=target.description or source.description,
        created=min(target.created, source.created) if source.created else target.created,
        last_seen=max(
            (last for last in (target.last_seen, source.last_seen) if last is not None),
            key=lambda last: last.stamp,
            default=None,
        ),
        encounters=target.encounters + source.encounters,
        facts=tuple(sorted(facts, key=lambda fact: fact.first_confirmed)),
        episodes=tuple(sorted(episodes, key=lambda episode: episode.start)),
        open_loops=tuple(loops),
        name_candidates=(*target.name_candidates, *source.name_candidates)[-MAX_NAME_CANDIDATES:],
    )


def _prune_faces(templates: list[FaceTemplate]) -> list[FaceTemplate]:
    """Keep the gallery diverse: at capacity the weakest template in the
    best-represented pose bucket goes, never the only one of its pose."""
    kept = list(templates)
    while len(kept) > MAX_FACE_TEMPLATES:
        buckets: dict[str, list[FaceTemplate]] = {}
        for template in kept:
            buckets.setdefault(template.pose_bucket, []).append(template)
        crowded = max(buckets.values(), key=len)
        weakest = min(crowded, key=lambda template: (template.quality, template.stamp))
        kept.remove(weakest)
    return kept


def _stack(embeddings: list[np.ndarray]) -> np.ndarray:
    if not embeddings:
        return np.zeros((0, 0), dtype=np.float32)
    return np.stack([np.asarray(embedding, dtype=np.float32).ravel() for embedding in embeddings])


def _thumbnail_ids(directory: Path) -> list[str]:
    try:
        names = [path.stem for path in directory.glob("thumb_*.jpg")]
    except OSError:
        return []
    return sorted(names, key=_thumb_number)


def _thumb_number(thumb_id: str) -> int:
    tail = thumb_id.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _now(now: float | None) -> float:
    return time.time() if now is None else now
