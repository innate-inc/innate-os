# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The engine end to end on synthetic frames and fake backends.

Frames are painted with cv2: a person-shaped blob with a textured face, so the
sharpness and luminance gates see something real. The models are the fakes from
``people.backends`` — a scripted detector, a centre-of-the-crop face locator and
a mean-colour embedder — so these tests exercise the pipeline and the duty
cycle, never a model's accuracy.
"""

import urllib.request
from pathlib import Path

import cv2
import numpy as np
import pytest
from people_fakes import (
    CenterFaceLocator,
    ColorBodyEmbedder,
    ColorFaceEmbedder,
    fake_backends,
    mean_color_embedding,
)

from brain_client.people import native_frames
from brain_client.people.backends import (
    YUNET,
    Backend,
    Backends,
    HogPersonDetector,
    NullBodyEmbedder,
    ensure_model,
    load_backends,
)
from brain_client.people.engine import EngineConfig, PeopleEngine
from brain_client.people.geometry import CameraModel, head_region
from brain_client.people.quality import FACE_MIN_MATCH_REAL_PX, EgoMotion
from brain_client.people.resolve import Resolution, Resolver
from brain_client.people.surfacing import FACE_STALE_SEC
from brain_client.people.types import (
    Detection,
    FaceObservation,
    FaceTemplate,
    HealthDict,
    HealthState,
    IdentityState,
    Pose,
)

FRAME_SIZE = (480, 640)
NATIVE_SIZE = (720, 2560)
PERSON = (0.20, 0.36, 0.86, 0.52)
FAR_PERSON = (0.30, 0.44, 0.452, 0.478)  # far enough that the face is 42 native-equivalent pixels


class FakeRoster:
    """Permissive and recording, and it does hand back the people it enrolled —
    a roster that forgets them reads every later frame as another stranger. The
    resolver's own suite owns the rules."""

    def __init__(self, *, collection: bool = True) -> None:
        self.collection = collection
        self.created: list[list[FaceTemplate]] = []
        self.people: dict[str, list[FaceTemplate]] = {}

    def person_ids(self) -> list[str]:
        return list(self.people)

    def name_of(self, person_id: str) -> str | None:
        del person_id
        return None

    def face_templates(self, person_id: str, model: str) -> list[FaceTemplate]:
        return [template for template in self.people.get(person_id, ()) if template.model == model]

    def outfits(self, person_id: str, model: str, now: float) -> list:
        del person_id, model, now
        return []

    def height(self, person_id: str):
        del person_id
        return None

    def collection_enabled(self) -> bool:
        return self.collection

    def can_enrol(self) -> bool:
        return True

    def create_unnamed(self, faces: list[FaceTemplate], thumbnail: bytes | None, now: float) -> str:
        del thumbnail, now
        self.created.append(list(faces))
        person_id = f"person_new{len(self.created)}"
        self.people[person_id] = list(faces)
        return person_id

    def add_face_template(self, person_id: str, template: FaceTemplate, thumbnail: bytes | None) -> None:
        del thumbnail
        self.people.setdefault(person_id, []).append(template)

    def add_outfit(self, person_id: str, outfit) -> None:
        del person_id, outfit

    def add_height_sample(self, person_id: str, height_m: float, variance: float) -> None:
        del person_id, height_m, variance

    def record_sighting(self, person_id: str, now: float, map_name: str | None, pose: Pose | None) -> None:
        del person_id, now, map_name, pose


class RecordingDetector:
    """A scripted detector that remembers the shape of every frame it saw."""

    name = "recording"

    def __init__(self, boxes: list[list[tuple]] | None = None) -> None:
        self.boxes = boxes or []
        self.shapes: list[tuple[int, int]] = []

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        index = min(len(self.shapes), len(self.boxes) - 1) if self.boxes else -1
        self.shapes.append(frame_bgr.shape[:2])
        return [Detection(box=box, score=0.9) for box in self.boxes[index]] if index >= 0 else []

    @property
    def calls(self) -> int:
        return len(self.shapes)


