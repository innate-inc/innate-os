# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The identity resolver: RFC 5.2-5.4, rule by rule.

Embeddings here are three-dimensional unit vectors so a test can dial an exact
cosine to each roster person: ``probe(a, b)`` matches person A at cosine ``a``
and person B at cosine ``b``, and nothing else. That keeps every assertion about
the *rule* rather than about a model.
"""

import math
from dataclasses import dataclass, field

import numpy as np
import pytest

from brain_client.people.resolve import (
    NEW_PERSON,
    BodyThresholds,
    Calibration,
    Resolver,
    ResolverConfig,
    body_thresholds_for,
    face_thresholds_for,
    outfit_age_weight,
)
from brain_client.people.types import (
    BodyObservation,
    FaceObservation,
    FaceTemplate,
    HeightEstimate,
    IdentityState,
    OutfitTemplate,
    Pose,
)

MODEL = "sface-2021dec-128"
BODY_MODEL = "osnet-x0_25-msmt17-512"
GENERIC_BODY_MODEL = "some-reid-256"  # anything but OSNet falls back to the default thresholds
HOUR = 3600.0
A_VECTOR = np.array([1.0, 0.0, 0.0], dtype=np.float32)
B_VECTOR = np.array([0.0, 1.0, 0.0], dtype=np.float32)
BOX = (0.20, 0.40, 0.80, 0.55)


def probe(a: float, b: float = 0.0) -> np.ndarray:
    """A unit vector whose cosine is ``a`` to person A and ``b`` to person B."""
    return np.array([a, b, math.sqrt(max(0.0, 1.0 - a * a - b * b))], dtype=np.float32)


@dataclass
class FakePerson:
    name: str | None = None
    faces: list[FaceTemplate] = field(default_factory=list)
    outfits: list[OutfitTemplate] = field(default_factory=list)
    height: HeightEstimate | None = None


class FakeRoster:
    """A RosterView backed by dicts, recording every write."""

    def __init__(self, people: dict[str, FakePerson] | None = None, *, collection: bool = True, capacity: bool = True):
        self.people = people or {}
        self._collection = collection
        self._capacity = capacity
        self.created: list[list[FaceTemplate]] = []
        self.templates: list[tuple[str, FaceTemplate]] = []
        self.written_outfits: list[tuple[str, OutfitTemplate]] = []
        self.heights: list[tuple[str, float, float]] = []
        self.sightings: list[tuple[str, float]] = []

    def person_ids(self) -> list[str]:
        return list(self.people)

    def name_of(self, person_id: str) -> str | None:
        person = self.people.get(person_id)
        return person.name if person else None

    def face_templates(self, person_id: str, model: str) -> list[FaceTemplate]:
        del model  # the store filters; the resolver must filter again anyway
        person = self.people.get(person_id)
        return list(person.faces) if person else []

    def outfits(self, person_id: str, model: str, now: float) -> list[OutfitTemplate]:
        del model, now
        person = self.people.get(person_id)
        return list(person.outfits) if person else []

    def height(self, person_id: str) -> HeightEstimate | None:
        person = self.people.get(person_id)
        return person.height if person else None

    def collection_enabled(self) -> bool:
        return self._collection

    def can_enrol(self) -> bool:
        return self._capacity

    def create_unnamed(self, faces: list[FaceTemplate], thumbnail: bytes | None, now: float) -> str:
        del thumbnail, now
        self.created.append(list(faces))
        person_id = f"person_new{len(self.created)}"
        self.people[person_id] = FakePerson(name=None, faces=list(faces))
        return person_id

    def add_face_template(self, person_id: str, template: FaceTemplate, thumbnail: bytes | None) -> None:
        del thumbnail
        self.templates.append((person_id, template))

    def add_outfit(self, person_id: str, outfit: OutfitTemplate) -> None:
        self.written_outfits.append((person_id, outfit))

    def add_height_sample(self, person_id: str, height_m: float, variance: float) -> None:
        self.heights.append((person_id, height_m, variance))

    def record_sighting(self, person_id: str, now: float, map_name: str | None, pose: Pose | None) -> None:
        del map_name, pose
        self.sightings.append((person_id, now))


@dataclass
class FakeTrack:
    tag: str = "P1"
    first_seen: float = 0.0
    last_seen: float = 0.0
    lost: bool = False


def roster_with(*, a_name: str | None = "Theo", b_name: str | None = "Ana", model: str = MODEL) -> FakeRoster:
    return FakeRoster(
        {
            "person_a": FakePerson(a_name, [FaceTemplate(A_VECTOR, model, 0.0, "frontal", 1.0)]),
            "person_b": FakePerson(b_name, [FaceTemplate(B_VECTOR, model, 0.0, "frontal", 1.0)]),
        }
    )


def face(
    stamp: float,
    embedding: np.ndarray,
    *,
    quality: float = 1.0,
    size_px: float = 64.0,
    yaw: float = 0.0,
    pitch: float = 0.0,
    model: str = MODEL,
    box=BOX,
    thumbnail: bytes | None = None,
) -> FaceObservation:
    return FaceObservation(
        stamp=stamp,
        box=box,
        size_px=size_px,
        real_px=size_px,  # these come off the native crop, where the two are one measurement
        yaw_deg=yaw,
        pitch_deg=pitch,
        sharpness=200.0,
        luminance=130.0,
        quality=quality,
        model=model,
        embedding=embedding,
        thumbnail=thumbnail,
    )


def body(stamp: float, embedding: np.ndarray, *, model: str = BODY_MODEL, height_px: float = 300.0) -> BodyObservation:
    return BodyObservation(stamp=stamp, box=BOX, height_px=height_px, sharpness=200.0, model=model, embedding=embedding)


def commit_theo(resolver: Resolver, tag: str = "P1", *, start: float = 100.0) -> float:
    """Three accepting, independent face frames over more than a second."""
    for step in range(3):
        resolver.observe_face(tag, face(start + 0.6 * step, probe(0.50)))
    end = start + 1.2
    resolver.resolve([FakeTrack(tag=tag, first_seen=start - 5.0, last_seen=end)], end)
    return end


# ------------------------------------------------------------- calibration


def test_calibration_maps_reject_and_accept_onto_a_symmetric_span():
    calibration = Calibration.between(0.30, 0.42, span=2.0)
    assert calibration.llr(0.30) == pytest.approx(-2.0)
    assert calibration.llr(0.42) == pytest.approx(2.0)


def test_calibration_is_zero_at_the_midpoint():
    assert Calibration.between(0.30, 0.42).llr(0.36) == pytest.approx(0.0)


def test_calibration_rejects_an_accept_line_below_the_reject_line():
    with pytest.raises(ValueError, match="accept"):
        Calibration.between(0.42, 0.30)


def test_sface_is_the_default_embedding_space():
    assert face_thresholds_for(MODEL).accept == 0.42


def test_inspireface_keeps_the_prototype_thresholds():
    thresholds = face_thresholds_for("inspireface-1")
    assert (thresholds.accept, thresholds.reject, thresholds.margin) == (0.50, 0.38, 0.06)


@pytest.mark.parametrize(
    ("age_hours", "weight"), [(0.0, 1.0), (4.0, 1.0), (14.0, 0.75), (24.0, 0.5), (36.0, 0.25), (48.0, 0.0), (72.0, 0.0)]
)
def test_outfit_age_weights(age_hours, weight):
    assert outfit_age_weight(age_hours * HOUR) == pytest.approx(weight)


# ------------------------------------------------------- committing a name


def test_three_accepting_face_frames_over_a_second_commit_the_name():
    resolver = Resolver(roster_with())
    commit_theo(resolver)
    identity = resolver.identity("P1")
    assert identity.state is IdentityState.KNOWN
    assert identity.name == "Theo"
    assert identity.person_id == "person_a"


def test_two_face_frames_are_not_enough():
    resolver = Resolver(roster_with())
    for step in range(2):
        resolver.observe_face("P1", face(100.0 + 0.6 * step, probe(0.50)))
    resolver.resolve([FakeTrack(last_seen=100.6)], 100.6)
    assert resolver.identity("P1").state is not IdentityState.KNOWN


def test_three_face_frames_inside_a_second_are_not_enough():
    resolver = Resolver(roster_with())
    for step in range(3):
        resolver.observe_face("P1", face(100.0 + 0.2 * step, probe(0.50)))
    resolver.resolve([FakeTrack(last_seen=100.4)], 100.4)
    assert resolver.identity("P1").state is not IdentityState.KNOWN


def test_an_unnamed_roster_entry_becomes_familiar_rather_than_known():
    resolver = Resolver(roster_with(a_name=None))
    commit_theo(resolver)
    identity = resolver.identity("P1")
    assert identity.state is IdentityState.FAMILIAR
    assert identity.name is None


def test_blurry_frames_never_change_the_state():
    resolver = Resolver(roster_with())
    for step in range(12):
        resolver.observe_face("P1", face(100.0 + 0.3 * step, probe(0.50), quality=0.02))
    resolver.resolve([FakeTrack(last_seen=104.0)], 104.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


def test_blur_only_delays_the_decision_it_does_not_prevent_it():
    resolver = Resolver(roster_with())
    for step in range(12):
        resolver.observe_face("P1", face(100.0 + 0.3 * step, probe(0.50), quality=0.02))
    commit_theo(resolver, start=110.0)
    assert resolver.identity("P1").state is IdentityState.KNOWN


def test_rejecting_frames_leave_a_track_unknown():
    resolver = Resolver(roster_with())
    for step in range(5):  # too small to enrol, so only the matching rule is under test
        resolver.observe_face("P1", face(100.0 + 0.5 * step, probe(0.10), size_px=44.0))
    resolver.resolve([FakeTrack(last_seen=102.0)], 102.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


def test_a_close_runner_up_blocks_the_commit():
    resolver = Resolver(roster_with())
    for step in range(6):
        resolver.observe_face("P1", face(100.0 + 0.4 * step, probe(0.45, 0.44), quality=0.3))
    resolver.resolve([FakeTrack(last_seen=102.0)], 102.0)
    assert resolver.identity("P1").state is not IdentityState.KNOWN


def test_the_runner_up_is_reported_by_name_for_the_conflict_wording():
    resolver = Resolver(roster_with())
    for step in range(6):
        resolver.observe_face("P1", face(100.0 + 0.4 * step, probe(0.55, 0.44), quality=0.3))
    resolver.resolve([FakeTrack(last_seen=102.0)], 102.0)
    identity = resolver.identity("P1")
    assert identity.runner_up_id == "person_b"
    assert identity.runner_up_name == "Ana"


def test_confidence_is_an_accumulated_posterior_not_a_cosine():
    resolver = Resolver(roster_with())
    commit_theo(resolver)
    confidence = resolver.identity("P1").confidence
    assert 0.9 < confidence <= 1.0  # the frames scored cosine 0.50


def test_an_untouched_tag_has_no_identity():
    assert Resolver(roster_with()).identity("P9").state is IdentityState.UNKNOWN


def test_face_evidence_is_reported_on_the_identity():
    resolver = Resolver(roster_with())
    commit_theo(resolver)
    assert "face" in [str(e) for e in resolver.identity("P1").evidence]


# ------------------------------------------------------------ body evidence


def fresh_outfit_roster(*, age_sec: float = 0.0, name: str | None = "Theo", model: str = BODY_MODEL) -> FakeRoster:
    return FakeRoster(
        {
            "person_a": FakePerson(
                name,
                [FaceTemplate(A_VECTOR, MODEL, 0.0, "frontal", 1.0)],
                [OutfitTemplate(A_VECTOR, model, 1000.0 - age_sec)],
            )
        }
    )


def test_body_evidence_alone_reaches_possible():
    resolver = Resolver(fresh_outfit_roster())
    for step in range(4):
        resolver.observe_body("P1", body(1000.0 + step, probe(0.95)), 1000.0 + step)
    resolver.resolve([FakeTrack(last_seen=1004.0)], 1004.0)
    assert resolver.identity("P1").state is IdentityState.POSSIBLE


def test_body_evidence_never_reaches_known_however_long_it_agrees():
    resolver = Resolver(fresh_outfit_roster())
    for step in range(60):
        resolver.observe_body("P1", body(1000.0 + step, probe(0.99)), 1000.0 + step)
        resolver.resolve([FakeTrack(last_seen=1000.0 + step)], 1000.0 + step)
    assert resolver.identity("P1").state is IdentityState.POSSIBLE


def test_body_evidence_never_enrols_anybody():
    roster = FakeRoster()
    resolver = Resolver(roster)
    for step in range(30):
        resolver.observe_body("P1", body(1000.0 + step, probe(0.99)), 1000.0 + step)
        resolver.resolve([FakeTrack(first_seen=1000.0, last_seen=1000.0 + step)], 1000.0 + step)
    assert roster.created == []


def test_an_outfit_older_than_forty_eight_hours_is_ignored_entirely():
    resolver = Resolver(fresh_outfit_roster(age_sec=49 * HOUR))
    for step in range(6):
        resolver.observe_body("P1", body(1000.0 + step, probe(0.95)), 1000.0 + step)
    resolver.resolve([FakeTrack(last_seen=1006.0)], 1006.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


def test_a_day_old_outfit_needs_more_frames_to_say_the_same_thing():
    def frames_to_possible(age_sec: float) -> int:
        resolver = Resolver(fresh_outfit_roster(age_sec=age_sec))
        for step in range(12):
            stamp = 1000.0 + step
            resolver.observe_body("P1", body(stamp, probe(0.80)), stamp)
            resolver.resolve([FakeTrack(last_seen=stamp)], stamp)
            if resolver.identity("P1").state is IdentityState.POSSIBLE:
                return step + 1
        return 99

    assert frames_to_possible(0.0) == 2
    assert frames_to_possible(24 * HOUR) > frames_to_possible(0.0)
    assert frames_to_possible(49 * HOUR) == 99  # nothing an expired outfit can ever say


def test_a_single_agreeing_outfit_frame_is_a_coincidence_not_evidence():
    resolver = Resolver(fresh_outfit_roster())
    resolver.observe_body("P1", body(1000.0, probe(0.99)), 1000.0)
    resolver.resolve([FakeTrack(last_seen=1000.0)], 1000.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN
    resolver.observe_body("P1", body(1001.0, probe(0.99)), 1001.0)
    resolver.resolve([FakeTrack(last_seen=1001.0)], 1001.0)
    assert resolver.identity("P1").state is IdentityState.POSSIBLE


def test_two_people_in_similar_clothes_agree_with_nobody():
    roster = FakeRoster(
        {
            "person_a": FakePerson("Theo", [], [OutfitTemplate(A_VECTOR, BODY_MODEL, 1000.0)]),
            "person_b": FakePerson("Ana", [], [OutfitTemplate(probe(0.99), BODY_MODEL, 1000.0)]),
        }
    )
    resolver = Resolver(roster)
    for step in range(6):
        resolver.observe_body("P1", body(1000.0 + step, probe(0.995)), 1000.0 + step)
    resolver.resolve([FakeTrack(last_seen=1006.0)], 1006.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


def test_the_body_calibration_runs_through_its_own_reject_and_accept():
    thresholds = BodyThresholds()
    assert thresholds.calibration.llr(thresholds.reject) < 0 < thresholds.calibration.llr(thresholds.accept)


def test_osnet_cosines_are_judged_on_osnet_thresholds():
    """OSNet sits far higher than a generic ReID space for the same error rate:
    0.65 is a match for one and a stranger for the other, and the only body
    embedder the robot actually runs is OSNet."""
    assert body_thresholds_for(BODY_MODEL).accept == 0.78
    assert body_thresholds_for(GENERIC_BODY_MODEL).accept == 0.60

    def state_after(model: str) -> IdentityState:
        resolver = Resolver(fresh_outfit_roster(model=model))
        for step in range(6):
            stamp = 1000.0 + step
            resolver.observe_body("P1", body(stamp, probe(0.65), model=model), stamp)
            resolver.resolve([FakeTrack(last_seen=stamp)], stamp)
        return resolver.identity("P1").state

    assert state_after(BODY_MODEL) is IdentityState.UNKNOWN
    assert state_after(GENERIC_BODY_MODEL) is IdentityState.POSSIBLE


def test_a_change_of_jacket_never_unseats_a_face_confirmed_name():
    """RFC 5.3.6: outfit is a weak cue — it cannot reach ``known`` on its own, so
    it must not be able to veto one either. Disagreement used to accumulate
    unbounded and either switch the track away or tear it apart."""
    resolver = Resolver(fresh_outfit_roster())
    for step in range(3):
        resolver.observe_face("P1", face(1000.0 + 0.6 * step, probe(0.50)))
    resolver.resolve([FakeTrack(last_seen=1001.2)], 1001.2)
    assert resolver.identity("P1").state is IdentityState.KNOWN

    split = False
    for step in range(60):
        stamp = 1002.0 + step
        resolver.observe_body("P1", body(stamp, B_VECTOR), stamp)
        split = split or resolver.resolve([FakeTrack(last_seen=stamp)], stamp)["P1"].split_requested
    assert not split
    assert resolver.identity("P1").person_id == "person_a"
    assert resolver.identity("P1").state is IdentityState.KNOWN


def test_outfits_of_a_different_model_are_never_compared():
    resolver = Resolver(fresh_outfit_roster(model="some-other-reid"))
    for step in range(6):
        resolver.observe_body("P1", body(1000.0 + step, probe(0.99)), 1000.0 + step)
    resolver.resolve([FakeTrack(last_seen=1006.0)], 1006.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


def test_a_body_observation_without_an_embedding_says_nothing():
    resolver = Resolver(fresh_outfit_roster())
    resolver.observe_body("P1", BodyObservation(1000.0, BOX, 300.0, 200.0, "", None), 1000.0)
    resolver.resolve([FakeTrack(last_seen=1000.0)], 1000.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


# ------------------------------------------------------------------- faces


def test_face_templates_of_a_different_model_are_never_compared():
    roster = FakeRoster(
        {"person_a": FakePerson("Theo", [FaceTemplate(A_VECTOR, "other-space-512", 0.0, "frontal", 1.0)])}
    )
    resolver = Resolver(roster)
    for step in range(5):
        resolver.observe_face("P1", face(100.0 + 0.5 * step, probe(1.0)))
    resolver.resolve([FakeTrack(last_seen=102.0)], 102.0)
    # A perfect cosine to a template in another space is invisible: the person
    # reads as a stranger, never as person_a.
    assert resolver.identity("P1").person_id != "person_a"


def test_a_face_observation_without_an_embedding_says_nothing():
    resolver = Resolver(roster_with())
    resolver.observe_face("P1", FaceObservation(100.0, BOX, 64.0, 64.0, 0.0, 0.0, 200.0, 130.0, 1.0, "", None))
    resolver.resolve([FakeTrack(last_seen=100.0)], 100.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


# --------------------------------------------------------------- continuity


def test_a_committed_identity_survives_a_rejecting_frame():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    resolver.observe_face("P1", face(end + 1.0, probe(0.05)))
    resolver.resolve([FakeTrack(last_seen=end + 1.0)], end + 1.0)
    assert resolver.identity("P1").state is IdentityState.KNOWN


def test_a_committed_identity_survives_frames_with_no_face_at_all():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    for step in range(20):
        resolver.resolve([FakeTrack(last_seen=end + step)], end + step)
    assert resolver.identity("P1").state is IdentityState.KNOWN


# --------------------------------------------------------------- hysteresis


def push_toward_b(resolver: Resolver, start: float, frames: int = 6) -> float:
    for step in range(frames):
        resolver.observe_face("P1", face(start + 0.4 * step, probe(0.20, 0.41)))
    return start + 0.4 * frames


def test_switching_needs_two_seconds_of_margin():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    pressure = push_toward_b(resolver, end + 1.0)
    resolver.resolve([FakeTrack(last_seen=pressure)], pressure)
    assert resolver.identity("P1").person_id == "person_a"
    resolver.resolve([FakeTrack(last_seen=pressure + 1.9)], pressure + 1.9)
    assert resolver.identity("P1").person_id == "person_a"


def test_switching_happens_once_the_margin_has_held():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    pressure = push_toward_b(resolver, end + 1.0)
    resolver.resolve([FakeTrack(last_seen=pressure)], pressure)
    resolutions = resolver.resolve([FakeTrack(last_seen=pressure + 2.1)], pressure + 2.1)
    assert resolver.identity("P1").person_id == "person_b"
    assert resolutions["P1"].switched_from == "person_a"


def test_pressure_that_fades_never_switches():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    pressure = push_toward_b(resolver, end + 1.0)
    resolver.resolve([FakeTrack(last_seen=pressure)], pressure)
    for step in range(4):  # the evidence swings back to A
        resolver.observe_face("P1", face(pressure + 0.4 * step, probe(0.50, 0.10)))
    resolver.resolve([FakeTrack(last_seen=pressure + 3.0)], pressure + 3.0)
    assert resolver.identity("P1").person_id == "person_a"


# ------------------------------------------------------ split and conflict


def test_a_face_frame_accepting_someone_else_splits_the_track():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    resolver.observe_face("P1", face(end + 0.5, probe(0.10, 0.55)))
    resolutions = resolver.resolve([FakeTrack(last_seen=end + 0.5)], end + 0.5)
    assert resolutions["P1"].split_requested


def test_a_split_is_requested_once_not_every_tick():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    resolver.observe_face("P1", face(end + 0.5, probe(0.10, 0.55)))
    resolver.resolve([FakeTrack(last_seen=end + 0.5)], end + 0.5)
    again = resolver.resolve([FakeTrack(last_seen=end + 0.7)], end + 0.7)
    assert not again["P1"].split_requested


def test_an_ambiguous_frame_does_not_split_a_committed_track():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    resolver.observe_face("P1", face(end + 0.5, probe(0.44, 0.45)))
    resolutions = resolver.resolve([FakeTrack(last_seen=end + 0.5)], end + 0.5)
    assert not resolutions["P1"].split_requested


def test_the_face_that_split_a_track_is_never_written_onto_the_person_it_left():
    """RFC 5.3.4: the frame that accepts B on a track committed to A is B's.
    Learning from it would put B's embedding in A's gallery, which is exactly
    the crossing the split exists to repair."""
    roster = roster_with()
    resolver = Resolver(roster)
    end = commit_theo(resolver)
    roster.templates.clear()

    resolver.observe_face("P1", face(end + 6.0, probe(0.10, 0.55)))
    resolutions = resolver.resolve([FakeTrack(last_seen=end + 6.0)], end + 6.0)

    assert resolutions["P1"].split_requested
    assert roster.templates == []


def test_a_stranger_the_tracker_walked_in_is_never_written_into_the_gallery():
    """RFC 5.3.4 the other way round: the crossing is with somebody the roster
    has never seen, so no entry claims the face and the track stays committed to
    the person it was following. Learning from it would put a stranger's face —
    and their clothes — in that person's gallery."""
    roster = roster_with()
    resolver = Resolver(roster)
    end = commit_theo(resolver)
    resolver.observe_body("P1", body(end + 0.5, probe(0.0)), end + 0.5)
    roster.templates.clear()
    roster.written_outfits.clear()

    for step in range(6):
        stamp = end + 6.0 + 0.4 * step
        resolver.observe_face("P1", face(stamp, probe(0.0), box=_moved(step)))
        resolver.resolve([FakeTrack(last_seen=stamp)], stamp)

    assert resolver.identity("P1").person_id == "person_a"  # nothing has named the stranger
    assert roster.templates == []
    assert roster.written_outfits == []


