# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The contracts between the packages, and the RFC's regression list.

Every other people suite tests one package against its own rules. This one
tests the seams: a real ``build_snapshot`` payload is carried through the wire
and handed to each of its four consumers — the brain's block, the brain's
overlay, the skills SDK and (by field name) the webapp — and the frame-pairing
invariant of RFC section 7 is walked from the driver's header stamp to the ring
the overlay is drawn on. The rest is docs/rfc/people-memory.md section 11's
regression list, the scenarios not already owned by one package's own suite.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import json
import sys
from collections.abc import MutableSequence
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from brain_client.brain import overlay, people_context
from brain_client.people.memory import Attribution, FactKind, FactSource
from brain_client.people.resolve import Resolver
from brain_client.people.scribe import (
    Introduction,
    ScribeName,
    ScribeOutput,
    Speaker,
    TagView,
    Utterance,
    Window,
)
from brain_client.people.scribe import apply as apply_window
from brain_client.people.sdk_parse import parse_snapshot
from brain_client.people.store import PeopleStore
from brain_client.people.surfacing import build_snapshot, choose_attention
from brain_client.people.types import (
    Evidence,
    FaceObservation,
    FaceTemplate,
    HealthDict,
    Identity,
    IdentityState,
    TrackState,
)

_ROS_ROOTS = frozenset({"brain_messages", "geometry_msgs", "nav_msgs", "rclpy", "sensor_msgs", "std_msgs"})


class _StubModule(ModuleType):
    __path__: MutableSequence[str] = []

    def __getattr__(self, name: str) -> MagicMock:
        value = MagicMock()
        setattr(self, name, value)
        return value


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec) -> ModuleType:
        return _StubModule(spec.name)

    def exec_module(self, module: ModuleType) -> None:
        pass


class _StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname.partition(".")[0] not in _ROS_ROOTS:
            return None
        return importlib.util.spec_from_loader(fullname, _StubLoader())


# Appended, never inserted: where ROS is installed its own finder answers first.
_STUB_FINDER = _StubFinder()
sys.meta_path.append(_STUB_FINDER)

from brain_client.people import node_adapters as na  # noqa: E402 — needs the stubs above
from brain_client.perception import camera as cam  # noqa: E402 — same

sys.meta_path.remove(_STUB_FINDER)
for _stubbed in [name for name, module in sys.modules.items() if isinstance(module, _StubModule)]:
    del sys.modules[_stubbed]

NOW = 1_788_818_400.0
MODEL = "sface-2021dec-128"
HEALTH: HealthDict = {
    "camera": "ok",
    "native": "unavailable",
    "face_model": "ok",
    "body_model": "unavailable",
    "gpu": "none",
}


@dataclass(frozen=True)
class _Track:
    """The little of a track the resolver reads (people.resolve.TrackView)."""

    tag: str
    first_seen: float
    last_seen: float
    lost: bool = False


@pytest.fixture
def store(tmp_path) -> PeopleStore:
    return PeopleStore(tmp_path / "people")


def template(model: str = MODEL, *, seed: float = 1.0) -> FaceTemplate:
    vector = np.array([seed, 0.0, 0.0], dtype=np.float32)
    return FaceTemplate(embedding=vector, model=model, stamp=NOW, pose_bucket="frontal", quality=0.9)


def enrol(store: PeopleStore, name: str | None = None, *, now: float = NOW, model: str = MODEL) -> str:
    person_id = store.create_unnamed([template(model)], b"thumbnail-jpeg", now)
    if name is not None:
        store.rename(person_id, name, "conversation", now=now)
    return person_id


def track(
    tag: str,
    *,
    state: IdentityState = IdentityState.KNOWN,
    person_id: str | None = None,
    name: str | None = None,
    box=(0.10, 0.30, 0.93, 0.56),
    lost: bool = False,
    range_m: float | None = 1.8,
) -> TrackState:
    return TrackState(
        tag=tag,
        box=box,
        head_box=(box[0], box[1], box[0] + 0.16, box[1] + 0.10),
        identity=Identity(state=state, person_id=person_id, name=name, confidence=0.91, evidence=(Evidence.FACE,)),
        first_seen=NOW - 41.2,
        last_seen=NOW,
        lost=lost,
        range_m=range_m,
        bearing_deg=-4.0,
        frames_with_face=9,
        last_face_stamp=NOW - 0.4,
    )


