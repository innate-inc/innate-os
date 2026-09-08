# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The people node's glue: everything ``people/node_adapters.py`` decides
before a message reaches ROS.

``node_adapters`` imports rclpy and the message packages at module level like
every other adapter in the tree, so they are fabricated here for the duration
of the import (the same ``sys.meta_path`` stub ``test_people_sdk.py`` uses; a
real installation wins). Nothing below touches ROS: the functions under test
take plain values, tracks and a real ``PeopleStore`` in a tmp directory.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import json
import sys
import time
from collections.abc import MutableSequence
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from brain_client.people.resolve import Resolution
from brain_client.people.scribe import Change, ChangeKind
from brain_client.people.store import MAX_NAMED, MAX_UNNAMED, PeopleStore
from brain_client.people.surfacing import PeopleEvents, build_snapshot
from brain_client.people.types import (
    EventKind,
    FaceTemplate,
    HealthDict,
    Identity,
    IdentityState,
    TrackState,
)

_ROS_ROOTS = frozenset({"brain_messages", "geometry_msgs", "nav_msgs", "rclpy", "sensor_msgs", "std_msgs"})


class _StubModule(ModuleType):
    """A module whose every attribute is a mock, and which is a package so the
    submodule imports below it keep resolving."""

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

# The stubs live exactly as long as the import above: every other test module in
# this session must go on finding ROS missing, because it is.
sys.meta_path.remove(_STUB_FINDER)
for _stubbed in [name for name, module in sys.modules.items() if isinstance(module, _StubModule)]:
    del sys.modules[_stubbed]

NOW = 1_788_818_400.0
HEALTH: HealthDict = {
    "camera": "ok",
    "native": "unavailable",
    "face_model": "ok",
    "body_model": "unavailable",
    "gpu": "none",
}


def track(
    tag: str = "P3",
    *,
    state: IdentityState = IdentityState.KNOWN,
    person_id: str | None = None,
    name: str | None = None,
    runner_up_name: str | None = None,
    range_m: float | None = 1.8,
    lost: bool = False,
    last_seen: float = NOW,
    frames_with_face: int = 3,
    last_face_stamp: float | None = NOW,
) -> TrackState:
    return TrackState(
        tag=tag,
        box=(0.1, 0.3, 0.93, 0.56),
        head_box=(0.1, 0.38, 0.26, 0.48),
        identity=Identity(state=state, person_id=person_id, name=name, confidence=0.9, runner_up_name=runner_up_name),
        first_seen=NOW - 40.0,
        last_seen=last_seen,
        lost=lost,
        range_m=range_m,
        bearing_deg=-4.0,
        frames_with_face=frames_with_face,
        last_face_stamp=last_face_stamp,
    )


@pytest.fixture
def store(tmp_path) -> PeopleStore:
    return PeopleStore(tmp_path / "people")


def enrol(store: PeopleStore, name: str | None = None, *, now: float = NOW) -> str:
    template = FaceTemplate(
        embedding=np.ones(4, dtype=np.float32), model="sface", stamp=now, pose_bucket="frontal", quality=0.9
    )
    person_id = store.create_unnamed([template], b"jpeg-bytes", now)
    if name is not None:
        store.rename(person_id, name, "app", now=now)
    return person_id


# ------------------------------------------------------------------- stamps


def test_header_stamp_is_nanoseconds_as_a_decimal_string():
    assert na.stamp_ns(1_788_818_400, 123_456_789) == 1_788_818_400_123_456_789
    assert na.stamp_text(na.stamp_ns(1_788_818_400, 123_456_789)) == "1788818400123456789"
    assert na.stamp_text(None) is None


def test_stamp_string_keeps_every_digit_a_json_number_would_lose():
    text = na.stamp_text(na.stamp_ns(1_788_818_400, 123_456_789))
    assert text is not None
    assert int(text) == 1_788_818_400_123_456_789  # the brain pairs its overlay on an exact match


# ------------------------------------------------------------------- frames