def test_sustained_frames_that_belong_to_nobody_split_a_committed_track():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    requested = False
    for step in range(20):
        stamp = end + 1.0 + 0.4 * step
        resolver.observe_face("P1", face(stamp, probe(0.0), box=_moved(step)))
        requested = requested or resolver.resolve([FakeTrack(last_seen=stamp)], stamp)["P1"].split_requested
    assert requested


def test_one_bad_angle_on_the_person_themselves_never_splits_the_track():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    resolver.observe_face("P1", face(end + 0.5, probe(0.15)))
    for step in range(10):
        stamp = end + 1.0 + 0.5 * step
        assert not resolver.resolve([FakeTrack(last_seen=stamp)], stamp)["P1"].split_requested


def test_applying_a_split_drops_everything_learned_on_both_tags():
    resolver = Resolver(roster_with())
    commit_theo(resolver)
    resolver.apply_split("P1", "P2")
    assert resolver.identity("P1").person_id is None
    assert resolver.identity("P2").person_id is None


def test_two_live_tracks_cannot_both_be_the_same_person():
    resolver = Resolver(roster_with())
    for tag in ("P1", "P2"):
        for step in range(3):
            resolver.observe_face(tag, face(100.0 + 0.6 * step, probe(0.50)))
    tracks = [FakeTrack("P1", 90.0, 101.2), FakeTrack("P2", 95.0, 101.2)]
    resolutions = resolver.resolve(tracks, 101.2)
    assert resolutions["P1"].identity.state is IdentityState.KNOWN
    assert resolutions["P2"].identity.state is IdentityState.POSSIBLE
    assert resolutions["P2"].conflict_with == "P1"


