# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The people store: the roster (face and outfit templates, heights,
thumbnails) and the person memory (profile, facts, episodes, open loops) under
one directory and one lock — ``data/people/`` on hardware, ``data/people_sim/``
in the simulator so the two never mix — laid out as ``index.json`` plus
``<person_id>/{person.json,templates.npz,thumb_<k>.jpg}`` and an ``audit.log``
(RFC 6.1). Same JSON + npz, tmp-file + ``os.replace`` pattern as
:mod:`brain_client.memory.store`; readers on other threads get frozen records
under the same lock. Ids are ``person_<8 hex>`` and never reused (forgotten and
expired ids are tombstoned), templates carry their model id and are never
compared across spaces, and track tags are not the store's business beyond the
persisted counter that keeps them unique across restarts. PURE: no rclpy;
stamps are epoch seconds."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

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
from brain_client.people.track import cosine
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
MAX_OUTFITS = 8
MAX_FACTS = 200  # a profile a scribe writes to for a year would otherwise grow without end
EPISODE_SUMMARY_LIMIT = 300
DIR_MODE = 0o700  # templates and thumbnails are special-category data (RFC section 10)
FILE_MODE = 0o600
OUTFIT_SAME_COSINE = 0.9  # above this it is the same clothes again, not another outfit


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
    SWEPT = "swept"


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
        self._tombstones: set[str] = set()
        self._faces: dict[str, list[FaceTemplate]] = {}
        self._outfits: dict[str, list[OutfitTemplate]] = {}
        self._heights: dict[str, list[tuple[float, float]]] = {}
        self._thumbs: dict[str, list[str]] = {}
        self._committed: dict[str, float] = {}  # person id -> stamp of its last disk write
        self._pending: set[str] = set()
        self._templates_pending: set[str] = set()
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
            _secure_dir(self._person_dir(person_id))
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
            self._outfits[person_id] = _with_outfit(kept, outfit)
            self._commit_templates_locked(person_id)

    def add_height_sample(self, person_id: str, height_m: float, variance: float) -> None:
        """The resolver sends one of these a second per settled person, so the
        sample lands in memory and the templates file waits for the next
        :meth:`flush` — rewriting every face and outfit vector per second is
        ~90 MB an hour of eMMC writes per person in view."""
        with self._lock:
            if person_id not in self._people:
                return
            samples = [*self._heights.get(person_id, ()), (height_m, variance)]
            self._heights[person_id] = samples[-64:]
            self._templates_pending.add(person_id)

    def record_sighting(self, person_id: str, now: float, map_name: str | None, pose: Pose | None) -> None:
        """Called every tick a person is resolved: one encounter is one episode,
        both moving only across a gap, and disk sees one write per half minute."""
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None:
                return
            last_seen = profile.last_seen
            gap = now - last_seen.stamp if last_seen is not None else None
            new_encounter = gap is None or gap > EPISODE_IDLE_SEC
            episodes = profile.episodes
            if new_encounter and last_seen is not None:
                episodes = close_episode(episodes, last_seen.stamp)
            if latest_open(episodes) is None:
                episodes = open_episode(episodes, self._episode_at(episodes, now, map_name, pose))
            self._people[person_id] = replace(
                profile,
                last_seen=LastSeen(
                    stamp=now,
                    map=map_name,
                    x=pose[0] if pose is not None else None,
                    y=pose[1] if pose is not None else None,
                ),
                encounters=profile.encounters + (1 if new_encounter else 0),
                episodes=episodes,
            )
            self._pending.add(person_id)
            if new_encounter or now - self._committed.get(person_id, 0.0) >= _SIGHTING_COMMIT_SEC:
                self._commit_person_locked(person_id, now)
                self._commit_index_locked()

    def flush(self, now: float | None = None) -> None:
        """Write out the sightings and height samples deferred by the commit
        interval, and close the episodes of people who have left (shutdown, or a
        node timer)."""
        stamp = _now(now)
        with self._lock:
            for person_id in list(self._people):
                self._close_idle_episode_locked(person_id, stamp)
            for person_id in list(self._pending):
                self._commit_person_locked(person_id, stamp)
            for person_id in list(self._templates_pending):
                self._commit_templates_locked(person_id)
            self._commit_index_locked()

    def _close_idle_episode_locked(self, person_id: str, now: float) -> None:
        profile = self._people[person_id]
        episode = latest_open(profile.episodes)
        if episode is None:
            return
        last_seen = profile.last_seen.stamp if profile.last_seen is not None else episode.start
        if now - last_seen <= EPISODE_IDLE_SEC:
            return
        self._people[person_id] = replace(profile, episodes=close_episode(profile.episodes, last_seen))
        self._pending.add(person_id)

    @staticmethod
    def _episode_at(episodes: tuple[Episode, ...], now: float, map_name: str | None, pose: Pose | None) -> Episode:
        return Episode(
            id=next_sequence_id("e", (existing.id for existing in episodes)),
            start=now,
            map=map_name,
            x=pose[0] if pose is not None else None,
            y=pose[1] if pose is not None else None,
        )

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
            # The target and then the index that stops naming the source are
            # committed before the source's files go: a crash in between costs an
            # orphan directory _load ignores, never the only copy of the source.
            self._commit_templates_locked(target_id)
            self._commit_person_locked(target_id, stamp)
            self._forget_locked(source_id)
            self._commit_index_locked()
            shutil.rmtree(self._person_dir(source_id), ignore_errors=True)
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

    def capacity_full(self) -> bool:
        """Whether either cap has been reached, so background enrolment has
        stopped (RFC section 9). :meth:`can_enrol` is the same test plus the
        owner's collection switch, which is a choice and not a full roster —
        the Settings page must tell the two apart."""
        named, unnamed = self.counts()
        return unnamed >= MAX_UNNAMED or named + unnamed >= MAX_NAMED + MAX_UNNAMED

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
            self._people[person_id] = replace(profile, facts=_capped((*profile.facts, fact)))
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
            self._people[person_id] = replace(profile, facts=_capped(supersede(profile.facts, fact_id, fact)))
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
            episode = replace(self._episode_at(profile.episodes, now, map_name, pose), present=present)
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
        is dropped rather than invented against an older encounter. One
        encounter is many windows, so the notes accumulate."""
        stamp = _now(now)
        with self._lock:
            profile = self._people.get(person_id)
            if profile is None or not summary.strip():
                return False
            episode = latest_open(profile.episodes)
            if episode is None:
                return False
            noted = replace(episode, summary=_noted(episode.summary, summary.strip()))
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
        """The files go first: a delete that could not reach the disk must be
        reported as the failure it is, not as a person who is gone from the
        roster and still on the card."""
        directory = self._person_dir(person_id)
        if directory.exists():
            shutil.rmtree(directory)
        self._forget_locked(person_id)

    def _forget_locked(self, person_id: str) -> None:
        self._people.pop(person_id, None)
        self._faces.pop(person_id, None)
        self._outfits.pop(person_id, None)
        self._heights.pop(person_id, None)
        self._thumbs.pop(person_id, None)
        self._committed.pop(person_id, None)
        self._pending.discard(person_id)
        self._templates_pending.discard(person_id)
        self._tombstones.add(person_id)

    def _add_thumbnail_locked(self, person_id: str, jpeg: bytes) -> str:
        thumbs = self._thumbs.setdefault(person_id, [])
        used = [_thumb_number(thumb) for thumb in thumbs]
        thumb_id = f"thumb_{max(used, default=-1) + 1}"
        directory = self._person_dir(person_id)
        _secure_dir(directory)
        tmp = directory / f"{thumb_id}.jpg.tmp"
        tmp.write_bytes(jpeg)
        os.chmod(tmp, FILE_MODE)
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
        _secure_dir(directory)
        _write_json(directory / "person.json", profile_to_dict(profile))
        self._committed[person_id] = now
        self._pending.discard(person_id)

    def _commit_templates_locked(self, person_id: str) -> None:
        directory = self._person_dir(person_id)
        _secure_dir(directory)
        faces = self._faces.get(person_id, [])
        outfits = self._outfits.get(person_id, [])
        heights = self._heights.get(person_id, [])
        # dict[str, Any]: numpy 2's savez stubs would otherwise bind a grouped array to allow_pickle
        payload: dict[str, Any] = {
            **_grouped([(template.model, template.embedding) for template in faces], "face_embeddings"),
            "face_meta": json.dumps(
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
            **_grouped([(outfit.model, outfit.embedding) for outfit in outfits], "outfit_embeddings"),
            "outfit_meta": json.dumps(
                [
                    {"model": outfit.model, "stamp": outfit.stamp, "thumbnail_id": outfit.thumbnail_id}
                    for outfit in outfits
                ]
            ),
            "height_samples": np.array(heights, dtype=np.float32).reshape(-1, 2),
        }
        tmp = directory / "templates.npz.tmp"
        with tmp.open("wb") as handle:
            np.savez(handle, **payload)
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, directory / "templates.npz")
        self._templates_pending.discard(person_id)

    def _commit_index_locked(self) -> None:
        _secure_dir(self._root)
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
                "tombstones": sorted(self._tombstones),
            },
        )

    def _audit_locked(self, action: AuditAction, person_id: str, stamp: float, **detail: object) -> None:
        _secure_dir(self._root)
        line = json.dumps({"stamp": stamp, "action": str(action), "person_id": person_id, **detail})
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        os.chmod(self.audit_path, FILE_MODE)

    # -------------------------------------------------------------- loading
    def _load(self) -> None:
        _secure_dir(self._root)
        index = _read_json(self._root / "index.json")
        if index is None:
            return
        if index.get("version") != INDEX_VERSION:
            return  # a future/older index is not ours to interpret; the roster starts empty
        self._collection_enabled = bool(index.get("collection_enabled", True))
        self._next_tag = int(index.get("next_tag", 1) or 1)
        self._tombstones = {str(person_id) for person_id in index.get("tombstones", []) if person_id}
        indexed = [
            str(entry.get("id", "")) for entry in index.get("people", []) if isinstance(entry, dict) and entry.get("id")
        ]
        for person_id in indexed:
            if person_id in self._tombstones:
                continue
            try:
                self._load_person(person_id)
            except Exception:  # noqa: BLE001 — a malformed record must not crash-loop a respawning node
                continue  # whatever of them did load stays; the rest of the roster is not theirs to cost
        self._sweep_orphans(set(indexed) - self._tombstones)

    def _load_person(self, person_id: str) -> None:
        profile = self._load_profile(person_id)
        if profile is None:
            return
        self._people[person_id] = profile
        _secure_dir(self._person_dir(person_id))
        self._load_templates(person_id)
        self._thumbs[person_id] = _thumbnail_ids(self._person_dir(person_id))

    def _sweep_orphans(self, indexed: set[str]) -> None:
        """A crash between writing a person's files and committing the index —
        this node is designed around native-library crashes with a 2 s respawn —
        leaves embeddings and thumbnails the roster, expiry, forget and the
        Settings page can never see again."""
        for directory in sorted(self._root.glob("person_*")):
            if directory.name in indexed or not directory.is_dir():
                continue
            try:
                shutil.rmtree(directory)
            except OSError:
                continue  # a sweep that cannot delete waits for the next boot
            self._audit_locked(AuditAction.SWEPT, directory.name, time.time())

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
                face_meta = json.loads(str(data["face_meta"]))
                outfit_meta = json.loads(str(data["outfit_meta"]))
                heights = np.asarray(data["height_samples"], dtype=np.float32)
                groups = {key: np.asarray(data[key], dtype=np.float32) for key in data.files if "embeddings" in key}
            faces = [
                FaceTemplate(
                    embedding=embedding,
                    model=str(meta.get("model", "")),
                    stamp=float(meta.get("stamp", 0.0)),
                    pose_bucket=str(meta.get("pose_bucket", "frontal")),
                    quality=float(meta.get("quality", 0.0)),
                    thumbnail_id=meta.get("thumbnail_id"),
                )
                for meta, embedding in zip(face_meta, _ungrouped(groups, face_meta, "face_embeddings"), strict=True)
                if embedding is not None
            ]
            outfits = [
                OutfitTemplate(
                    embedding=embedding,
                    model=str(meta.get("model", "")),
                    stamp=float(meta.get("stamp", 0.0)),
                    thumbnail_id=meta.get("thumbnail_id"),
                )
                for meta, embedding in zip(
                    outfit_meta, _ungrouped(groups, outfit_meta, "outfit_embeddings"), strict=True
                )
                if embedding is not None
            ]
            samples = [(float(value), float(variance)) for value, variance in heights.reshape(-1, 2)]
        except (OSError, AttributeError, KeyError, TypeError, ValueError):
            # Parsing lives inside the try because this runs in the node's
            # constructor under respawn: one malformed file crash-loops it.
            return  # unreadable templates cost recognition, never the memory
        self._faces[person_id] = faces
        self._outfits[person_id] = outfits
        self._heights[person_id] = samples


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
        facts=_capped(tuple(sorted(facts, key=lambda fact: fact.first_confirmed))),
        episodes=tuple(sorted(episodes, key=lambda episode: episode.start)),
        open_loops=tuple(loops),
        name_candidates=(*target.name_candidates, *source.name_candidates)[-MAX_NAME_CANDIDATES:],
    )


def _capped(facts: tuple[Fact, ...]) -> tuple[Fact, ...]:
    """Facts only ever accumulate, so at the cap the record sheds what it can
    spare: superseded records oldest first, then the least important live ones.
    What the owner entered is never dropped, even over the cap."""
    if len(facts) <= MAX_FACTS:
        return facts
    droppable = sorted((fact for fact in facts if fact.attribution is not Attribution.OWNER), key=_drop_order)
    dropped = {fact.id for fact in droppable[: len(facts) - MAX_FACTS]}
    return tuple(fact for fact in facts if fact.id not in dropped)


def _drop_order(fact: Fact) -> tuple[bool, float, float]:
    live = fact.superseded_by is None
    return (live, fact.importance if live else 0.0, fact.first_confirmed)


def _noted(summary: str, note: str) -> str:
    """The encounter's notes, one window's at a time, bounded to whole
    sentences: replacing the summary would keep only the last window of a
    twenty-minute conversation."""
    if note in summary:
        return summary
    joined = f"{summary} {note}".strip()
    if len(joined) <= EPISODE_SUMMARY_LIMIT:
        return joined
    tail = joined[-EPISODE_SUMMARY_LIMIT:]
    sentence = re.search(r"[.!?]\s+", tail)
    return tail[sentence.end() :] if sentence is not None else tail.lstrip()


def _prune_faces(templates: list[FaceTemplate]) -> list[FaceTemplate]:
    """Keep the gallery diverse: at capacity the weakest template in the
    best-represented pose bucket goes, never the only one of its pose."""
    kept = list(templates)
    while len(kept) > MAX_FACE_TEMPLATES:
        buckets: dict[str, list[int]] = {}
        for index, template in enumerate(kept):
            buckets.setdefault(template.pose_bucket, []).append(index)
        crowded = max(buckets.values(), key=len)
        # By index: `==` on two templates compares their embeddings elementwise.
        del kept[min(crowded, key=lambda index: (kept[index].quality, kept[index].stamp))]
    return kept


def _with_outfit(outfits: list[OutfitTemplate], outfit: OutfitTemplate) -> list[OutfitTemplate]:
    """A face confirmation every five seconds would file the same clothes all day
    long: an outfit that is already on record is refreshed rather than appended,
    and the gallery keeps the most recent few looks."""
    for index, existing in enumerate(outfits):
        if existing.model == outfit.model and cosine(existing.embedding, outfit.embedding) >= OUTFIT_SAME_COSINE:
            refreshed = list(outfits)
            refreshed[index] = replace(existing, stamp=outfit.stamp, thumbnail_id=outfit.thumbnail_id)
            return refreshed
    return sorted([*outfits, outfit], key=lambda entry: entry.stamp)[-MAX_OUTFITS:]


def _grouped(embeddings: list[tuple[str, np.ndarray]], prefix: str) -> dict[str, np.ndarray]:
    """One array per embedding space: a 128-d SFace vector and a 512-d
    InspireFace one cannot share a rectangular array, and the merge that put them
    on one person must not cost the whole file."""
    rows: dict[str, list[np.ndarray]] = {}
    for model, embedding in embeddings:
        rows.setdefault(model, []).append(np.asarray(embedding, dtype=np.float32).ravel())
    return {f"{prefix}__{model}": np.stack(group) for model, group in rows.items()}


def _ungrouped(groups: dict[str, np.ndarray], meta: list, prefix: str) -> list[np.ndarray | None]:
    """Each meta entry's embedding, read back from its own model's array."""
    taken: dict[str, int] = {}
    found: list[np.ndarray | None] = []
    for entry in meta:
        model = str(entry.get("model", "")) if isinstance(entry, dict) else ""
        row = taken.get(model, 0)
        taken[model] = row + 1
        group = groups.get(f"{prefix}__{model}")
        found.append(group[row] if group is not None and row < len(group) else None)
    return found


def _thumbnail_ids(directory: Path) -> list[str]:
    try:
        names = [path.stem for path in directory.glob("thumb_*.jpg")]
    except OSError:
        return []
    return sorted(names, key=_thumb_number)


def _thumb_number(thumb_id: str) -> int:
    tail = thumb_id.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _secure_dir(path: Path) -> None:
    """0700 even when the directory was already there: mkdir's mode applies only
    to a directory it creates, and these files are special-category data."""
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    os.chmod(path, DIR_MODE)


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(tmp, FILE_MODE)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _now(now: float | None) -> float:
    return time.time() if now is None else now