def test_a_jpeg_frame_decodes_to_bgr_pixels():
    cv2 = pytest.importorskip("cv2")
    image = np.zeros((8, 12, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    decoded = na.decode_frame(na.CameraFrame(1, bytes(buffer)))
    assert decoded is not None
    assert decoded.shape == (8, 12, 3)


def test_a_raw_frame_decodes_by_encoding_and_owns_its_memory():
    pixels = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
    bgr = na.decode_frame(na.CameraFrame(1, pixels.tobytes(), encoding="bgr8", width=3, height=2))
    rgb = na.decode_frame(na.CameraFrame(1, pixels.tobytes(), encoding="rgb8", width=3, height=2))
    assert bgr is not None and rgb is not None
    assert np.array_equal(bgr, pixels)
    assert np.array_equal(rgb, pixels[:, :, ::-1])
    assert bgr.flags.writeable and bgr.flags.c_contiguous  # cv2 refuses a read-only or strided view


def test_a_truncated_or_unknown_raw_frame_decodes_to_nothing():
    assert na.decode_frame(na.CameraFrame(1, b"\x00\x01", encoding="bgr8", width=640, height=480)) is None
    assert na.decode_frame(na.CameraFrame(1, b"\x00" * 12, encoding="mono8", width=2, height=2)) is None
    assert na.decode_frame(na.CameraFrame(1, b"not a jpeg")) is None


def test_the_native_buffer_pairs_by_nearest_stamp_inside_the_drivers_skew_cap():
    base = 1_788_818_400_000_000_000
    buffers = [(base - 120_000_000, b"early"), (base + 20_000_000, b"paired"), (base + 400_000_000, b"late")]
    assert na.pair_native(base, buffers) == b"paired"
    assert na.pair_native(base + 900_000_000, buffers) is None  # nothing inside 130 ms
    assert na.pair_native(base, []) is None


# --------------------------------------------------------------- duty cycle


def test_the_engine_runs_while_the_brain_is_active_or_always_on():
    assert na.engine_active(enabled=True, always_on=False, brain_active=True)
    assert na.engine_active(enabled=True, always_on=True, brain_active=False)
    assert not na.engine_active(enabled=True, always_on=False, brain_active=False)
    assert not na.engine_active(enabled=False, always_on=True, brain_active=True)


def test_the_tick_is_sampled_at_the_engines_own_detect_cadence():
    from brain_client.people.engine import EngineConfig

    config = EngineConfig()
    idle = na.decode_period(config, tracked=False, driving=False, motion=False)
    tracked = na.decode_period(config, tracked=True, driving=False, motion=False)
    motion = na.decode_period(config, tracked=False, driving=False, motion=True)
    driving = na.decode_period(config, tracked=True, driving=True, motion=False)
    assert idle == pytest.approx(2.0)  # 0.5 Hz with nobody around
    assert tracked == pytest.approx(0.2) and motion == pytest.approx(0.2)
    assert driving == pytest.approx(0.5)  # association only while a skill drives the base


def test_the_lazy_native_topic_is_wanted_only_while_a_track_needs_a_face():
    settled = track(state=IdentityState.KNOWN, person_id="person_a", last_face_stamp=NOW)
    assert not na.wants_native([settled], NOW, refresh_sec=5.0)
    assert na.wants_native([settled], NOW + 6.0, refresh_sec=5.0)  # the outfit refresh comes due
    assert na.wants_native([track(state=IdentityState.UNKNOWN)], NOW)
    assert not na.wants_native([track(state=IdentityState.UNKNOWN, lost=True)], NOW)
    assert not na.wants_native([], NOW)


# ------------------------------------------------------------- the snapshot


def test_a_lost_track_rides_the_snapshot_for_a_minute_and_then_stops():
    lost = track(lost=True, last_seen=NOW - 30.0)
    stale = track(tag="P4", lost=True, last_seen=NOW - 90.0)
    assert [t.tag for t in na.publishable_tracks([lost, stale], NOW)] == ["P3"]
    assert [t.tag for t in na.publishable_tracks([track()], NOW)] == ["P3"]


def test_a_camera_that_went_quiet_claims_nobody_rather_than_freezing_the_scene():
    tracks = [track()]
    assert na.fresh_tracks(tracks, now=NOW, last_frame_at=NOW - 1.0, stale_sec=3.0) == (tracks[0],)
    assert na.fresh_tracks(tracks, now=NOW, last_frame_at=NOW - 9.0, stale_sec=3.0) == ()
    assert na.fresh_tracks(tracks, now=NOW, last_frame_at=0.0, stale_sec=3.0) == ()  # no frame has ever arrived


def test_a_one_person_two_tracks_clash_reads_as_conflict_without_losing_the_name():
    tracks = [track(person_id="person_a", name="Theo", runner_up_name="Ana")]
    resolutions = {"P3": Resolution(tag="P3", identity=tracks[0].identity, conflict_with="P1")}
    resolved = na.apply_conflicts(tracks, resolutions)
    assert resolved[0].identity.state is IdentityState.CONFLICT
    assert resolved[0].identity.person_id == "person_a"
    assert resolved[0].identity.name == "Theo"
    # The rival is this person's own other track, not a second candidate.
    assert resolved[0].identity.runner_up_name is None


def test_a_track_without_a_clash_is_handed_through_untouched():
    tracks = [track(person_id="person_a", name="Theo")]
    resolutions = {"P3": Resolution(tag="P3", identity=tracks[0].identity, enrolled_id="person_a")}
    assert na.apply_conflicts(tracks, resolutions) == tracks
    assert na.apply_conflicts(tracks, {}) == tracks


def test_the_conflict_override_reaches_the_snapshot_and_raises_one_event(store):
    person_id = enrol(store, "Theo")
    tracks = [track(state=IdentityState.KNOWN, person_id=person_id, name="Theo", runner_up_name="Ana")]
    resolutions = {"P3": Resolution(tag="P3", identity=tracks[0].identity, conflict_with="P1")}
    resolved = na.apply_conflicts(tracks, resolutions)

    snapshot = build_snapshot(resolved, store, HEALTH, NOW)
    person = snapshot["people"][0]
    assert person["state"] == "conflict"
    assert person["name"] == "Theo" and person["person_id"] == person_id
    assert person["runner_up_name"] is None
    assert person["digest"] is None  # memories stay shut while it is unclear whose they are

    events = PeopleEvents().emit(resolved, NOW)
    assert [event["kind"] for event in events if event["kind"] == EventKind.CONFLICT] == [EventKind.CONFLICT]


def test_the_snapshot_carries_the_runner_up_so_a_conflict_can_be_worded(store):
    person_id = enrol(store, "Theo")
    tracks = [track(state=IdentityState.POSSIBLE, person_id=person_id, name="Theo", runner_up_name="Ana")]
    assert build_snapshot(tracks, store, HEALTH, NOW)["people"][0]["runner_up_name"] == "Ana"


def test_seek_faces_names_the_skill_that_would_get_the_face():
    tracks = [track(state=IdentityState.UNKNOWN, frames_with_face=0, last_face_stamp=None, range_m=3.0)]
    attention = {"tag": "P3", "text": "trying to see P3's face (3.0 m away, face not seen yet)", "head_bbox": None}
    assert na.seek_hint(attention, tracks, seek_faces=False) == attention
    hinted = na.seek_hint(attention, tracks, seek_faces=True)
    assert hinted is not None and hinted["text"].endswith("; approach_person(P3) would get a look")
    assert na.seek_hint(None, tracks, seek_faces=True) is None


def test_seek_faces_stays_quiet_when_walking_over_would_add_nothing():
    close = [track(state=IdentityState.UNKNOWN, frames_with_face=0, last_face_stamp=None, range_m=1.0)]
    seen = [track(state=IdentityState.UNKNOWN, frames_with_face=4, range_m=3.0)]
    attention = {"tag": "P3", "text": "trying to see P3's face", "head_bbox": None}
    assert na.seek_hint(attention, close, seek_faces=True) == attention
    assert na.seek_hint(attention, seen, seek_faces=True) == attention


# --------------------------------------------------------- publish cadence


def _snapshot(store: PeopleStore, tracks: list[TrackState], now: float = NOW):
    return build_snapshot(tracks, store, HEALTH, now)


def test_anyone_in_view_publishes_every_tick(store):
    snapshot = _snapshot(store, [track()])
    assert na.should_publish(snapshot, snapshot, now=NOW, last_publish=NOW, active=True)


def test_an_empty_unchanged_scene_publishes_only_on_the_heartbeat(store):
    first = _snapshot(store, [])
    again = _snapshot(store, [], now=NOW + 1.0)
    assert not na.should_publish(again, first, now=NOW + 1.0, last_publish=NOW, active=False)
    assert na.should_publish(again, first, now=NOW + 5.0, last_publish=NOW, active=False)


def test_a_change_publishes_even_with_nobody_in_view(store):
    first = _snapshot(store, [])
    store.set_collection(False)
    changed = _snapshot(store, [], now=NOW + 1.0)
    assert na.should_publish(changed, first, now=NOW + 1.0, last_publish=NOW, active=False)


def test_only_the_clock_moving_is_not_a_change(store):
    first = _snapshot(store, [])
    later = _snapshot(store, [], now=NOW + 1.0)
    assert not na.snapshot_changed(later, first)
    assert na.snapshot_changed(first, None)


# --------------------------------------------------- speaking attribution


def test_speech_attributes_to_the_one_person_in_talking_range():
    assert na.speaking_tag([track(tag="P3", range_m=1.2)]) == "P3"
    assert na.speaking_tag([track(tag="P3", range_m=None)]) == "P3"  # tracked, range not measured yet


def test_speech_attributes_to_nobody_when_the_answer_would_be_a_guess():
    assert na.speaking_tag([track(tag="P3", range_m=1.2), track(tag="P4", range_m=2.0)]) is None
    assert na.speaking_tag([]) is None
    assert na.speaking_tag([track(tag="P3", range_m=6.0)]) is None  # across the room, not talking to it
    assert na.speaking_tag([track(tag="P3", range_m=1.2, lost=True)]) is None


def test_the_speaking_mark_expires_with_the_message():
    assert na.held_speaking(("P3", NOW + 3.0), NOW) == ("P3",)
    assert na.held_speaking(("P3", NOW + 3.0), NOW + 4.0) == ()
    assert na.held_speaking(None, NOW) == ()


# --------------------------------------------------------- the scribe feed


def test_a_chat_in_message_is_the_user_talking_with_the_tags_that_were_in_view():
    views = na.tag_views([track(tag="P3", person_id="person_a", name="Theo"), track(tag="P4", lost=True)])
    utterance = na.chat_in_utterance({"text": "Hi, I'm Ana"}, uid="u_1", now=NOW, in_view=views)
    assert utterance is not None
    assert utterance.speaker == "user" and utterance.text == "Hi, I'm Ana" and utterance.id == "u_1"
    assert [view.tag for view in utterance.in_view] == ["P3"]  # a lost track was not in view


def test_simulated_environment_speech_never_enters_the_transcript():
    payload = {"text": "I am the resident", "sender": "environment_speech", "voice_id": "x"}
    assert na.chat_in_utterance(payload, uid="u_1", now=NOW, in_view=()) is None
    assert na.chat_in_utterance({"text": "   "}, uid="u_1", now=NOW, in_view=()) is None


def test_chat_out_carries_speech_and_skill_results_but_not_thoughts():
    for sender in ("robot", "skill_output"):
        utterance = na.chat_out_utterance({"sender": sender, "text": "hello"}, uid="u_2", now=NOW, in_view=())
        assert utterance is not None and utterance.speaker == "robot"
    for sender in ("robot_thoughts", "system", "user"):
        assert na.chat_out_utterance({"sender": sender, "text": "x"}, uid="u_2", now=NOW, in_view=()) is None


def test_a_freshly_enrolled_track_is_nameable_to_the_scribe():
    tracks = [track(tag="P5", state=IdentityState.FAMILIAR, person_id="person_a")]
    assert na.tag_views(tracks)[0].enrolling is False
    assert na.tag_views(tracks, ["P5"])[0].enrolling is True
    assert na.tag_views(tracks, ["P5"])[0].nameable is True


# ---------------------------------------------------------------- recall


def test_a_memory_question_resolves_a_tag_a_name_and_a_pronoun(store):
    ana = enrol(store, "Ana")
    tracks = [track(tag="P3", person_id=ana, name="Ana")]
    roster = store.roster()
    assert na.recall_person("P3", tracks, roster) == ana
    assert na.recall_person("ana", tracks, roster) == ana
    assert na.recall_person("they", tracks, roster) == ana  # the only person in view


def test_a_pronoun_with_nobody_or_everybody_in_view_recalls_nothing(store):
    ana = enrol(store, "Ana")
    theo = enrol(store, "Theo")
    two = [track(tag="P3", person_id=ana), track(tag="P4", person_id=theo)]
    assert na.recall_person("they", two, store.roster()) is None
    assert na.recall_person("they", [], store.roster()) is None
    assert na.recall_person("P9", two, store.roster()) is None  # a tag nobody is tracking


# -------------------------------------------------------------- services


def test_a_live_tag_resolves_to_the_person_it_is_tracking(store):
    ana = enrol(store, "Ana")
    tracks = [track(tag="P3", person_id=ana, name="Ana")]
    assert na.resolve_who("P3", tracks, store.roster()) == (ana, "")
    assert na.resolve_who("p3", tracks, store.roster()) == (ana, "")


def test_an_expired_tag_is_an_error_and_never_the_nearest_live_track(store):
    ana = enrol(store, "Ana")
    tracks = [track(tag="P3", person_id=ana, name="Ana")]
    person_id, message = na.resolve_who("P9", tracks, store.roster())
    assert person_id is None
    assert "P9" in message and "in view" in message


def test_a_tag_the_robot_has_not_enrolled_yet_is_an_error(store):
    tracks = [track(tag="P3", state=IdentityState.UNKNOWN, person_id=None)]
    person_id, message = na.resolve_who("P3", tracks, store.roster())
    assert person_id is None and "on file" in message


def test_a_person_id_resolves_and_a_forgotten_one_says_so(store):
    ana = enrol(store, "Ana")
    assert na.resolve_who(ana, [], store.roster()) == (ana, "")
    store.forget(ana)
    person_id, message = na.resolve_who(ana, [], store.roster(), forgotten=store.is_tombstoned)
    assert person_id is None and "forgotten" in message


def test_a_name_resolves_when_it_is_unambiguous_and_asks_otherwise(store):
    ana = enrol(store, "Ana")
    assert na.resolve_who("ana", [], store.roster()) == (ana, "")
    enrol(store, "Ana")
    person_id, message = na.resolve_who("Ana", [], store.roster())
    assert person_id is None and "2 people called Ana" in message
    unknown_id, unknown_message = na.resolve_who("Zoe", [], store.roster())
    assert unknown_id is None and "Zoe" in unknown_message


def test_an_empty_who_is_an_actionable_error(store):
    person_id, message = na.resolve_who("   ", [], store.roster())
    assert person_id is None and "P3" in message


def test_get_people_answers_the_snapshot_alone_unless_the_roster_was_asked_for(store):
    enrol(store, "Ana")
    snapshot = _snapshot(store, [track()])
    assert "roster" not in na.roster_answer(snapshot, roster=None, capacity_full=False)


def test_get_people_with_the_roster_answers_what_the_settings_card_reads(store):
    ana = enrol(store, "Ana")
    store.record_sighting(ana, NOW, "home", (3.1, 1.4, 0.0))
    unnamed = enrol(store)
    answer = na.roster_answer(
        _snapshot(store, [track()]),
        roster=store.roster(include_thumbnails=True),
        capacity_full=store.capacity_full(),
    )
    assert answer["stamp"] == pytest.approx(NOW)
    assert answer["collection_enabled"] is True
    assert answer["capacity_full"] is False
    rows = {entry["person_id"]: entry for entry in answer["roster"]}
    assert rows[ana]["name"] == "Ana" and rows[ana]["unnamed"] is False
    assert rows[ana]["last_seen"]["map"] == "home"
    assert rows[unnamed]["name"] is None and rows[unnamed]["unnamed"] is True
    assert isinstance(rows[ana]["thumbnail"], str)  # base64 JPEG, only because it was asked for


def test_the_roster_answer_omits_thumbnails_unless_asked(store):
    enrol(store, "Ana")
    answer = na.roster_answer(_snapshot(store, []), roster=store.roster(), capacity_full=False)
    assert answer["roster"][0]["thumbnail"] is None


def test_the_store_reports_a_full_roster_so_the_settings_page_can_say_so(store):
    assert store.capacity_full() is False
    for _ in range(MAX_UNNAMED):
        enrol(store)
    assert store.capacity_full() is True
    assert store.can_enrol() is False
    assert len(store.person_ids()) == MAX_UNNAMED <= MAX_NAMED + MAX_UNNAMED


def test_a_collection_switch_is_not_a_full_roster(store):
    store.set_collection(False)
    assert store.can_enrol() is False
    assert store.capacity_full() is False  # the owner's choice, not "no room" — the card says different things


# ---------------------------------------------------- parameters -> config


def test_the_parameters_map_onto_the_config_the_node_builds():
    config = na.config_from_params(
        {
            "enabled": True,
            "always_on": True,
            "seek_faces": True,
            "scribe": False,
            "prefer_backend": "inspireface",
            "tick_source": "raw",
            "allow_model_download": False,
            "retention_unnamed_days": 7.0,
            "retention_named_days": 100.0,
            "simulator_mode": True,
            "camera_height_m": 0.2,
            "gemini_model": "gemini-3.6-pro",
        }
    )
    assert config.always_on and config.seek_faces and not config.scribe
    assert config.prefer_backend == "inspireface"
    assert config.allow_model_download is False
    assert config.retention_unnamed_days == 7.0 and config.retention_named_days == 100.0
    assert config.camera_height_m == 0.2
    assert config.image_topic == na.RAW_IMAGE_TOPIC
    assert config.data_dir.name == "people_sim"  # sim evidence never mixes with the hardware's


def test_the_defaults_are_the_documented_ones():
    config = na.config_from_params({})
    assert config == na.PeopleNodeConfig()
    assert config.enabled and config.scribe and config.allow_model_download
    assert not config.always_on and not config.seek_faces and not config.simulator_mode
    assert config.prefer_backend == "opencv"
    assert config.camera_height_m == 0.26
    assert config.image_topic == na.COMPRESSED_IMAGE_TOPIC
    assert config.data_dir.name == "people"
    assert config.models_dir.parts[-3:] == ("data", "models", "people")


def test_a_mistyped_setting_keeps_the_default_rather_than_stopping_the_node():
    config = na.config_from_params(
        {"retention_named_days": "soon", "prefer_backend": "", "tick_source": "sideways", "camera_height_m": True}
    )
    assert config.retention_named_days == 548.0
    assert config.prefer_backend == "opencv"
    assert config.tick_source == na.TickSource.COMPRESSED
    assert config.camera_height_m == 0.26  # bool is an int; a height of True is not a height


def test_every_declared_parameter_reaches_a_config_field():
    assert set(na.PARAM_DEFAULTS) == {field for field in na.PeopleNodeConfig().__dataclass_fields__}


# ------------------------------------------------------- one tick, end to end


def _adapters(store: PeopleStore, tmp_path, *, frames=None, scribe=None):
    """The real adapters around a real engine with scripted backends; the node
    itself is a mock, so every ROS call is recorded rather than made."""
    from brain_client.people.backends import Backends, FixedDetector
    from brain_client.people.engine import EngineConfig, PeopleEngine
    from brain_client.people.types import Detection

    detector = FixedDetector(frames or [[Detection(box=(0.10, 0.30, 0.93, 0.56), score=0.9)]])
    backends = Backends(
        detector=detector,
        locator=None,
        face=None,
        body=None,
        health={"face_model": "unavailable", "body_model": "unavailable", "gpu": "none"},
    )
    engine_config = EngineConfig()
    engine = PeopleEngine(backends, store, config=engine_config)
    return na.PeopleAdapters(
        MagicMock(),
        na.PeopleNodeConfig(scribe=False, simulator_mode=True),
        store=store,
        engine=engine,
        engine_config=engine_config,
        scribe=scribe,
        transport=None,
    )


def test_one_tick_turns_a_frame_into_the_published_snapshot(store, tmp_path):
    cv2 = pytest.importorskip("cv2")
    adapters = _adapters(store, tmp_path)
    ok, buffer = cv2.imencode(".jpg", np.full((480, 640, 3), 120, dtype=np.uint8))
    assert ok
    stamp = na.stamp_ns(1_788_818_400, 123_456_789)
    adapters._sensors.brain_active = True
    adapters._sensors._frame = na.CameraFrame(stamp, bytes(buffer))

    # The adapters run on the wall clock: the tracks a service reads are only
    # the ones a frame vouched for a moment ago.
    adapters._tick(time.time())

    assert adapters._snapshot_pub.publish.called
    payload = json.loads(na.String.call_args.kwargs["data"])
    assert payload["schema"] == 1
    assert payload["frame_stamp_ns"] == "1788818400123456789"  # the brain pairs its overlay on this
    assert payload["image_size"] == [640, 480]
    assert [person["tag"] for person in payload["people"]] == ["P1"]
    assert payload["people"][0]["state"] == "unknown"
    assert payload["health"]["camera"] == "ok" and payload["health"]["native"] == "unavailable"
    assert payload["health"]["scribe"] == "none"  # switched off in this config
    assert [track.tag for track in adapters.tracks()] == ["P1"]


def _jpeg():
    cv2 = pytest.importorskip("cv2")
    ok, buffer = cv2.imencode(".jpg", np.full((480, 640, 3), 120, dtype=np.uint8))
    assert ok
    return bytes(buffer)


def _walking(step: int) -> bytes:
    """A frame with a large block in a different place each step — motion the
    gate cannot mistake for an exposure change."""
    cv2 = pytest.importorskip("cv2")
    frame = np.full((480, 640, 3), 100, np.uint8)
    left = 40 + 60 * step
    cv2.rectangle(frame, (left, 60), (left + 160, 420), (230, 230, 230), -1)
    ok, buffer = cv2.imencode(".jpg", frame)
    assert ok
    return bytes(buffer)


def _left(store, tmp_path):
    """A node whose one tracked person walks out of frame on the second tick."""
    from brain_client.people.types import Detection

    adapters = _adapters(store, tmp_path, frames=[[Detection(box=(0.10, 0.30, 0.93, 0.56), score=0.9)], []])
    adapters._sensors.brain_active = True
    jpeg = _jpeg()
    start = time.time()
    for step, moment in enumerate((start, start + 3.0)):
        adapters._sensors._frame = na.CameraFrame(na.stamp_ns(step + 1, 0), jpeg)
        adapters._tick(moment)
    return adapters, jpeg, start + 3.0


def test_a_track_that_only_lingers_lost_lets_the_node_back_onto_the_idle_clock(store, tmp_path):
    """The tracker keeps a lost track for five minutes so it can be re-associated;
    counting it as "somebody is here" would hold the whole duty cycle (RFC 4.6)
    at 5 Hz for those five minutes with nobody in the room."""
    adapters, jpeg, last = _left(store, tmp_path)
    assert [track.lost for track in adapters._engine.tracks()] == [True]

    adapters._sensors._frame = na.CameraFrame(na.stamp_ns(3, 0), jpeg)
    adapters._tick(last + 0.3)
    assert adapters._last_tick == last  # idle: 0.5 Hz, so that tick was not due


def _image(data: bytes, sec: int = 1):
    return SimpleNamespace(data=data, header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=0)))