def test_a_split_survives_losing_a_conflict_on_the_same_tick():
    """The two rules fire on one tick: P1's frames say the tracker swapped
    somebody in, and P2 claims the person first. Rebuilding P1's resolution for
    the conflict must not drop the split, or the crossed track never gets fixed."""
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    for step in range(3):  # P2 walks in and claims the same person
        resolver.observe_face("P2", face(end + 0.6 * step, probe(0.50)))
    resolver.observe_face("P1", face(end + 1.5, probe(0.10, 0.55)))
    stamp = end + 1.5
    resolutions = resolver.resolve([FakeTrack("P1", 90.0, stamp), FakeTrack("P2", 80.0, stamp)], stamp)
    assert resolutions["P1"].conflict_with == "P2"
    assert resolutions["P1"].split_requested


def test_the_older_track_keeps_the_name_in_a_conflict():
    resolver = Resolver(roster_with())
    for tag in ("P1", "P2"):
        for step in range(3):
            resolver.observe_face(tag, face(100.0 + 0.6 * step, probe(0.50)))
    resolutions = resolver.resolve([FakeTrack("P1", 99.0, 101.2), FakeTrack("P2", 80.0, 101.2)], 101.2)
    assert resolutions["P2"].identity.state is IdentityState.KNOWN
    assert resolutions["P1"].conflict_with == "P2"