def frame_jpeg(width: int = 640, height: int = 480) -> bytes:
    ok, encoded = cv2.imencode(".jpg", np.full((height, width, 3), 90, np.uint8))
    assert ok
    return bytes(encoded)


# ------------------------------------------------- one snapshot, all consumers


@pytest.fixture
def scene(store: PeopleStore) -> tuple[dict, str]:
    """One real snapshot: a known person with a memory, and a stranger."""
    theo = enrol(store, "Theo")
    store.set_description(theo, "Man, 30s, glasses.", now=NOW - 90)
    store.add_fact(
        theo,
        "likes pasta",
        FactKind.PREFERENCE,
        now=NOW - 50,
        attribution=Attribution.SELF,
        source=FactSource(stamp=NOW - 50, quote="I love pasta"),
        importance=0.7,
    )
    store.add_open_loop(theo, "find the blue socks", now=NOW - 40, due="2026-09-09")
    store.record_sighting(theo, NOW - 3600, "kitchen", (3.12, 1.40, 0.0))
    tracks = [
        track("P3", person_id=theo, name="Theo"),
        track("P4", state=IdentityState.UNKNOWN, box=(0.2, 0.62, 0.9, 0.8), range_m=2.5),
    ]
    snapshot = build_snapshot(
        tracks,
        store,
        HEALTH,
        NOW,
        frame_stamp_ns="1788818400123456789",
        image_size=(640, 480),
        attention=choose_attention(tracks, NOW),
    )
    return json.loads(json.dumps(snapshot)), theo


def test_the_snapshot_survives_json_and_reaches_the_sdk_unchanged(scene):
    snapshot, theo = scene
    view = parse_snapshot(json.dumps(snapshot))

    assert view.stamp == snapshot["stamp"] and view.is_fresh(NOW)
    assert [person.tag for person in view.in_view()] == ["P3", "P4"]  # nearest first
    found = view.find("Theo")
    assert found is not None and found.person_id == theo
    # Per-mille ints on the wire, normalized floats in the skill.
    assert snapshot["people"][0]["bbox"] == [100, 300, 930, 560]
    assert found.bbox == pytest.approx((0.100, 0.300, 0.930, 0.560))
    assert found.range_m == 1.8 and found.bearing_deg == -4.0


def test_the_same_snapshot_reads_as_the_rfc_block(scene):
    snapshot, _theo = scene
    block = people_context.render(snapshot, [], [("user", "what is for dinner?")], NOW, boxes_drawn=True)

    assert block is not None
    lines = block.splitlines()
    assert lines[0] == "People in view:"
    assert lines[1].startswith("- P3 = Theo (known, face). Man, 30s, glasses.")
    assert "likes pasta" in block and "find the blue socks (open, due 2026-09-09)" in block
    assert "P4 = unknown (tracked 41 s)" in block
    assert block.rstrip().splitlines()[-1].startswith("Attention: trying to see P4's face (2.5 m away")
    assert "0.9" not in block and "%" not in block  # a cosine is never shown as a number


def test_the_same_snapshot_draws_only_the_people_who_are_there(scene):
    snapshot, _theo = scene
    jpeg = frame_jpeg()
    drawn = overlay.draw_people(jpeg, snapshot)
    assert drawn is not None and drawn != jpeg
    assert cv2.imdecode(np.frombuffer(drawn, np.uint8), cv2.IMREAD_COLOR).shape == (480, 640, 3)

    snapshot["people"][0]["lost"] = True
    snapshot["people"][1]["lost"] = True
    assert overlay.draw_people(jpeg, snapshot) is jpeg