def test_the_motion_gate_lifts_an_empty_room_off_the_idle_clock(store, tmp_path, monkeypatch):
    """RFC 4.6: nobody tracked is 0.5 Hz until the scene changes, and then 5 Hz
    for ten seconds. It is the brain's gate, run here on this node's own stream
    rather than duplicated as a second rule."""
    pytest.importorskip("cv2")
    from brain_client.perception import motion_gate

    clock = [1000.0]
    monkeypatch.setattr(motion_gate.time, "monotonic", lambda: clock[0])
    adapters = _adapters(store, tmp_path, frames=[[]])
    adapters._sensors.brain_active = True
    now = time.time()
    assert adapters._sensors.motion(now) is False

    for step in range(3):
        adapters._sensors._on_compressed_image(_image(_walking(step), sec=step + 1))
        clock[0] += motion_gate.MOTION_SAMPLE_SEC
    assert adapters._sensors.motion(now) is True

    adapters._tick(now)
    adapters._sensors._frame = na.CameraFrame(na.stamp_ns(9, 0), _jpeg())
    adapters._tick(now + 0.3)  # idle would have skipped this; the burst does not
    assert adapters._last_tick == now + 0.3


def test_a_still_room_never_opens_the_motion_burst(store, tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    from brain_client.perception import motion_gate

    clock = [1000.0]
    monkeypatch.setattr(motion_gate.time, "monotonic", lambda: clock[0])
    adapters = _adapters(store, tmp_path, frames=[[]])
    for step in range(4):
        adapters._sensors._on_compressed_image(_image(_jpeg(), sec=step + 1))
        clock[0] += motion_gate.MOTION_SAMPLE_SEC
    assert adapters._sensors.motion(time.time()) is False


def test_a_tick_the_engine_skipped_never_restamps_the_boxes_with_a_newer_frame(store, tmp_path):
    """RFC section 7: the stamp the brain pairs on must name the frame the boxes
    were measured on. The node's sampling clock and the engine's detect clock are
    two clocks; only the engine knows which frame it actually looked at."""
    adapters, jpeg, last = _left(store, tmp_path)

    adapters._sensors._frame = na.CameraFrame(na.stamp_ns(9, 0), jpeg)
    adapters._tick(last + 2.05)  # due for the node, and the engine skips nothing
    measured = json.loads(na.String.call_args.kwargs["data"])["frame_stamp_ns"]
    assert measured == "9000000000"

    adapters._engine._last_detect = last + 100.0  # the engine will skip the next frame
    adapters._sensors._frame = na.CameraFrame(na.stamp_ns(10, 0), jpeg)
    adapters._tick(last + 4.1)
    assert json.loads(na.String.call_args.kwargs["data"])["frame_stamp_ns"] == "9000000000"


def test_an_idle_node_still_latches_its_health(store, tmp_path):
    adapters = _adapters(store, tmp_path)
    adapters._sensors.brain_active = False  # the brain is deactivated and always_on is off

    adapters._tick(time.time())

    payload = json.loads(na.String.call_args.kwargs["data"])
    assert payload["people"] == []
    assert payload["health"]["camera"] == "unavailable"
    assert adapters._want_native is False  # nothing to look at, so the lazy topic stays unsubscribed


def _served(adapters, handler_name: str, **request):
    """One service call against the real handler, with the .srv's own fields."""
    response = SimpleNamespace(success=False, message="", json="", person_id="")
    return getattr(adapters, handler_name)(SimpleNamespace(**request), response)


def test_the_tag_counter_is_persisted_so_a_respawn_does_not_reissue_p1(store, tmp_path):
    """respawn=True brings this node back in two seconds with its latched
    snapshot still on the wire; a skill holding P1 must not be handed a
    different person under that tag."""
    adapters, _jpeg, _last = _left(store, tmp_path)
    assert adapters._engine.tracker.next_tag == 2

    adapters._store_tick()
    assert PeopleStore(tmp_path / "people").next_tag() == 2


def test_the_get_service_answers_the_snapshot_and_the_roster(store, tmp_path):
    ana = enrol(store, "Ana")
    adapters = _adapters(store, tmp_path)

    plain = _served(adapters, "_svc_get", include_roster=False, include_thumbnails=False)
    assert plain.success and "roster" not in json.loads(plain.json)

    answer = json.loads(_served(adapters, "_svc_get", include_roster=True, include_thumbnails=True).json)
    assert answer["schema"] == 1  # the snapshot rides along even before the first tick
    assert answer["health"]["camera"] == "unavailable"
    assert [row["person_id"] for row in answer["roster"]] == [ana]
    assert answer["capacity_full"] is False


def test_the_rename_service_binds_a_name_to_a_live_tag(store, tmp_path):
    cv2 = pytest.importorskip("cv2")
    adapters = _adapters(store, tmp_path)
    ok, buffer = cv2.imencode(".jpg", np.full((480, 640, 3), 120, dtype=np.uint8))
    assert ok
    person_id = enrol(store)
    adapters._sensors.brain_active = True
    adapters._sensors._frame = na.CameraFrame(na.stamp_ns(1, 0), bytes(buffer))
    adapters._tick(time.time())
    # The engine's own resolution is not the point here: bind the tracked tag
    # to a real record the way the resolver would have.
    tracked = adapters.tracks()[0]
    with adapters._lock:
        adapters._tracks = (replace(tracked, identity=Identity(state=IdentityState.FAMILIAR, person_id=person_id)),)

    answer = _served(adapters, "_svc_rename", who="P1", name=" Ana ", source="app")
    assert answer.success and answer.person_id == person_id
    assert store.name_of(person_id) == "Ana"


def test_renaming_an_expired_tag_fails_with_something_the_robot_can_say(store, tmp_path):
    adapters = _adapters(store, tmp_path)
    answer = _served(adapters, "_svc_rename", who="P7", name="Ana", source="app")
    assert not answer.success and answer.person_id == ""
    assert "P7" in answer.message and "in view" in answer.message


def test_the_forget_service_tombstones_the_person(store, tmp_path):
    ana = enrol(store, "Ana")
    adapters = _adapters(store, tmp_path)
    answer = _served(adapters, "_svc_forget", who=ana)
    assert answer.success
    assert store.person_ids() == []
    assert store.is_tombstoned(ana)
    assert not _served(adapters, "_svc_forget", who=ana).success  # the id is gone for good


def test_forgetting_someone_reaches_the_work_the_scribe_still_has_in_flight(store, tmp_path):
    """RFC section 10: deletion removes the caches too. A window queued by an
    outage still carries their transcript, and would be spent on Gemini the
    moment the connection came back."""
    from brain_client.people.scribe import Scribe, Speaker, TagView, Utterance, Window

    ana = enrol(store, "Ana")
    scribe = Scribe(store, None, model="m", queue_path=tmp_path / "queue.jsonl")
    seen = (TagView(tag="P1", state=IdentityState.KNOWN, person_id=ana, name="Ana"),)
    scribe.process(Window((Utterance("u_1", NOW, Speaker.USER, "I'm Ana", seen),)), NOW)
    assert scribe.queued == 1

    adapters = _adapters(store, tmp_path, scribe=scribe)
    assert _served(adapters, "_svc_forget", who=ana).success
    adapters._scribe_cycle(0.0)

    assert scribe.queued == 0


def test_the_collection_switch_is_stored_and_shows_in_the_snapshot(store, tmp_path):
    adapters = _adapters(store, tmp_path)
    assert _served(adapters, "_svc_set_collection", enabled=False).success
    assert store.collection_enabled() is False
    answer = json.loads(_served(adapters, "_svc_get", include_roster=True, include_thumbnails=False).json)
    assert answer["collection_enabled"] is False
    assert answer["capacity_full"] is False  # switched off is not full


def test_a_learned_name_is_shown_once_and_then_cleared(store, tmp_path):
    cv2 = pytest.importorskip("cv2")
    adapters = _adapters(store, tmp_path)
    ok, buffer = cv2.imencode(".jpg", np.full((480, 640, 3), 120, dtype=np.uint8))
    assert ok
    adapters._sensors.brain_active = True
    adapters._sensors._frame = na.CameraFrame(na.stamp_ns(1, 0), bytes(buffer))
    now = time.time()
    adapters._tick(now)
    adapters._apply_changes([Change(ChangeKind.NAME, "P1", None, 'P1 said "I\'m Ana" — P1 = Ana from here on')])

    adapters._sensors._frame = na.CameraFrame(na.stamp_ns(2, 0), bytes(buffer))
    adapters._tick(now + 1.0)
    assert json.loads(na.String.call_args.kwargs["data"])["people"][0]["learned"].endswith("from here on")

    adapters._sensors._frame = na.CameraFrame(na.stamp_ns(3, 0), bytes(buffer))
    adapters._tick(now + 2.0)
    assert json.loads(na.String.call_args.kwargs["data"])["people"][0]["learned"] is None