# --------------------------------------------------------------- enrolment


def enrol_frames(resolver: Resolver, tag: str = "P1", *, count: int = 5, span: float = 2.4, start: float = 100.0):
    step = span / max(1, count - 1)
    for index in range(count):
        resolver.observe_face(tag, face(start + step * index, probe(0.98), box=_moved(index)))


def _moved(index: int):
    ymin, xmin, ymax, xmax = BOX
    return (ymin, xmin + 0.02 * index, ymax, xmax + 0.02 * index)


def test_five_self_consistent_frames_over_two_seconds_enrol_a_new_person():
    roster = FakeRoster()
    resolver = Resolver(roster)
    enrol_frames(resolver)
    resolutions = resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    assert len(roster.created) == 1
    assert resolutions["P1"].enrolled_id is not None
    assert resolver.identity("P1").state is IdentityState.FAMILIAR


def test_four_frames_are_not_enough_to_enrol():
    roster = FakeRoster()
    resolver = Resolver(roster)
    enrol_frames(resolver, count=4)
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    assert roster.created == []


def test_five_frames_inside_two_seconds_are_not_enough_to_enrol():
    roster = FakeRoster()
    resolver = Resolver(roster)
    enrol_frames(resolver, span=1.5)
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=101.5)], 101.5)
    assert roster.created == []