def test_the_snapshot_carries_every_field_its_consumers_read(scene):
    """The four readers of /brain/people, by name: brain/people_context.py,
    brain/overlay.py, people/sdk_parse.py and webapp/js/agent/peopleOverlay.js."""
    snapshot, _theo = scene
    assert set(snapshot) == {
        "schema",
        "stamp",
        "frame_stamp_ns",
        "image_size",
        "health",
        "collection_enabled",
        "attention",
        "people",
        "recent",
    }
    assert {
        "tag",
        "person_id",
        "name",
        "state",
        "evidence",
        "confidence",
        "runner_up_name",
        "bbox",
        "head_bbox",
        "range_m",
        "bearing_deg",
        "tracked_sec",
        "lost",
        "description",
        "hint",
        "learned",
        "digest",
    } == set(snapshot["people"][0])
    assert set(snapshot["attention"]) == {"tag", "text", "head_bbox"}
    assert set(snapshot["people"][0]["digest"]) == {"facts", "open_loops", "episodes", "last_seen", "encounters"}
    assert isinstance(snapshot["frame_stamp_ns"], str)  # a JSON number loses its last digits in JS
    assert snapshot["image_size"] == [640, 480]  # width, height — perception/gaze.py unpacks it that way


def test_the_settings_card_reads_what_get_people_answers(store: PeopleStore, scene):
    """webapp/js/settings/people.js parseRoster, field by field."""
    snapshot, theo = scene
    answer = json.loads(
        json.dumps(
            na.roster_answer(
                snapshot, roster=store.roster(include_thumbnails=True), capacity_full=store.capacity_full()
            )
        )
    )
    row = answer["roster"][0]
    assert row["person_id"] == theo and row["name"] == "Theo" and row["unnamed"] is False
    assert row["last_seen"]["stamp"] == NOW - 3600 and row["encounters"] >= 1
    assert row["description"] == "Man, 30s, glasses."
    assert isinstance(row["thumbnail"], str)  # base64 JPEG, only because it was asked for
    assert answer["capacity_full"] is False and answer["collection_enabled"] is True

    plain = json.loads(json.dumps(na.roster_answer(snapshot, roster=store.roster(), capacity_full=False)))
    assert plain["roster"][0]["thumbnail"] is None


# ------------------------------------------------------- frame pairing (RFC 7)


class _Ring:
    """A real CameraCapture with a mock node and a clock the test drives."""

    def __init__(self, monkeypatch):
        self.monotonic = [1000.0]
        monkeypatch.setattr(cam.time, "monotonic", lambda: self.monotonic[0])
        config = SimpleNamespace(
            image_topic="/mars/main_camera/left/image_raw/compressed",
            arm_camera_image_topic="/arm/compressed",
            send_arm_camera_image=False,
            cmd_vel_topic="/cmd_vel",
        )
        self.capture = cam.CameraCapture(MagicMock(), config)

    def arrive(self, sec: int, nanosec: int, jpeg: bytes = b"jpeg", *, pitch: float = 0.0) -> int:
        self.capture.current_head_pitch = pitch
        self.capture._on_image(
            SimpleNamespace(data=jpeg, header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nanosec)))
        )
        self.monotonic[0] += 1.0 / 7.5  # the compressed topic's own rate
        return na.stamp_ns(sec, nanosec)


def test_the_engines_stamp_and_the_brains_ring_name_the_same_frame(monkeypatch):
    """The driver's header stamp, formatted by the node and parsed by the brain,
    has to survive the round trip digit for digit — it is the only thing tying a
    box to a picture."""
    ring = _Ring(monkeypatch)
    stamp = ring.arrive(1_788_818_400, 123_456_789, b"the-frame", pitch=-7.5)

    published = na.stamp_text(stamp)
    assert published == "1788818400123456789"
    assert people_context.frame_stamp_ns({"frame_stamp_ns": published}) == stamp

    paired = ring.capture.frame_for_stamp(stamp, 3.0)
    assert paired == (b"the-frame", -7.5)  # the pitch the frame was taken at, not the pitch now