class RecordingLocator(CenterFaceLocator):
    """CenterFaceLocator that remembers how many pixels of face it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.crop_heights: list[int] = []

    def locate(self, crop_bgr: np.ndarray):
        self.crop_heights.append(crop_bgr.shape[0])
        return super().locate(crop_bgr)


class OnceLocator(CenterFaceLocator):
    """A face on the first crop it is handed and never again — the head turned
    away, which is what the HOG detector alone reports for ever after."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def locate(self, crop_bgr: np.ndarray):
        self.calls += 1
        return super().locate(crop_bgr) if self.calls == 1 else []


class RecordingResolver(Resolver):
    """Keeps every face observation the engine judged worth handing over."""

    def __init__(self, roster) -> None:
        super().__init__(roster)
        self.faces: list[FaceObservation] = []

    def observe_face(self, tag: str, observation: FaceObservation) -> None:
        self.faces.append(observation)
        super().observe_face(tag, observation)


class CountingResolver(Resolver):
    """Records every tag the engine asked it to resume after a re-association."""

    def __init__(self, roster) -> None:
        super().__init__(roster)
        self.resumed: list[str] = []

    def on_reassociated(self, tag: str, now: float) -> None:
        self.resumed.append(tag)
        super().on_reassociated(tag, now)


class StillRecordingResolver(Resolver):
    """Remembers what each tick told it about the robot holding still."""

    def __init__(self, roster) -> None:
        super().__init__(roster)
        self.stillness: list[bool] = []

    def resolve(self, tracks, now, *, still=True, map_name=None, pose=None):
        self.stillness.append(still)
        return super().resolve(tracks, now, still=still, map_name=map_name, pose=pose)


class SplitOnceResolver(Resolver):
    """Asks the engine to split the first track it ever resolves."""

    def __init__(self, roster) -> None:
        super().__init__(roster)
        self.fired = False

    def resolve(self, tracks, now, *, still=True, map_name=None, pose=None):
        resolutions = super().resolve(tracks, now, still=still, map_name=map_name, pose=pose)
        if not self.fired and resolutions:
            self.fired = True
            tag = next(iter(resolutions))
            resolutions[tag] = Resolution(tag=tag, identity=resolutions[tag].identity, split_requested=True)
        return resolutions