def test_frames_arriving_faster_than_the_span_still_enrol():
    """The window is the buffer, not its last five frames: at four frames a
    second the last five span one, and a person standing in front of the robot
    would never enrol however long they looked at it."""
    roster = FakeRoster()
    resolver = Resolver(roster)
    for index in range(24):
        stamp = 100.0 + 0.25 * index
        resolver.observe_face("P1", face(stamp, probe(0.98), box=_moved(index % 5)))
        resolver.resolve([FakeTrack(first_seen=99.0, last_seen=stamp)], stamp)
    assert len(roster.created) == 1
    assert 5 < len(roster.created[0]) <= 10  # RFC 5.4: up to ten, not only the five that decided


def test_frames_at_the_five_hertz_face_cadence_enrol_once_they_span_two_seconds():
    """The engine's ceiling is five face frames a second, so a ten-frame buffer
    spans 1.8 s and the two-second enrol window is never reachable on hardware."""
    roster = FakeRoster()
    resolver = Resolver(roster)
    for index in range(15):
        stamp = 100.0 + 0.2 * index
        resolver.observe_face("P1", face(stamp, probe(0.98), box=_moved(index % 5)))
        resolver.resolve([FakeTrack(first_seen=97.0, last_seen=stamp)], stamp)
    assert len(roster.created) == 1


