# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Surfacing: the /brain/people snapshot, the attention policy of RFC 5.5, and
the cooled-down wake events. No ROS, no network."""

from __future__ import annotations

import base64

import numpy as np
import pytest

from brain_client.people.memory import Attribution, FactKind
from brain_client.people.store import PeopleStore
from brain_client.people.surfacing import (
    ATTENTION_NEAR_M,
    DIGEST_STATES,
    FACE_STALE_SEC,
    REENTRY_GAP_SEC,
    PeopleEvents,
    build_snapshot,
    choose_attention,
    per_mille,
    start_of_day,
)
from brain_client.people.types import (
    SNAPSHOT_SCHEMA,
    EventKind,
    Evidence,
    FaceTemplate,
    HealthDict,
    Identity,
    IdentityState,
    TrackState,
)

NOW = 1_788_818_400.0
HEALTH: HealthDict = {
    "camera": "ok",
    "native": "unavailable",
    "face_model": "ok",
    "body_model": "unavailable",
    "gpu": "none",
}


@pytest.fixture
def store(tmp_path) -> PeopleStore:
    return PeopleStore(tmp_path / "people")


def enrol(store: PeopleStore, now: float = NOW) -> str:
    template = FaceTemplate(
        embedding=np.ones(4, dtype=np.float32), model="sface", stamp=now, pose_bucket="frontal", quality=0.9
    )
    return store.create_unnamed([template], b"jpeg", now)


def track(
    tag: str = "P3",
    *,
    state: IdentityState = IdentityState.KNOWN,
    person_id: str | None = None,
    name: str | None = None,
    box: tuple[float, float, float, float] = (0.1, 0.3, 0.93, 0.56),
    head_box: tuple[float, float, float, float] | None = (0.1, 0.38, 0.26, 0.48),
    range_m: float | None = 1.8,
    speaking: bool = False,
    lost: bool = False,
    first_seen: float = NOW - 41.2,
    frames_with_face: int = 3,
    last_face_stamp: float | None = NOW,
    confidence: float = 0.91,
) -> TrackState:
    return TrackState(
        tag=tag,
        box=box,
        head_box=head_box,
        identity=Identity(
            state=state,
            person_id=person_id,
            name=name,
            confidence=confidence,
            evidence=(Evidence.FACE,),
        ),
        first_seen=first_seen,
        last_seen=NOW,
        lost=lost,
        range_m=range_m,
        bearing_deg=-4.0,
        speaking=speaking,
        frames_with_face=frames_with_face,
        last_face_stamp=last_face_stamp,
    )


# ------------------------------------------------------------- the snapshot


def test_boxes_are_per_mille_ints_clamped_to_the_frame():
    assert per_mille((0.1, 0.3, 0.93, 0.56)) == [100, 300, 930, 560]
    assert per_mille((-0.5, 0.0, 1.5, 1.0)) == [0, 0, 1000, 1000]


def test_the_snapshot_carries_the_schema_health_and_the_collection_flag(store: PeopleStore):
    snapshot = build_snapshot([], store, HEALTH, NOW, frame_stamp_ns="1788818400123456789")
    assert snapshot["schema"] == SNAPSHOT_SCHEMA
    assert snapshot["stamp"] == NOW
    assert snapshot["frame_stamp_ns"] == "1788818400123456789"
    assert snapshot["image_size"] == [640, 480]
    assert snapshot["health"] == HEALTH
    assert snapshot["collection_enabled"] is True
    assert snapshot["people"] == []
    assert snapshot["attention"] is None


def test_a_person_in_view_carries_the_documented_fields(store: PeopleStore):
    person_id = enrol(store)
    store.rename(person_id, "Theo", "conversation", now=NOW)
    store.set_description(person_id, "Man, 30s, glasses.", now=NOW)
    snapshot = build_snapshot([track(person_id=person_id, name="Theo")], store, HEALTH, NOW)
    person = snapshot["people"][0]
    assert person["tag"] == "P3"
    assert person["person_id"] == person_id
    assert person["name"] == "Theo"
    assert person["state"] == "known"
    assert person["evidence"] == ["face"]
    assert person["confidence"] == 0.91
    assert person["bbox"] == [100, 300, 930, 560]
    assert person["head_bbox"] == [100, 380, 260, 480]
    assert person["range_m"] == 1.8
    assert person["bearing_deg"] == -4.0
    assert person["tracked_sec"] == 41.2
    assert person["lost"] is False
    assert person["description"] == "Man, 30s, glasses."
    assert person["digest"] is not None


def test_only_people_the_robot_has_a_claim_on_get_a_digest(store: PeopleStore):
    person_id = enrol(store)
    for state in DIGEST_STATES:
        snapshot = build_snapshot([track(person_id=person_id, state=state)], store, HEALTH, NOW)
        assert snapshot["people"][0]["digest"] is not None
    for state in (IdentityState.UNKNOWN, IdentityState.CONFLICT):
        snapshot = build_snapshot([track(person_id=person_id, state=state)], store, HEALTH, NOW)
        assert snapshot["people"][0]["digest"] is None


def test_an_unidentified_track_has_no_digest_and_no_description(store: PeopleStore):
    snapshot = build_snapshot([track(state=IdentityState.UNKNOWN)], store, HEALTH, NOW)
    assert snapshot["people"][0]["digest"] is None
    assert snapshot["people"][0]["description"] is None
    assert snapshot["people"][0]["person_id"] is None


def test_the_digest_holds_the_facts_and_the_open_loops(store: PeopleStore):
    person_id = enrol(store)
    store.add_fact(person_id, "likes pasta", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    store.add_open_loop(person_id, "find the blue socks", now=NOW, due="2026-09-09")
    snapshot = build_snapshot([track(person_id=person_id)], store, HEALTH, NOW)
    digest = snapshot["people"][0]["digest"]
    assert digest is not None
    assert [fact["text"] for fact in digest["facts"]] == ["likes pasta"]
    assert digest["open_loops"][0]["text"] == "find the blue socks"


def test_a_head_box_the_engine_never_found_stays_none(store: PeopleStore):
    snapshot = build_snapshot([track(head_box=None)], store, HEALTH, NOW)
    assert snapshot["people"][0]["head_bbox"] is None


def test_the_learned_line_and_the_hint_ride_the_person_they_are_about(store: PeopleStore):
    person_id = enrol(store)
    snapshot = build_snapshot(
        [track(person_id=person_id), track("P4")],
        store,
        HEALTH,
        NOW,
        learned={"P3": 'P3 said "I\'m Zoe" — P3 = Zoe from here on'},
        hints={"P4": "heard 'I'm Ana' but two people are in view; if it matters, ask which one"},
    )
    assert snapshot["people"][0]["learned"] is not None and snapshot["people"][0]["hint"] is None
    assert snapshot["people"][1]["hint"] is not None and snapshot["people"][1]["learned"] is None


def test_recent_lists_who_was_seen_today_but_is_not_in_view(store: PeopleStore):
    here, elsewhere = enrol(store, NOW), enrol(store, NOW)
    store.rename(elsewhere, "Marc", "app", now=NOW)
    store.record_sighting(here, NOW, "home", None)
    store.record_sighting(elsewhere, NOW - 600, "hallway", None)
    snapshot = build_snapshot([track(person_id=here)], store, HEALTH, NOW)
    assert [entry["person_id"] for entry in snapshot["recent"]] == [elsewhere]
    assert snapshot["recent"][0]["name"] == "Marc"


def test_recent_stops_at_the_start_of_the_day(store: PeopleStore):
    yesterday = enrol(store, NOW)
    store.record_sighting(yesterday, start_of_day(NOW) - 3600, "hallway", None)
    assert build_snapshot([], store, HEALTH, NOW)["recent"] == []


def test_the_snapshot_mirrors_a_collection_opt_out(store: PeopleStore):
    store.set_collection(False, now=NOW)
    assert build_snapshot([], store, HEALTH, NOW)["collection_enabled"] is False


def test_a_lost_track_still_rides_the_snapshot_flagged_as_lost(store: PeopleStore):
    snapshot = build_snapshot([track(lost=True)], store, HEALTH, NOW)
    assert snapshot["people"][0]["lost"] is True


# -------------------------------------------------------------- attention


def test_nobody_unresolved_means_no_attention_line():
    assert choose_attention([track(state=IdentityState.KNOWN)], NOW) is None
    assert choose_attention([], NOW) is None


def test_someone_talking_to_the_robot_outranks_a_closer_unresolved_person():
    speaking = track("P5", state=IdentityState.UNKNOWN, range_m=3.0, speaking=True)
    near = track("P4", state=IdentityState.POSSIBLE, range_m=1.0)
    attention = choose_attention([near, speaking], NOW)
    assert attention is not None and attention["tag"] == "P5"


def test_an_unresolved_person_within_two_metres_outranks_one_further_away():
    near = track("P4", state=IdentityState.POSSIBLE, range_m=ATTENTION_NEAR_M)
    far = track("P6", state=IdentityState.UNKNOWN, range_m=4.0)
    attention = choose_attention([far, near], NOW)
    assert attention is not None and attention["tag"] == "P4"


def test_the_nearest_unresolved_person_is_chosen_when_nobody_is_near_or_speaking():
    attention = choose_attention(
        [
            track("P6", state=IdentityState.UNKNOWN, range_m=5.0),
            track("P7", state=IdentityState.UNKNOWN, range_m=3.0),
        ],
        NOW,
    )
    assert attention is not None and attention["tag"] == "P7"


def test_a_person_whose_range_is_unknown_is_still_a_candidate():
    attention = choose_attention([track("P8", state=IdentityState.UNKNOWN, range_m=None)], NOW)
    assert attention is not None and attention["tag"] == "P8"
    assert "distance unknown" in attention["text"]


def test_a_known_person_is_never_the_target_and_a_lost_one_is_ignored():
    assert choose_attention([track("P3", state=IdentityState.KNOWN, range_m=0.5)], NOW) is None
    assert choose_attention([track("P4", state=IdentityState.UNKNOWN, lost=True)], NOW) is None


def test_a_face_confirmed_person_is_settled_even_without_a_name():
    """FAMILIAR is the engine's own "decided, just unnamed". Chasing it would
    park the gaze on a settled person and say "still deciding" about a face the
    engine already confirmed."""
    familiar = track("P4", state=IdentityState.FAMILIAR, range_m=0.5)
    unresolved = track("P5", state=IdentityState.UNKNOWN, range_m=3.0)
    assert choose_attention([familiar], NOW) is None
    assert choose_attention([familiar, unresolved], NOW) == choose_attention([unresolved], NOW)

    speaking = track("P4", state=IdentityState.FAMILIAR, range_m=0.5, speaking=True)
    attention = choose_attention([speaking, unresolved], NOW)
    assert attention is not None and attention["tag"] == "P5"


def test_a_conflicted_track_is_worth_a_face():
    attention = choose_attention([track("P4", state=IdentityState.CONFLICT)], NOW)
    assert attention is not None and attention["tag"] == "P4"


def test_the_attention_line_says_the_distance_and_why_the_face_is_missing():
    turned = track("P4", state=IdentityState.POSSIBLE, range_m=2.5, last_face_stamp=NOW - FACE_STALE_SEC - 1)
    attention = choose_attention([turned], NOW)
    assert attention is not None
    assert attention["text"] == "trying to see P4's face (2.5 m away, turned aside)"
    assert attention["head_bbox"] == [100, 380, 260, 480]


def test_a_face_never_seen_reads_differently_from_one_seen_a_moment_ago():
    never = choose_attention([track("P4", state=IdentityState.UNKNOWN, frames_with_face=0, last_face_stamp=None)], NOW)
    just_now = choose_attention([track("P4", state=IdentityState.POSSIBLE, last_face_stamp=NOW)], NOW)
    assert never is not None and "face not seen yet" in never["text"]
    assert just_now is not None and "still deciding" in just_now["text"]


def test_the_attention_target_without_a_head_box_still_produces_a_line():
    attention = choose_attention([track("P4", state=IdentityState.UNKNOWN, head_box=None)], NOW)
    assert attention is not None and attention["head_bbox"] is None


# ----------------------------------------------------------------- events


def test_a_new_person_is_announced_once_with_the_crop():
    events = PeopleEvents()
    first = events.emit([track(person_id="person_1")], NOW, enrolled={"P3": b"jpeg"})
    assert [event["kind"] for event in first] == [EventKind.ENROLLED]
    assert first[0]["image_b64"] == base64.b64encode(b"jpeg").decode()
    assert events.emit([track(person_id="person_1")], NOW + 1, enrolled={"P3": b"jpeg"}) == []


def test_a_person_just_enrolled_is_not_also_announced_as_returning():
    events = PeopleEvents()
    emitted = events.emit([track(person_id="person_1")], NOW, enrolled={"P3": None})
    assert [event["kind"] for event in emitted] == [EventKind.ENROLLED]


def test_a_known_person_seen_again_after_ten_minutes_wakes_the_brain():
    events = PeopleEvents()
    person = track(person_id="person_1", name="Theo")
    assert [event["kind"] for event in events.emit([person], NOW)] == [EventKind.REENTERED]
    assert events.emit([person], NOW + 60) == []
    later = events.emit([person], NOW + 60 + REENTRY_GAP_SEC + 1)
    assert [event["kind"] for event in later] == [EventKind.REENTERED]
    assert "Theo is here again" in later[0]["text"]


def test_an_unidentified_track_raises_no_re_entry():
    assert PeopleEvents().emit([track(state=IdentityState.UNKNOWN)], NOW) == []


def test_a_lost_track_raises_nothing():
    assert PeopleEvents().emit([track(person_id="person_1", lost=True)], NOW) == []


def test_a_learned_name_is_announced_once_per_person():
    events = PeopleEvents()
    line = 'P3 said "I\'m Zoe" — P3 = Zoe from here on'
    first = events.emit([track(person_id="person_1")], NOW, learned={"P3": line})
    assert [event["kind"] for event in first] == [EventKind.REENTERED, EventKind.NAME_LEARNED]
    assert first[1]["text"] == line
    assert events.emit([track(person_id="person_1")], NOW + 30, learned={"P3": line}) == []


def test_a_disambiguation_hint_is_announced_under_its_own_cooldown():
    events = PeopleEvents()
    hint = "heard 'I'm Ana' but two people are in view; if it matters, ask which one"
    first = events.emit([track(person_id="person_1")], NOW, hints={"P3": hint})
    assert [event["kind"] for event in first][-1] == EventKind.DISAMBIGUATION
    assert events.emit([track(person_id="person_1")], NOW + 60, hints={"P3": hint}) == []
    later = events.emit([track(person_id="person_1")], NOW + 300, hints={"P3": hint})
    assert [event["kind"] for event in later] == [EventKind.DISAMBIGUATION]


def test_a_conflict_is_announced_and_then_held_down():
    events = PeopleEvents()
    conflicted = track("P4", state=IdentityState.CONFLICT, person_id="person_1", name="Theo")
    first = events.emit([conflicted], NOW)
    assert [event["kind"] for event in first] == [EventKind.CONFLICT]
    assert "not sure who P4 is" in first[0]["text"]
    assert events.emit([conflicted], NOW + 60) == []


def test_cooldowns_are_per_person():
    events = PeopleEvents()
    events.emit([track("P3", person_id="person_1")], NOW)
    second = events.emit([track("P4", person_id="person_2")], NOW + 1)
    assert [event["kind"] for event in second] == [EventKind.REENTERED]


def test_a_recall_that_found_something_becomes_one_event():
    events = PeopleEvents()
    event = events.recalled("person_1", "Ana", "She asked for the blue socks on Tuesday.", NOW)
    assert event is not None
    assert event["kind"] == EventKind.RECALLED
    assert event["text"] == "Recalled about Ana: She asked for the blue socks on Tuesday."
    assert events.recalled("person_1", "Ana", "Something else.", NOW + 5) is None


def test_an_empty_recall_is_no_event():
    assert PeopleEvents().recalled("person_1", "Ana", "   ", NOW) is None


def test_event_payloads_are_shaped_for_the_wire():
    event = PeopleEvents().emit([track(person_id="person_1", name="Theo")], NOW)[0]
    assert set(event) == {"kind", "stamp", "tag", "person_id", "name", "text", "image_b64"}
    assert event["tag"] == "P3"
    assert event["person_id"] == "person_1"
    assert event["name"] == "Theo"
    assert event["stamp"] == NOW


def test_cooldowns_older_than_the_longest_one_are_forgotten():
    """Tags are never reused and this node runs for weeks: an entry that can no
    longer suppress anything is leaked memory."""
    events = PeopleEvents()
    events.emit([track("P3", person_id="person_1")], NOW)
    assert events._last

    longest = max(events._cooldowns.values())
    events.emit([track("P4", person_id="person_2")], NOW + longest + 1)
    assert [key[1] for key in events._last] == ["person_2"]


def test_the_cooldowns_are_configurable():
    events = PeopleEvents(cooldowns={EventKind.REENTERED: 0.0}, reentry_gap_sec=0.0)
    person = track(person_id="person_1")
    assert events.emit([person], NOW) != []
    assert events.emit([person], NOW + 1) != []