def test_the_ring_holds_a_tick_and_a_half_of_frames_and_no_more(monkeypatch):
    """The engine ticks at 5 Hz and publishes a few hundred milliseconds later;
    the ring has to outlive that and nothing else."""
    ring = _Ring(monkeypatch)
    stamps = [ring.arrive(1000 + step, 0) for step in range(30)]

    assert ring.capture.frame_for_stamp(stamps[-1], 3.0) is not None
    assert ring.capture.frame_for_stamp(stamps[-8], 3.0) is not None  # ~1.0 s back
    assert ring.capture.frame_for_stamp(stamps[-20], 3.0) is None  # ~2.5 s back: gone
    assert ring.capture.frame_for_stamp(stamps[0], 3.0) is None


def test_a_stamp_the_ring_never_held_pairs_with_nothing(monkeypatch):
    ring = _Ring(monkeypatch)
    ring.arrive(1000, 0)
    assert ring.capture.frame_for_stamp(na.stamp_ns(999, 0), 3.0) is None


def test_a_camera_stamp_reset_pairs_on_the_new_epoch_not_the_old_one(monkeypatch):
    """The driver restarts and its stamps jump backwards. Both epochs sit in the
    ring; a match is exact, so the old stamp cannot claim the new frame."""
    ring = _Ring(monkeypatch)
    before = ring.arrive(1_788_818_400, 0, b"before-restart")
    after = ring.arrive(12, 0, b"after-restart")

    assert ring.capture.frame_for_stamp(after, 3.0) == (b"after-restart", 0.0)
    assert ring.capture.frame_for_stamp(before, 3.0) == (b"before-restart", 0.0)
    assert ring.capture.fresh_frame(3.0) == (b"after-restart", 0.0)  # freshest is arrival, not stamp


def test_the_block_says_so_when_the_frame_could_not_be_paired(scene):
    snapshot, _theo = scene
    block = people_context.render(snapshot, [], [], NOW, boxes_drawn=False)
    assert block is not None and block.startswith("People in view (positions not drawn this turn):")


# ------------------------------------------ RFC section 11 regression scenarios


def test_two_people_with_the_same_name_are_two_records_and_an_actionable_error(store: PeopleStore):
    first, second = enrol(store, "Alex"), enrol(store, "Alex", now=NOW + 1)
    assert first != second

    person_id, message = na.resolve_who("Alex", (), store.roster())
    assert person_id is None and "2 people called Alex" in message
    assert na.resolve_who(first, (), store.roster())[0] == first

    tracks = (track("P3", person_id=first, name="Alex"), track("P4", person_id=second, name="Alex", range_m=3.0))
    snapshot = build_snapshot(tracks, store, HEALTH, NOW)
    nearer = parse_snapshot(json.dumps(snapshot)).find("Alex")
    assert nearer is not None and nearer.tag == "P3"  # names are not unique: the nearer one
    block = people_context.render(snapshot, [], [], NOW, boxes_drawn=True)
    assert block is not None and block.count("= Alex (known") == 2  # two tags, never one name on both


def test_a_retried_mutation_is_answered_the_same_way_twice(store: PeopleStore):
    ana = enrol(store, "Ana")
    assert store.rename(ana, "Anna", "app", now=NOW) is True
    assert store.rename(ana, "Anna", "app", now=NOW) is True  # idempotent: the name is still Anna
    assert store.name_of(ana) == "Anna"

    assert store.forget(ana, now=NOW) is True
    assert store.forget(ana, now=NOW) is False
    person_id, message = na.resolve_who(ana, (), store.roster(), forgotten=store.is_tombstoned)
    assert person_id is None and "forgotten" in message  # never silently the nearest live track