def test_one_stray_frame_above_reject_does_not_stop_a_later_enrolment():
    """RFC 5.4 asks the enrolling frames to match nobody, and the per-frame gate
    on ``enrol_faces`` is what enforces it. A lifetime maximum over hundreds of
    impostor comparisons a second crosses reject on any track sooner or later,
    and would then block that person for the rest of their visit."""
    roster = FakeRoster({"person_a": FakePerson("Theo", [FaceTemplate(A_VECTOR, MODEL, 0.0, "frontal", 1.0)])})
    resolver = Resolver(roster)
    resolver.observe_face("P1", face(99.0, probe(0.35)))  # above reject, below accept
    for index in range(5):
        resolver.observe_face("P1", face(100.0 + 0.6 * index, probe(0.05), box=_moved(index)))
    resolver.resolve([FakeTrack(first_seen=95.0, last_seen=102.4)], 102.4)
    assert len(roster.created) == 1


def test_one_odd_crop_among_the_oldest_frames_does_not_block_a_long_buffer():
    """Twenty-five frames are five seconds of natural head motion; judged all
    at once, one turned-away crop at the start would hold enrolment for the
    whole buffer, and the gallery would be the ten oldest crops."""
    roster = FakeRoster()
    resolver = Resolver(roster)
    resolver.observe_face("P1", face(100.0, B_VECTOR))
    for index in range(1, 25):
        resolver.observe_face("P1", face(100.0 + 0.2 * index, probe(0.05), box=_moved(index)))
    resolver.resolve([FakeTrack(first_seen=95.0, last_seen=104.8)], 104.8)
    assert len(roster.created) == 1
    assert len(roster.created[0]) == 10


def test_a_passer_by_seen_for_under_two_seconds_never_enrols():
    roster = FakeRoster()
    resolver = Resolver(roster)
    enrol_frames(resolver)
    resolver.resolve([FakeTrack(first_seen=101.0, last_seen=102.4)], 102.4)
    assert roster.created == []


def test_frames_that_disagree_with_each_other_never_enrol():
    roster = FakeRoster()
    resolver = Resolver(roster)
    for index in range(5):
        vector = probe(0.98) if index % 2 else probe(0.0, 0.98)
        resolver.observe_face("P1", face(100.0 + 0.6 * index, vector, box=_moved(index)))
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    assert roster.created == []


def test_a_face_that_matches_the_roster_is_never_enrolled_as_somebody_new():
    roster = roster_with()
    resolver = Resolver(roster)
    for index in range(6):
        resolver.observe_face("P1", face(100.0 + 0.5 * index, probe(0.35), box=_moved(index)))
    resolver.resolve([FakeTrack(first_seen=90.0, last_seen=103.0)], 103.0)
    assert roster.created == []


def test_a_full_roster_stops_enrolling():
    roster = FakeRoster(capacity=False)
    resolver = Resolver(roster)
    enrol_frames(resolver)
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    assert roster.created == []


class RacedRoster(FakeRoster):
    """``can_enrol`` said yes and the write lost the race — the empty id
    :meth:`RosterView.create_unnamed` documents."""

    def create_unnamed(self, faces: list[FaceTemplate], thumbnail: bytes | None, now: float) -> str:
        del faces, thumbnail, now
        return ""


def test_an_enrolment_that_lost_the_capacity_race_commits_to_nobody():
    roster = RacedRoster()
    resolver = Resolver(roster)
    enrol_frames(resolver)
    resolutions = resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    assert resolutions["P1"].enrolled_id is None
    assert resolver.identity("P1").person_id is None
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


def test_collection_turned_off_stops_enrolling():
    roster = FakeRoster(collection=False)
    resolver = Resolver(roster)
    enrol_frames(resolver)
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    assert roster.created == []


def test_a_turned_head_is_never_an_enrolment_frame():
    roster = FakeRoster()
    resolver = Resolver(roster)
    for index in range(6):
        resolver.observe_face("P1", face(100.0 + 0.6 * index, probe(0.98), yaw=40.0, box=_moved(index)))
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=103.0)], 103.0)
    assert roster.created == []


def test_a_small_face_is_never_an_enrolment_frame():
    roster = FakeRoster()
    resolver = Resolver(roster)
    for index in range(6):
        resolver.observe_face("P1", face(100.0 + 0.6 * index, probe(0.98), size_px=44.0, box=_moved(index)))
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=103.0)], 103.0)
    assert roster.created == []


# ---------------------------------------------------------------- learning


def test_a_confirmation_writes_a_template_and_todays_outfit():
    roster = roster_with()
    resolver = Resolver(roster)
    resolver.observe_body("P1", body(99.0, probe(0.0)), 99.0)
    commit_theo(resolver)
    assert [pid for pid, _ in roster.templates] == ["person_a"]
    assert [pid for pid, _ in roster.written_outfits] == ["person_a"]