def paint_person(frame: np.ndarray, box: tuple) -> np.ndarray:
    """A body slab with a textured face, so the crop has edges and mid exposure."""
    height, width = frame.shape[:2]
    ymin, xmin, ymax, xmax = box
    y0, y1 = int(ymin * height), int(ymax * height)
    x0, x1 = int(xmin * width), int(xmax * width)
    cv2.rectangle(frame, (x0, y0), (x1, y1), (140, 120, 170), -1)
    head_bottom = y0 + int(0.28 * (y1 - y0))
    cv2.rectangle(frame, (x0, y0), (x1, head_bottom), (170, 165, 195), -1)
    eye_y = y0 + int(0.35 * (head_bottom - y0))
    span = max(2, (x1 - x0) // 8)
    cv2.rectangle(frame, (x0 + span, eye_y), (x0 + 2 * span, eye_y + span), (40, 40, 50), -1)
    cv2.rectangle(frame, (x1 - 2 * span, eye_y), (x1 - span, eye_y + span), (40, 40, 50), -1)
    mouth_y = y0 + int(0.72 * (head_bottom - y0))
    cv2.rectangle(frame, (x0 + span, mouth_y), (x1 - span, mouth_y + span // 2), (60, 50, 80), -1)
    return frame


def scene(box: tuple = PERSON, size: tuple = FRAME_SIZE) -> np.ndarray:
    frame = np.full((size[0], size[1], 3), 95, np.uint8)
    return paint_person(frame, box)


def native_jpeg(box: tuple = PERSON) -> bytes:
    """The sensor's own 2560x720 buffer: both eyes, unrotated, the published
    left eye living upside down in the right half."""
    buffer = np.full((NATIVE_SIZE[0], NATIVE_SIZE[1], 3), 95, np.uint8)
    left = paint_person(np.full((720, 1280, 3), 95, np.uint8), box)
    buffer[:, 1280:] = left[::-1, ::-1]
    ok, encoded = cv2.imencode(".jpg", buffer)
    assert ok
    return bytes(encoded)


def shifted(box: tuple, dx: float) -> tuple:
    ymin, xmin, ymax, xmax = box
    return (ymin, xmin + dx, ymax, xmax + dx)


def build(
    *,
    boxes: list[list[tuple]] | None = None,
    roster: FakeRoster | None = None,
    config: EngineConfig | None = None,
    locator=None,
    resolver=None,
) -> tuple[PeopleEngine, RecordingDetector, FakeRoster]:
    detector = RecordingDetector(boxes or [[PERSON]])
    store = roster or FakeRoster()
    backends = Backends(
        detector=detector,
        locator=locator or CenterFaceLocator(),
        face=ColorFaceEmbedder(),
        body=ColorBodyEmbedder(),
        health=HealthDict(face_model=str(HealthState.OK), body_model=str(HealthState.OK), gpu=str(HealthState.NONE)),
    )
    engine = PeopleEngine(backends, store, config=config, resolver=resolver)
    return engine, detector, store


def still(now: float) -> EgoMotion:
    return EgoMotion.still_at(now)


# ------------------------------------------------------------------- frames


def test_a_tick_without_a_frame_keeps_the_last_states():
    engine, _detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    assert engine.tick(None, None, 100.2, still(100.2))[0].tag == "P1"


def test_detection_runs_on_the_un_squashed_frame():
    engine, detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    assert detector.shapes == [(360, 640)]


def test_a_detected_person_becomes_a_tagged_track():
    engine, _detector, _roster = build()
    states = engine.tick(scene(), None, 100.0, still(100.0))
    assert [s.tag for s in states] == ["P1"]
    assert states[0].identity.state is IdentityState.UNKNOWN


def test_an_empty_scene_produces_no_tracks():
    engine, _detector, _roster = build(boxes=[[]])
    assert engine.tick(scene(), None, 100.0, still(100.0)) == []


# --------------------------------------------------------------- duty cycle


def test_with_nobody_around_the_detector_runs_at_half_a_hertz():
    engine, detector, _roster = build(boxes=[[]])
    engine.tick(scene(), None, 100.0, still(100.0))
    engine.tick(scene(), None, 101.0, still(101.0))
    assert detector.calls == 1
    engine.tick(scene(), None, 102.5, still(102.5))
    assert detector.calls == 2


def test_a_live_track_raises_the_detector_to_five_hertz():
    engine, detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    engine.tick(scene(), None, 100.25, still(100.25))
    assert detector.calls == 2


def test_the_motion_gate_raises_the_rate_before_anyone_is_tracked():
    engine, detector, _roster = build(boxes=[[]])
    engine.tick(scene(), None, 100.0, still(100.0), motion=True)
    engine.tick(scene(), None, 100.3, still(100.3))
    assert detector.calls == 2


def test_the_motion_burst_expires():
    engine, detector, _roster = build(boxes=[[]], config=EngineConfig(motion_burst_sec=1.0))
    engine.tick(scene(), None, 100.0, still(100.0), motion=True)
    engine.tick(scene(), None, 101.5, still(101.5))
    engine.tick(scene(), None, 102.0, still(102.0))
    assert detector.calls == 2  # the third tick is back on the idle clock


def test_driving_drops_the_detector_to_two_hertz():
    engine, detector, _roster = build()
    driving = EgoMotion(stamp=100.0, last_drive=100.0)
    engine.tick(scene(), None, 100.0, driving)
    engine.tick(scene(), None, 100.25, EgoMotion(stamp=100.25, last_drive=100.0))
    assert detector.calls == 1
    engine.tick(scene(), None, 100.6, EgoMotion(stamp=100.6, last_drive=100.0))
    assert detector.calls == 2


def test_the_engine_throttles_a_caller_that_ticks_at_frame_rate():
    engine, detector, _roster = build()
    for step in range(15):
        engine.tick(scene(), None, 100.0 + step / 15.0, still(100.0 + step / 15.0))
    assert detector.calls <= 6  # 5 Hz over one second, plus the opening tick


def test_the_frame_stamp_names_the_frame_the_boxes_were_measured_on():
    """RFC section 7: the brain draws only on the frame the engine measured, and
    it finds it by this stamp. A tick the duty cycle skipped must keep the stamp
    of the frame the boxes still come from, or a name lands on a newer picture."""
    engine, detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0), frame_stamp_ns="1000")
    assert engine.frame_stamp_ns == "1000"

    engine.tick(scene(), None, 100.05, still(100.05), frame_stamp_ns="2000")
    assert detector.calls == 1  # throttled: nothing was measured on frame 2000
    assert engine.frame_stamp_ns == "1000"

    engine.tick(scene(), None, 100.3, still(100.3), frame_stamp_ns="3000")
    assert engine.frame_stamp_ns == "3000"


def test_the_frame_stamp_is_empty_until_a_frame_has_been_through_detection():
    engine, _detector, _roster = build()
    assert engine.frame_stamp_ns is None
    engine.tick(None, None, 100.0, still(100.0), frame_stamp_ns="1000")
    assert engine.frame_stamp_ns is None


# ------------------------------------------------------------- ego-motion


def test_no_face_evidence_is_gathered_while_the_robot_drives():
    engine, _detector, _roster = build()
    driving = EgoMotion(stamp=100.0, last_drive=100.0)
    states = engine.tick(scene(), None, 100.0, driving)
    assert states[0].frames_with_face == 0


def test_face_evidence_is_gathered_once_the_robot_holds_still():
    engine, _detector, _roster = build()
    states = engine.tick(scene(), None, 100.0, still(100.0))
    assert states[0].frames_with_face == 1
    assert states[0].last_face_stamp == 100.0


def test_a_moving_head_also_stops_the_evidence():
    engine, _detector, _roster = build()
    ego = EgoMotion(stamp=100.0, head_pitch_range_deg=2.0)
    assert engine.tick(scene(), None, 100.0, ego)[0].frames_with_face == 0


def test_the_resolver_is_told_which_ticks_gathered_no_evidence():
    """The switch hysteresis is spent in evidence, and a tick that skipped the
    gathering supplied none: the resolver can only pause its timer if the engine
    passes its own stillness through."""
    roster = FakeRoster()
    resolver = StillRecordingResolver(roster)
    engine, _detector, _roster = build(roster=roster, resolver=resolver)
    engine.tick(scene(), None, 100.0, still(100.0))
    engine.tick(scene(), None, 101.0, EgoMotion(stamp=101.0, last_drive=101.0))
    assert resolver.stillness == [True, False]


# ---------------------------------------------------------------- geometry


def test_range_bearing_and_height_are_measured_from_the_box():
    engine, _detector, _roster = build()
    state = engine.tick(scene(), None, 100.0, still(100.0))[0]
    assert state.range_m is not None and 0.2 < state.range_m < 3.5
    assert state.bearing_deg is not None
    assert state.height_m is not None and state.height_m > 0.0


def test_a_person_left_of_centre_reads_a_positive_bearing():
    engine, _detector, _roster = build(boxes=[[shifted(PERSON, -0.3)]])
    state = engine.tick(scene(shifted(PERSON, -0.3)), None, 100.0, still(100.0))[0]
    assert state.bearing_deg is not None and state.bearing_deg > 0


def test_a_head_box_is_reported_once_a_face_is_located():
    engine, _detector, _roster = build()
    state = engine.tick(scene(), None, 100.0, still(100.0))[0]
    assert state.head_box is not None
    assert state.head_box[0] >= PERSON[0] - 0.2  # inside the top of the person box


def test_a_head_box_from_an_earlier_frame_travels_with_the_body_it_belongs_to():
    """A settled track refreshes its face every five seconds and the last hit is
    republished until then. Held at the pixels it was found in, the gaze and the
    overlay aim at where the head was rather than where the person now is."""
    engine, detector, _roster = build(locator=OnceLocator())
    fresh = engine.tick(scene(), None, 100.0, still(100.0))[0]
    assert fresh.head_box is not None

    moved = shifted(PERSON, 0.05)
    detector.boxes = [[moved]]
    later = engine.tick(scene(moved), None, 100.5, still(100.5))[0]

    assert later.box == pytest.approx(moved)
    assert later.head_box is not None
    assert later.head_box[1] == pytest.approx(fresh.head_box[1] + 0.05, abs=0.005)
    assert later.head_box != pytest.approx(head_region(later.box))  # still the hit, not the fallback


def test_a_head_box_older_than_the_face_it_came_from_follows_the_body_again():
    """Detections carry no head box, so the one a face hit left behind would go
    on aiming the gaze and the overlay at where the head was minutes ago."""
    engine, _detector, _roster = build(locator=OnceLocator())
    fresh = engine.tick(scene(), None, 100.0, still(100.0))[0]
    assert fresh.head_box is not None
    assert fresh.head_box != pytest.approx(head_region(fresh.box))

    stale = 100.0 + FACE_STALE_SEC + 0.5
    later = engine.tick(scene(), None, stale, still(stale))[0]
    assert later.head_box == pytest.approx(head_region(later.box))


# ------------------------------------------------------------ native frames


def test_the_native_buffer_gives_the_locator_far_more_pixels():
    locator = RecordingLocator()
    engine, _detector, _roster = build(locator=locator)
    engine.tick(scene(), None, 100.0, still(100.0))
    published_crop = locator.crop_heights[-1]
    engine.tick(scene(), native_jpeg(), 100.25, still(100.25))
    assert locator.crop_heights[-1] > published_crop


def _far_person(*, native: bool) -> tuple:
    """One tick on someone whose face is 42 native-equivalent pixels: 21 real
    ones out of the published 640x360 frame, 42 out of the native crop."""
    roster = FakeRoster()
    resolver = RecordingResolver(roster)
    engine, _detector, _roster = build(boxes=[[FAR_PERSON]], roster=roster, resolver=resolver)
    native_buffer = native_jpeg(FAR_PERSON) if native else None
    states = engine.tick(scene(FAR_PERSON), native_buffer, 100.0, still(100.0))
    return states[0], resolver.faces


def test_a_face_cropped_from_the_published_frame_is_detect_only():
    """RFC section 9: with the native topic gone the face range is 1.3 m. The
    box is the same in both paths, so the native-equivalent size cannot tell
    them apart — only the pixels the crop really carried can."""
    state, faces = _far_person(native=False)
    assert state.frames_with_face == 1 and state.head_box is not None
    assert (state.head_box[2] - state.head_box[0]) * 360 == pytest.approx(21.0, abs=1.0)
    assert faces == []  # it keeps the track alive; it says nothing about who


def test_the_same_face_out_of_the_native_crop_carries_enough_pixels_to_match():
    state, faces = _far_person(native=True)
    assert state.frames_with_face == 1
    (observation,) = faces
    assert observation.real_px == pytest.approx(observation.size_px)
    assert observation.real_px >= FACE_MIN_MATCH_REAL_PX
    assert observation.embedding is not None


def test_native_health_is_unavailable_until_the_topic_arrives():
    engine, _detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    assert engine.health(100.0).get("native") == str(HealthState.UNAVAILABLE)


def test_native_health_is_ok_while_the_topic_flows():
    engine, _detector, _roster = build()
    engine.tick(scene(), native_jpeg(), 100.0, still(100.0))
    assert engine.health(100.0).get("native") == str(HealthState.OK)


def test_native_health_goes_stale_when_the_topic_stops():
    engine, _detector, _roster = build()
    engine.tick(scene(), native_jpeg(), 100.0, still(100.0))
    assert engine.health(110.0).get("native") == str(HealthState.STALE)


def test_camera_health_goes_stale_when_frames_stop():
    engine, _detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    assert engine.health(100.5).get("camera") == str(HealthState.OK)
    assert engine.health(110.0).get("camera") == str(HealthState.STALE)


def test_camera_health_is_unavailable_before_any_frame():
    engine, _detector, _roster = build()
    assert engine.health(100.0).get("camera") == str(HealthState.UNAVAILABLE)


def test_health_carries_the_backend_states_through():
    engine, _detector, _roster = build()
    health = engine.health(100.0)
    assert health.get("face_model") == str(HealthState.OK)
    assert health.get("body_model") == str(HealthState.OK)
    assert health.get("gpu") == str(HealthState.NONE)


# ---------------------------------------------------------------- enrolment


def walk_in_place(engine: PeopleEngine, detector: RecordingDetector, *, ticks: int = 8, step: float = 0.5) -> float:
    """A person who stands and shifts slightly, so consecutive frames are
    independent evidence rather than near-duplicates."""
    now = 1000.0
    for index in range(ticks):
        box = shifted(PERSON, 0.03 * (index % 2))
        detector.boxes = [[box]]
        engine.tick(scene(box), None, now, still(now))
        now += step
    return now


def test_a_person_who_stays_and_matches_nobody_is_enrolled():
    engine, detector, roster = build()
    walk_in_place(engine, detector)
    assert len(roster.created) == 1
    assert engine.tracks()[0].identity.state is IdentityState.FAMILIAR


def test_the_enrolment_is_reported_in_the_tick_resolutions():
    """The node turns the enrolling tick's resolutions into a people event, so
    the id has to be reported on the tick that created it and on no other."""
    engine, detector, _roster = build()
    enrolled: list[str] = []
    now = 1000.0
    for index in range(8):
        box = shifted(PERSON, 0.03 * (index % 2))
        detector.boxes = [[box]]
        engine.tick(scene(box), None, now, still(now))
        enrolled += [r.enrolled_id for r in engine.resolutions().values() if r.enrolled_id]
        now += 0.5
    assert enrolled == [engine.tracks()[0].identity.person_id]


def test_a_passer_by_seen_for_under_two_seconds_is_never_enrolled():
    engine, detector, roster = build()
    walk_in_place(engine, detector, ticks=4, step=0.4)
    assert roster.created == []


def test_collection_turned_off_stops_the_engine_enrolling():
    engine, detector, roster = build(roster=FakeRoster(collection=False))
    walk_in_place(engine, detector)
    assert roster.created == []


def test_enrolment_templates_carry_the_embedding_model():
    engine, detector, roster = build()
    walk_in_place(engine, detector)
    assert {t.model for t in roster.created[0]} == {ColorFaceEmbedder.model}


# -------------------------------------------------------------- split hook


def test_a_resolver_split_request_retires_the_tag_and_issues_a_new_one():
    roster = FakeRoster()
    engine, _detector, _roster = build(roster=roster, resolver=SplitOnceResolver(roster))
    engine.tick(scene(), None, 100.0, still(100.0))
    assert engine.tracker.get("P1") is None
    assert [t.tag for t in engine.tracker.live()] == ["P2"]


def test_the_split_track_resolves_afresh():
    roster = FakeRoster()
    engine, _detector, _roster = build(roster=roster, resolver=SplitOnceResolver(roster))
    engine.tick(scene(), None, 100.0, still(100.0))
    assert engine.resolver.identity("P2").person_id is None


# ------------------------------------------------------------ re-association

LEAVES_AND_RETURNS = [[PERSON], [], [shifted(PERSON, 0.3)]]
"""One person, then an empty room long enough to lose the track, then the same
person somewhere else — which the outfit embedding folds back onto the old tag."""


def walk_out_and_back(engine: PeopleEngine, *, native: bool = False) -> None:
    for now, box in ((100.0, PERSON), (104.0, PERSON), (110.0, shifted(PERSON, 0.3))):
        engine.tick(scene(box), native_jpeg(box) if native else None, now, still(now))


def test_a_re_association_resumes_the_identity_exactly_once():
    """The tracker hands the restored tag straight back, so the engine resumes it
    on this tick. Queueing it for ``take_recovered`` as well resumed it again the
    next tick, throwing away the reconfirmation it had just been given."""
    roster = FakeRoster()
    resolver = CountingResolver(roster)
    engine, _detector, _roster = build(boxes=LEAVES_AND_RETURNS, roster=roster, resolver=resolver)
    walk_out_and_back(engine)
    engine.tick(scene(shifted(PERSON, 0.3)), None, 110.4, still(110.4))
    assert [t.tag for t in engine.tracker.live()] == ["P1"]
    assert resolver.resumed == ["P1"]


def test_a_re_association_leaves_nothing_behind_under_the_retired_tag():
    engine, _detector, _roster = build(boxes=LEAVES_AND_RETURNS)
    walk_out_and_back(engine, native=True)
    assert [t.tag for t in engine.tracker.live()] == ["P1"]
    # _build_states is what normally cleans the gate up, and it only ever sees
    # tags the tracker still knows about, so a tag retired here leaks for good.
    assert set(engine._independence._last) == {"P1"}


# ------------------------------------------------------------------ outputs


def test_a_speaking_tag_is_marked_on_its_track():
    engine, _detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    states = engine.tick(scene(), None, 100.25, still(100.25), speaking=["P1"])
    assert states[0].speaking


def test_a_lost_track_is_still_reported_so_the_block_can_say_so():
    engine, detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    detector.boxes = [[]]
    states = engine.tick(np.full((480, 640, 3), 95, np.uint8), None, 103.0, still(103.0))
    assert [s.lost for s in states] == [True]


def test_tracks_are_reported_in_the_order_they_were_first_seen():
    engine, detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    second = shifted(PERSON, 0.35)
    detector.boxes = [[PERSON, second]]
    frame = paint_person(scene(), second)
    states = engine.tick(frame, None, 100.25, still(100.25))
    assert [s.tag for s in states] == ["P1", "P2"]


def test_the_camera_model_can_be_replaced_at_runtime():
    engine, _detector, _roster = build()
    original = engine.camera
    engine.set_camera(original.native())
    assert engine.camera.fx == pytest.approx(original.fx * 2)


def test_resolutions_are_exposed_for_every_live_track():
    engine, _detector, _roster = build()
    engine.tick(scene(), None, 100.0, still(100.0))
    assert set(engine.resolutions()) == {"P1"}


def test_the_engine_runs_with_no_face_backend_at_all():
    detector = RecordingDetector([[PERSON]])
    backends = fake_backends([[Detection(box=PERSON, score=0.9)]], with_face=False, with_body=False)
    backends.detector = detector
    engine = PeopleEngine(backends, FakeRoster())
    states = engine.tick(scene(), None, 100.0, still(100.0))
    assert states[0].frames_with_face == 0
    assert states[0].identity.state is IdentityState.UNKNOWN


# ------------------------------------------------------------ frame helpers


def test_the_left_eye_is_the_right_half_of_the_buffer_turned_around():
    buffer = np.zeros((720, 2560, 3), np.uint8)
    buffer[0:10, 1280:1290] = (0, 0, 255)  # a mark at the right half's top-left
    eye = native_frames.left_eye(buffer)
    assert eye.shape == (720, 1280, 3)
    assert tuple(eye[-1, -1]) == (0, 0, 255)  # ends up at the bottom-right


def test_the_left_eye_split_follows_the_buffer_it_is_given():
    reduced = np.zeros((360, 1280, 3), np.uint8)
    assert native_frames.left_eye(reduced).shape == (360, 640, 3)


def test_a_reduced_decode_halves_the_native_buffer():
    full = native_frames.decode(native_jpeg())
    half = native_frames.decode(native_jpeg(), reduced=True)
    assert full is not None and half is not None
    assert half.shape[0] == full.shape[0] // 2


def test_decoding_an_empty_payload_is_none_rather_than_a_crash():
    assert native_frames.decode(b"") is None
    assert native_frames.decode_left_eye(b"\x00\x01") is None


def test_the_published_frame_is_un_squashed_to_true_proportions():
    assert native_frames.unsquash_published(scene()).shape[:2] == (360, 640)


def test_un_squashing_an_already_true_frame_is_a_no_op():
    frame = np.zeros((360, 640, 3), np.uint8)
    assert native_frames.unsquash_published(frame) is frame


def test_a_crop_with_margin_grows_the_box_and_clips_to_the_frame():
    frame = np.zeros((100, 200, 3), np.uint8)
    tight = native_frames.crop(frame, (0.4, 0.4, 0.6, 0.6))
    grown = native_frames.crop(frame, (0.4, 0.4, 0.6, 0.6), margin=0.5)
    assert tight is not None and grown is not None
    assert grown.shape[0] > tight.shape[0]
    whole = native_frames.crop(frame, (0.0, 0.0, 2.0, 2.0))
    assert whole is not None and whole.shape[:2] == (100, 200)


def test_a_crop_entirely_outside_the_frame_is_none():
    frame = np.zeros((100, 200, 3), np.uint8)
    assert native_frames.crop(frame, (1.5, 1.5, 1.6, 1.6)) is None


def test_the_crop_origin_is_where_the_crop_starts():
    frame = np.zeros((100, 200, 3), np.uint8)
    assert native_frames.crop_origin(frame, (0.4, 0.5, 0.6, 0.7)) == (100, 40)


def test_undistorting_with_no_coefficients_returns_the_crop_untouched():
    crop = np.zeros((20, 20, 3), np.uint8)
    model = CameraModel.published_default()
    assert native_frames.undistort_crop(crop, model, (0, 0)) is crop


def test_undistorting_with_coefficients_returns_a_crop_of_the_same_shape():
    crop = scene()[:40, :40]
    model = CameraModel(200.3, 267.3, 319.1, 248.7, 640, 480, distortion=(-0.3, 0.1, 0.0, 0.0, 0.0))
    assert native_frames.undistort_crop(crop, model, (100, 100)).shape == crop.shape


def test_a_small_head_region_is_upscaled_for_the_detector():
    small = np.zeros((30, 30, 3), np.uint8)
    assert native_frames.upscale_for_detection(small).shape[0] > 30


def test_a_head_region_that_is_already_big_is_left_alone():
    big = np.zeros((200, 200, 3), np.uint8)
    assert native_frames.upscale_for_detection(big) is big


# ---------------------------------------------------------------- backends


def test_the_zero_download_stack_always_loads():
    loaded = load_backends(Backend.NONE, Path("/nonexistent/models"), allow_download=False)
    assert loaded.detector.name == "hog"
    assert loaded.locator is None and loaded.face is None
    assert loaded.health.get("face_model") == str(HealthState.NONE)


def test_a_missing_model_file_degrades_to_a_health_flag(tmp_path):
    loaded = load_backends(Backend.OPENCV, tmp_path, allow_download=False)
    assert loaded.face is None
    assert loaded.health.get("face_model") == str(HealthState.UNAVAILABLE)


def test_a_model_is_never_fetched_when_downloads_are_off(tmp_path):
    assert ensure_model(YUNET, tmp_path, allow_download=False) is None
    assert list(tmp_path.iterdir()) == []


def test_an_already_present_model_is_used_without_a_fetch(tmp_path):
    (tmp_path / YUNET.filename).write_bytes(b"not really an onnx")
    assert ensure_model(YUNET, tmp_path, allow_download=False) == tmp_path / YUNET.filename


class _Fetched:
    """The two lines of ``urlopen``'s response that ``ensure_model`` uses."""

    def __enter__(self) -> "_Fetched":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return b"pretend onnx"


def test_a_model_that_cannot_be_written_degrades_instead_of_raising(tmp_path, monkeypatch):
    """load_backends is documented never to raise. A models directory the robot
    cannot write is a health flag, not a node that refuses to start."""

    def _no_space(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Fetched())
    monkeypatch.setattr(Path, "write_bytes", _no_space)

    assert ensure_model(YUNET, tmp_path / "models") is None


def test_the_hog_detector_finds_nothing_in_an_empty_room():
    assert HogPersonDetector().detect(np.full((360, 640, 3), 95, np.uint8)) == []


def test_the_hog_detector_tolerates_a_degenerate_frame():
    assert HogPersonDetector().detect(np.zeros((0, 0, 3), np.uint8)) == []


def test_the_fake_face_embedding_is_a_unit_vector():
    embedding = mean_color_embedding(np.full((10, 10, 3), 200, np.uint8))
    assert float(np.linalg.norm(embedding)) == pytest.approx(1.0, abs=1e-5)


def test_differently_painted_people_embed_differently():
    red = mean_color_embedding(np.full((10, 10, 3), (0, 0, 200), np.uint8))
    blue = mean_color_embedding(np.full((10, 10, 3), (200, 0, 0), np.uint8))
    assert float(np.dot(red, blue)) < 0.2


def test_the_null_body_embedder_says_nothing():
    assert NullBodyEmbedder().embed(np.zeros((10, 10, 3), np.uint8)).size == 0


def test_fake_backends_report_their_own_health():
    backends = fake_backends(with_body=False)
    assert backends.body_model == ""
    assert backends.health.get("body_model") == str(HealthState.NONE)