def test_a_model_upgrade_starts_a_new_template_space_rather_than_comparing_across(store: PeopleStore, tmp_path):
    """Templates carry their model id, and a reload must keep it: an embedding
    compared across spaces is a random number with a name attached."""
    theo = enrol(store, "Theo", model="sface-2021dec-128")
    store.flush()

    reloaded = PeopleStore(tmp_path / "people")
    assert [t.model for t in reloaded.face_templates(theo, "sface-2021dec-128")] == ["sface-2021dec-128"]
    assert reloaded.face_templates(theo, "sface-2026jun-256") == []

    resolver = Resolver(reloaded)
    for step in range(4):
        resolver.observe_face(
            "P1",
            FaceObservation(
                stamp=NOW + step,
                box=(0.2, 0.4, 0.8, 0.55),
                size_px=64.0,
                real_px=64.0,
                yaw_deg=0.0,
                pitch_deg=0.0,
                sharpness=200.0,
                luminance=130.0,
                quality=1.0,
                model="sface-2026jun-256",
                embedding=np.array([1.0, 0.0, 0.0], dtype=np.float32),
            ),
        )
    resolved = resolver.resolve([_Track("P1", NOW, NOW + 3)], NOW + 3)
    assert resolved["P1"].identity.person_id != theo


def test_a_brain_activating_late_reads_the_enrolment_off_the_latched_snapshot(store: PeopleStore):
    """RFC 6.4: an inactive brain misses the wake events and misses nothing that
    matters, because the digest rides the latched snapshot."""
    zoe = enrol(store, "Zoe")
    store.add_fact(
        zoe,
        "works upstairs",
        FactKind.BIOGRAPHY,
        now=NOW - 10,
        attribution=Attribution.SELF,
        source=FactSource(stamp=NOW - 10),
    )
    snapshot = build_snapshot((track("P3", person_id=zoe, name="Zoe"),), store, HEALTH, NOW)

    block = people_context.render(snapshot, [], [], NOW, boxes_drawn=True)
    assert block is not None and "P3 = Zoe (known" in block and "works upstairs" in block


def test_a_scribe_window_with_three_people_in_view_asks_instead_of_guessing(store: PeopleStore):
    people = [enrol(store, now=NOW + index) for index in range(3)]
    views = tuple(
        TagView(tag=f"P{index + 1}", state=IdentityState.FAMILIAR, person_id=person_id)
        for index, person_id in enumerate(people)
    )
    window = Window((Utterance("u_1", NOW, Speaker.USER, "I'm Ana", views),))
    output = ScribeOutput(
        name_candidates=(
            ScribeName(who="P1", name="Ana", quote="I'm Ana", utterance="u_1", introduction=Introduction.SELF),
        )
    )

    changes = apply_window(output, window, store, NOW)
    assert changes[0].text == "heard 'I'm Ana' but three people are in view; if it matters, ask which one"
    assert [store.name_of(person_id) for person_id in people] == [None, None, None]


def test_a_name_heard_in_one_window_commits_in_the_next_when_it_is_resolved(store: PeopleStore):
    """Delayed naming (RFC 6.3): the candidate waits, the agent asks in its own
    words, and the answer settles it a window later."""
    first, second = enrol(store, now=NOW), enrol(store, now=NOW + 1)
    seen = (
        TagView(tag="P1", state=IdentityState.FAMILIAR, person_id=first),
        TagView(tag="P2", state=IdentityState.FAMILIAR, person_id=second),
    )
    heard = Window((Utterance("u_1", NOW, Speaker.USER, "I'm Ana", seen),))
    apply_window(
        ScribeOutput(
            name_candidates=(
                ScribeName(who="P1", name="Ana", quote="I'm Ana", utterance="u_1", introduction=Introduction.SELF),
            )
        ),
        heard,
        store,
        NOW,
    )
    assert store.name_of(first) is None
    profile = store.profile(first)
    assert profile is not None and profile.name_candidates[0].name == "Ana"

    answered = Window((Utterance("u_2", NOW + 20, Speaker.USER, "that's me", seen),))
    changes = apply_window(
        ScribeOutput(
            name_candidates=(
                ScribeName(
                    who="P1",
                    name="Ana",
                    quote="that's me",
                    utterance="u_2",
                    introduction=Introduction.SELF,
                    referent="P1",
                ),
            )
        ),
        answered,
        store,
        NOW + 20,
    )
    assert changes[0].text.endswith("P1 = Ana from here on")
    assert store.name_of(first) == "Ana" and store.name_of(second) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