def test_templates_are_rate_limited_rather_than_written_every_frame():
    roster = roster_with()
    resolver = Resolver(roster)
    end = commit_theo(resolver)
    for step in range(1, 6):
        resolver.observe_face("P1", face(end + 0.5 * step, probe(0.60), box=_moved(step)))
        resolver.resolve([FakeTrack(last_seen=end + 0.5 * step)], end + 0.5 * step)
    assert len(roster.templates) == 1


def test_a_confirmation_records_the_sighting():
    roster = roster_with()
    resolver = Resolver(roster)
    commit_theo(resolver)
    assert [pid for pid, _ in roster.sightings] == ["person_a"]


def test_height_samples_are_written_while_a_track_is_committed():
    roster = roster_with()
    resolver = Resolver(roster)
    resolver.observe_height("P1", 1.72, 0.01)
    commit_theo(resolver)
    assert roster.heights and roster.heights[0][0] == "person_a"


def test_a_height_that_stopped_being_measured_is_not_rewritten():
    """The store keeps the last 64 samples, so a long encounter re-writing its
    own running mean every second evicts every other encounter's."""
    roster = roster_with()
    resolver = Resolver(roster)
    resolver.observe_height("P1", 1.72, 0.01)
    end = commit_theo(resolver)
    for step in range(1, 120):
        resolver.resolve([FakeTrack(last_seen=end + step)], end + step)
    assert len(roster.heights) == 1


def test_a_height_measured_every_second_reaches_the_store_twice_a_minute():
    """The engine measures at 1 Hz; written at that rate, one two-minute
    encounter would fill the store's 64 samples with its own running mean."""
    roster = roster_with()
    resolver = Resolver(roster)
    resolver.observe_height("P1", 1.72, 0.01)
    end = commit_theo(resolver)
    for step in range(1, 120):
        resolver.observe_height("P1", 1.72 + 0.0001 * step, 0.01)
        resolver.resolve([FakeTrack(last_seen=end + step)], end + step)
    assert 1 <= len(roster.heights) <= 5


def test_nothing_is_learned_while_collection_is_off():
    roster = roster_with()
    roster._collection = False
    resolver = Resolver(roster)
    resolver.observe_height("P1", 1.72, 0.01)
    commit_theo(resolver)
    assert roster.templates == []
    assert roster.written_outfits == []
    assert roster.heights == []


def test_recognising_someone_with_collection_off_still_records_the_sighting():
    """RFC section 10: collection off is a collection control, not a deletion.
    Freezing last_seen while the robot recognises the person daily hands them to
    the store's fourteen-day expiry."""
    roster = roster_with()
    roster._collection = False
    resolver = Resolver(roster)
    commit_theo(resolver)
    assert [pid for pid, _ in roster.sightings] == ["person_a"]


def test_nothing_is_learned_on_the_track_that_lost_a_conflict():
    roster = roster_with()
    resolver = Resolver(roster)
    for tag in ("P1", "P2"):
        for step in range(3):
            resolver.observe_face(tag, face(100.0 + 0.6 * step, probe(0.50), box=_moved(step)))
    resolver.resolve([FakeTrack("P1", 90.0, 101.2), FakeTrack("P2", 95.0, 101.2)], 101.2)
    assert [pid for pid, _ in roster.sightings] == ["person_a"]  # written once, by P1


# --------------------------------------------------------------- height


def test_height_agreeing_with_the_roster_is_a_tie_breaker_not_a_decision():
    roster = roster_with()
    roster.people["person_a"].height = HeightEstimate(1.72, 0.0009, 20)
    resolver = Resolver(roster)
    for _ in range(10):
        resolver.observe_height("P1", 1.72, 0.0025)
    resolver.resolve([FakeTrack(last_seen=100.0)], 100.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


def test_a_matching_height_helps_and_a_mismatched_one_hurts():
    def confidence(measured: float) -> float:
        roster = roster_with(b_name=None)
        roster.people["person_a"].height = HeightEstimate(1.72, 0.0009, 20)
        resolver = Resolver(roster)
        for step in range(2):
            resolver.observe_face("P1", face(100.0 + 0.6 * step, probe(0.45), quality=0.5))
        resolver.observe_height("P1", measured, 0.0025)
        resolver.resolve([FakeTrack(last_seen=101.2)], 101.2)
        return resolver.identity("P1").confidence

    assert confidence(1.72) > confidence(1.40)


# ------------------------------------------------------------ re-association


def test_a_reassociated_track_resumes_at_possible():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    resolver.on_reassociated("P1", end + 300.0)
    resolver.resolve([FakeTrack(last_seen=end + 300.0)], end + 300.0)
    identity = resolver.identity("P1")
    assert identity.state is IdentityState.POSSIBLE
    assert identity.person_id == "person_a"


def test_one_accepting_face_frame_brings_a_resumed_track_back_to_known():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    resolver.on_reassociated("P1", end + 300.0)
    resolver.resolve([FakeTrack(last_seen=end + 300.0)], end + 300.0)
    resolver.observe_face("P1", face(end + 301.0, probe(0.50)))
    resolver.resolve([FakeTrack(last_seen=end + 301.0)], end + 301.0)
    assert resolver.identity("P1").state is IdentityState.KNOWN


def test_agreeing_outfit_for_three_seconds_brings_a_resumed_track_back():
    roster = fresh_outfit_roster()
    resolver = Resolver(roster)
    for step in range(3):
        resolver.observe_face("P1", face(1000.0 + 0.6 * step, probe(0.50)))
    resolver.resolve([FakeTrack(last_seen=1001.2)], 1001.2)
    resolver.on_reassociated("P1", 1002.0)
    for step in range(5):
        stamp = 1003.0 + step
        resolver.observe_body("P1", body(stamp, probe(0.95)), stamp)
        resolver.resolve([FakeTrack(last_seen=stamp)], stamp)
    assert resolver.identity("P1").state is IdentityState.KNOWN


def test_an_outfit_that_agreed_before_the_gap_does_not_stand_in_for_one_after_it():
    """Reconfirmation wants three seconds of agreement since the track came back.
    A stamp left over from before the gap is never replaced while the frames keep
    agreeing, so the track would sit at ``possible`` for ever."""
    roster = fresh_outfit_roster()
    resolver = Resolver(roster)
    for step in range(3):
        resolver.observe_face("P1", face(1000.0 + 0.6 * step, probe(0.50)))
    resolver.resolve([FakeTrack(last_seen=1001.2)], 1001.2)
    for step in range(2):
        stamp = 1002.0 + step
        resolver.observe_body("P1", body(stamp, probe(0.95)), stamp)
        resolver.resolve([FakeTrack(last_seen=stamp)], stamp)
    assert resolver.identity("P1").state is IdentityState.KNOWN

    resolver.on_reassociated("P1", 1100.0)
    for step in range(5):
        stamp = 1101.0 + step
        resolver.observe_body("P1", body(stamp, probe(0.95)), stamp)
        resolver.resolve([FakeTrack(last_seen=stamp)], stamp)
    assert resolver.identity("P1").state is IdentityState.KNOWN


def test_reassociating_an_uncommitted_track_changes_nothing():
    resolver = Resolver(roster_with())
    resolver.on_reassociated("P1", 100.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN


# ---------------------------------------------------------------- lifecycle


def test_forgetting_a_tag_drops_its_belief():
    resolver = Resolver(roster_with())
    commit_theo(resolver)
    resolver.forget("P1")
    assert resolver.identity("P1").person_id is None


def test_a_forgotten_track_is_suppressed_and_never_re_enrols():
    """RFC section 8: forget_person "suppresses the live track". Without that,
    the person who just asked to be forgotten walks straight back into the
    roster as a new id, because the track is still standing there enrolling."""
    roster = FakeRoster()
    resolver = Resolver(roster)
    enrol_frames(resolver)
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    assert len(roster.created) == 1

    roster.people.clear()  # what PeopleStore.forget did to the record
    resolver.forget("P1")

    enrol_frames(resolver, start=110.0)
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=112.4)], 112.4)
    assert len(roster.created) == 1
    assert resolver.identity("P1").person_id is None


def test_a_merge_moves_a_live_track_onto_the_id_that_survived():
    """RFC section 8: merge_people tombstones the source id, so a track still
    committed to it would publish an id the store no longer has and learn onto
    nothing."""
    roster = roster_with(a_name=None)
    resolver = Resolver(roster)
    end = commit_theo(resolver)
    assert resolver.identity("P1").person_id == "person_a"

    roster.people["person_b"].faces.extend(roster.people.pop("person_a").faces)  # what store.merge does
    resolver.rebind("person_a", "person_b")

    identity = resolver.identity("P1")
    assert identity.person_id == "person_b"
    assert identity.name == "Ana"
    assert identity.state is IdentityState.KNOWN

    resolver.observe_face("P1", face(end + 6.0, probe(0.50)))
    resolver.resolve([FakeTrack(last_seen=end + 6.0)], end + 6.0)
    assert roster.templates[-1][0] == "person_b"


def test_the_suppression_dies_with_the_track_it_was_set_on():
    """A tag is never reused, so the next person to carry one enrols normally."""
    roster = FakeRoster()
    resolver = Resolver(roster)
    resolver.forget("P1")
    resolver.resolve([FakeTrack("P2", 0.0, 0.0)], 0.0)  # P1 is gone from the tracker

    enrol_frames(resolver)
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    assert len(roster.created) == 1


def test_resolve_drops_the_beliefs_of_tracks_that_are_gone():
    resolver = Resolver(roster_with())
    commit_theo(resolver)
    resolver.resolve([FakeTrack("P2", 200.0, 200.0)], 200.0)
    assert resolver.identity("P1").person_id is None


def test_a_lost_track_is_not_resolved_but_keeps_its_belief():
    resolver = Resolver(roster_with())
    end = commit_theo(resolver)
    resolutions = resolver.resolve([FakeTrack("P1", 90.0, end, lost=True)], end + 1.0)
    assert resolutions == {}
    assert resolver.identity("P1").person_id == "person_a"


def test_the_new_person_hypothesis_is_never_offered_as_an_identity():
    roster = FakeRoster()
    resolver = Resolver(roster)
    enrol_frames(resolver, count=3)
    resolver.resolve([FakeTrack(first_seen=99.0, last_seen=102.4)], 102.4)
    identity = resolver.identity("P1")
    assert identity.person_id != NEW_PERSON
    assert identity.runner_up_id != NEW_PERSON


def test_a_custom_config_moves_the_accept_line():
    resolver = Resolver(roster_with(), config=ResolverConfig(accept_log_odds=100.0))
    commit_theo(resolver)
    assert resolver.identity("P1").state is not IdentityState.KNOWN


def test_custom_body_thresholds_are_honoured():
    resolver = Resolver(fresh_outfit_roster(), body=BodyThresholds(accept=0.99, reject=0.98, margin=0.01))
    resolver.observe_body("P1", body(1000.0, probe(0.60)), 1000.0)
    resolver.resolve([FakeTrack(last_seen=1000.0)], 1000.0)
    assert resolver.identity("P1").state is IdentityState.UNKNOWN
