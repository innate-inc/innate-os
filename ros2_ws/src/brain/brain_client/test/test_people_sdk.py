# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The people SDK: the snapshot parse, the questions a skill asks of it, and the
two skills the agent gets.

ROS-free. ``parse_snapshot`` and ``PeopleView`` import nothing but the shared
people vocabulary, and the two skills run against fake ``people``/``mobility``
objects with the real cancel latch. The skill files themselves must import
their feed types for real — the framework resolves those annotations at
runtime to decide what to inject — so the ROS packages that import chain pulls
in are fabricated below when they are missing; a real installation wins.
"""

import importlib.abc
import importlib.util
import json
import logging
import sys
import uuid
from collections.abc import MutableSequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from brain_client.people.sdk_parse import (
    EMPTY_VIEW,
    SNAPSHOT_FRESH_SEC,
    PeopleView,
    PersonInView,
    RecentPerson,
    parse_snapshot,
)
from brain_client.people.types import IdentityState

# The ROS packages `from innate import Mobility, People, Skill` reaches through.
_ROS_ROOTS = frozenset(
    {
        "brain_messages",
        "geometry_msgs",
        "mars_msgs",
        "nav2_simple_commander",
        "rclpy",
        "sensor_msgs",
        "std_msgs",
        "std_srvs",
    }
)


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


# workspace/ is an import root on the robot (skills.workspace_import.ensure_import_roots
# puts it there); a bare pytest run has to say so itself.
_WORKSPACE = str(Path(__file__).resolve().parents[5] / "workspace")
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

# Appended, never inserted: where ROS is installed its own finder answers first.
_STUB_FINDER = _StubFinder()
sys.meta_path.append(_STUB_FINDER)

from innate_skills.people import approach_person as approach  # noqa: E402 — needs the stubs above
from innate_skills.people.approach_person import ApproachPerson  # noqa: E402 — needs the stubs above
from innate_skills.people.forget_person import ForgetPerson  # noqa: E402 — needs the stubs above

from brain_client.robot.people import People  # noqa: E402 — needs the stubs above
from brain_client.skills.types import (  # noqa: E402 — needs the stubs above
    InterfaceType,
    SkillCancelled,
    SkillFailed,
    SkillOutput,
)

# The stubs live exactly as long as the imports above: every other test module
# in this session must go on finding ROS missing, because it is.
sys.meta_path.remove(_STUB_FINDER)
for _stubbed in [name for name, module in sys.modules.items() if isinstance(module, _StubModule)]:
    del sys.modules[_stubbed]

NOW = 1788818400.12

SNAPSHOT = {
    "schema": 1,
    "stamp": NOW,
    "frame_stamp_ns": "1788818400123456789",
    "image_size": [640, 480],
    "health": {"camera": "ok", "native": "unavailable", "face_model": "ok", "gpu": "none"},
    "collection_enabled": True,
    "attention": {"tag": "P4", "text": "trying to see P4's face", "head_bbox": [120, 400, 220, 480]},
    "people": [
        {
            "tag": "P3",
            "person_id": "person_7f92a1b3",
            "name": "Theo",
            "state": "known",
            "evidence": ["face"],
            "confidence": 0.91,
            "bbox": [100, 300, 930, 560],
            "head_bbox": [100, 380, 260, 480],
            "range_m": 1.8,
            "bearing_deg": -4.0,
            "tracked_sec": 41.2,
            "lost": False,
            "description": "Man, 30s, glasses.",
            "hint": None,
            "learned": None,
            "digest": {"facts": [{"id": "f_01", "text": "likes pasta"}], "encounters": 41},
        },
        {
            "tag": "P4",
            "person_id": "person_0c1d2e3f",
            "name": "Ana",
            "state": "possible",
            "bbox": [200, 10, 900, 260],
            "head_bbox": None,
            "range_m": 2.5,
            "bearing_deg": 12.0,
            "tracked_sec": 6.0,
            "lost": False,
        },
        {"tag": "P9", "person_id": "person_deadbeef", "name": "Zoe", "state": "known", "lost": True, "range_m": 0.9},
    ],
    "recent": [{"person_id": "person_11aa22bb", "name": "Marc", "last_seen": NOW - 3600.0, "map": "home"}],
}


SNAPSHOT_TEXT = json.dumps(SNAPSHOT)


def person(range_m: float | None, bearing_deg: float = 0.0, tag: str = "P3", name: str | None = "Theo"):
    return PersonInView(
        tag=tag,
        person_id="person_7f92a1b3",
        name=name,
        state=IdentityState.KNOWN,
        range_m=range_m,
        bearing_deg=bearing_deg,
    )


# ---------- the parse ----------


def test_parse_reads_a_whole_snapshot():
    view = parse_snapshot(SNAPSHOT_TEXT)
    assert view.stamp == NOW
    theo, ana = view.people
    assert (theo.tag, theo.person_id, theo.name) == ("P3", "person_7f92a1b3", "Theo")
    assert theo.state is IdentityState.KNOWN
    # per-mille ints on the wire, normalized (ymin, xmin, ymax, xmax) in the SDK
    assert theo.bbox == (0.1, 0.3, 0.93, 0.56)
    assert theo.head_bbox == (0.1, 0.38, 0.26, 0.48)
    assert (theo.range_m, theo.bearing_deg, theo.tracked_sec) == (1.8, -4.0, 41.2)
    assert theo.description == "Man, 30s, glasses."
    assert ana.state is IdentityState.POSSIBLE and ana.head_bbox is None
    (marc,) = view.recent
    assert (marc.person_id, marc.name, marc.map_name) == ("person_11aa22bb", "Marc", "home")


def test_parse_keeps_the_frame_stamp_the_boxes_were_measured_on():
    """The stamp a mutation quotes back as the snapshot it was decided on."""
    assert parse_snapshot(SNAPSHOT_TEXT).frame_stamp_ns == "1788818400123456789"
    assert parse_snapshot(json.dumps({"stamp": NOW, "people": []})).frame_stamp_ns == ""


def test_parse_drops_lost_tracks():
    """A lost track rides the snapshot so the brain can still talk about the
    person; nobody is in view any more, so no skill may act on it."""
    view = parse_snapshot(SNAPSHOT_TEXT)
    assert [p.tag for p in view.people] == ["P3", "P4"]
    assert view.find("Zoe") is None and view.find("P9") is None


@pytest.mark.parametrize("text", ["", "   ", "not json at all", "null", "[]", "3", '{"people": 7}'])
def test_parse_returns_an_empty_view_for_anything_that_is_not_a_snapshot(text: str):
    assert parse_snapshot(text) == EMPTY_VIEW


def test_parse_keeps_the_fields_it_understands_when_the_rest_is_junk():
    view = parse_snapshot(
        json.dumps(
            {
                "stamp": "not a number",
                "people": [
                    {"tag": "P1"},
                    {"tag": "P2", "range_m": "far", "bearing_deg": None, "bbox": [1, 2, 3], "state": "asleep"},
                    {"tag": "  ", "range_m": 1.0},
                    {"person_id": "person_00000000"},
                    "P5",
                ],
                "recent": [{"name": "nobody"}, {"person_id": "person_11aa22bb", "last_seen": None}],
            }
        )
    )
    assert view.stamp == 0.0
    lone, junked = view.people
    assert lone == PersonInView(tag="P1")
    assert (junked.range_m, junked.bearing_deg, junked.bbox) == (None, None, None)
    # a state this SDK has never heard of is not an identity claim it can act on
    assert junked.state is IdentityState.UNKNOWN
    assert view.recent == (RecentPerson(person_id="person_11aa22bb", last_seen=0.0),)


def test_parse_ignores_schema_drift():
    view = parse_snapshot(
        json.dumps(
            {
                "schema": 7,
                "stamp": NOW,
                "mood": "curious",
                "people": [{"tag": "P3", "range_m": 2.0, "gait": "brisk"}],
            }
        )
    )
    assert [p.tag for p in view.people] == ["P3"] and view.people[0].range_m == 2.0


# ---------- the questions a skill asks ----------


def test_in_view_is_nearest_first_and_ranks_a_person_without_a_range_last():
    view = PeopleView(stamp=NOW, people=(person(None, tag="P1"), person(3.0, tag="P2"), person(1.0, tag="P3")))
    assert [p.tag for p in view.in_view()] == ["P3", "P2", "P1"]


def test_find_matches_a_tag_an_id_or_a_name_case_insensitively():
    view = parse_snapshot(SNAPSHOT_TEXT)
    theo, ana = view.people
    assert view.find("P3") == theo
    assert view.find("person_0c1d2e3f") == ana
    assert view.find("  ana  ") == ana and view.find("ANA") == ana
    assert view.find("Marc") is None  # recently seen is not in view
    assert view.find("") is None and view.find("P7") is None


def test_find_prefers_the_nearer_person_when_two_share_a_name():
    far, near = person(4.0, tag="P1", name="Alex"), person(1.5, tag="P2", name="Alex")
    assert PeopleView(stamp=NOW, people=(far, near)).find("alex") == near


def test_recently_seen_windows_the_digest_and_counts_whoever_is_in_view():
    view = parse_snapshot(SNAPSHOT_TEXT)
    within_two_hours = view.recently_seen(120.0, NOW)
    assert [entry.name for entry in within_two_hours] == ["Theo", "Ana", "Marc"]
    assert within_two_hours[0].last_seen == NOW
    assert [entry.name for entry in view.recently_seen(10.0, NOW)] == ["Theo", "Ana"]
    assert view.recently_seen(10.0, NOW + 3600.0) == []


def test_recently_seen_keeps_one_entry_per_person():
    view = PeopleView(
        stamp=NOW,
        people=(person(1.0),),
        recent=(RecentPerson(person_id="person_7f92a1b3", name="Theo", last_seen=NOW - 600.0),),
    )
    (theo,) = view.recently_seen(60.0, NOW)
    assert theo.last_seen == NOW  # in view now, not ten minutes ago


def test_a_snapshot_is_only_fresh_while_the_engine_keeps_confirming_it():
    view = parse_snapshot(SNAPSHOT_TEXT)
    assert view.is_fresh(NOW) and view.is_fresh(NOW + SNAPSHOT_FRESH_SEC)
    assert not view.is_fresh(NOW + SNAPSHOT_FRESH_SEC + 0.1)
    assert not EMPTY_VIEW.is_fresh(NOW)


# ---------- the mutations a skill makes ----------


def _people_interface() -> People:
    """The real interface around a mock node: every ROS call is recorded."""
    node = MagicMock()
    node.create_client.side_effect = lambda *args, **kwargs: MagicMock()
    people = People(node, logging.getLogger("test_people_sdk"))
    people._on_snapshot(SimpleNamespace(data=SNAPSHOT_TEXT))
    return people


def test_a_mutation_says_which_snapshot_it_was_decided_on():
    """RFC section 8: the node refuses a tag issued after the snapshot the
    caller was looking at rather than acting on whoever holds it now."""
    people = _people_interface()

    assert people.forget("P3")[0]
    forget = people._forget_client.call_async.call_args.args[0]
    assert forget.who == "P3"
    assert forget.decided_on_stamp_ns == SNAPSHOT["frame_stamp_ns"]

    people.rename("P3", "Theo")
    rename = people._rename_client.call_async.call_args.args[0]
    assert rename.decided_on_stamp_ns == SNAPSHOT["frame_stamp_ns"]


def test_every_mutation_carries_its_own_retry_key():
    people = _people_interface()
    request = people._forget_client.call_async

    people.forget("P3")
    first = request.call_args.args[0].idempotency_key
    people.forget("P3")
    second = request.call_args.args[0].idempotency_key
    assert uuid.UUID(first).version == 4 and first != second


def test_a_mutation_before_the_first_snapshot_claims_no_decision():
    node = MagicMock()
    node.create_client.side_effect = lambda *args, **kwargs: MagicMock()
    people = People(node, logging.getLogger("test_people_sdk"))
    people.forget("Ana")
    assert people._forget_client.call_async.call_args.args[0].decided_on_stamp_ns == ""


# ---------- the fakes the skills run against ----------


class FakePeople:
    """The People interface as a skill sees it: ``find`` answers from a script,
    repeating the last answer once the script runs out."""

    def __init__(self, script=(), *, forget=(True, "Done, I've forgotten Theo.")):
        self._script = list(script)
        self._forget = forget
        self.asked: list[str] = []
        self.forgotten: list[str] = []

    def find(self, name_or_tag: str):
        self.asked.append(name_or_tag)
        if len(self._script) > 1:
            return self._script.pop(0)
        return self._script[0] if self._script else None

    def forget(self, who: str) -> tuple[bool, str]:
        self.forgotten.append(who)
        return self._forget


class FakeMobility:
    def __init__(self):
        self.commands: list[tuple[float, float]] = []
        self.stops = 0

    def send_cmd_vel(self, linear_x: float = 0.0, angular_z: float = 0.0, duration: float | None = None) -> None:
        self.commands.append((linear_x, angular_z))

    def stop(self) -> None:
        self.stops += 1


def build(skill_class, people, mobility=None):
    skill = skill_class(logging.getLogger("test_people_sdk"))
    skill.inject_interface(InterfaceType.PEOPLE, people)
    if mobility is not None:
        skill.inject_interface(InterfaceType.MOBILITY, mobility)
    return skill


# ---------- approach_person ----------


def test_servo_is_zero_in_the_deadband_and_clamped_outside_it():
    assert approach.servo(0.1, 1.0, 0.05, 0.5, 0.15) == 0.0
    assert approach.servo(2.0, 1.0, 0.05, 0.5, 0.15) == 0.5
    assert approach.servo(-2.0, 1.0, 0.05, 0.5, 0.15) == -0.5
    assert approach.servo(0.2, 0.01, 0.05, 0.5, 0.15) == 0.05  # below v_min, still moves
    assert approach.servo(-0.2, 0.01, 0.05, 0.5, 0.15) == -0.05


def test_approach_person_drives_until_the_person_is_in_the_band_and_reports_the_range():
    parked = person(approach.TARGET_RANGE_M)
    people = FakePeople([person(3.0), person(3.0), person(2.0), parked])
    mobility = FakeMobility()
    output = build(ApproachPerson, people, mobility).execute(who="Theo")

    assert isinstance(output, SkillOutput) and output.ok
    assert f"{approach.TARGET_RANGE_M:.2f} m" in output.message and "Theo" in output.message
    assert output.data.range_m == approach.TARGET_RANGE_M and output.data.tag == "P3"
    # resolved by name once, then followed by tag
    assert people.asked[0] == "Theo" and set(people.asked[1:]) == {"P3"}
    assert mobility.commands and all(0.0 < linear <= approach.MAX_LINEAR for linear, _ in mobility.commands)
    assert mobility.stops >= approach.ARRIVE_FRAMES


def test_approach_person_turns_toward_the_person_before_driving_at_them():
    people = FakePeople([person(3.0, bearing_deg=40.0)])
    mobility = FakeMobility()
    skill = build(ApproachPerson, people, mobility)
    skill._cancelled = True  # the latch stops the loop after exactly one command

    with pytest.raises(SkillCancelled):
        skill.execute(who="P3")
    ((linear, angular),) = mobility.commands
    assert linear == 0.0 and angular > 0.0  # positive bearing is to the left


def test_approach_person_fails_when_nobody_matches():
    with pytest.raises(SkillFailed, match="can't see Ana"):
        build(ApproachPerson, FakePeople([]), FakeMobility()).execute(who="Ana")


def test_approach_person_gives_up_once_the_person_has_been_gone_too_long(monkeypatch):
    monkeypatch.setattr(approach, "LOST_GRACE_SEC", 0.05)
    mobility = FakeMobility()
    with pytest.raises(SkillFailed, match="Lost Theo"):
        build(ApproachPerson, FakePeople([person(3.0), None]), mobility).execute(who="Theo")
    assert mobility.stops >= 1


def test_approach_person_says_so_when_it_can_see_the_person_but_not_their_range(monkeypatch):
    monkeypatch.setattr(approach, "LOST_GRACE_SEC", 0.05)
    with pytest.raises(SkillFailed, match="can't tell how far"):
        build(ApproachPerson, FakePeople([person(3.0), person(None)]), FakeMobility()).execute(who="Theo")


def test_approach_person_gives_up_when_it_cannot_close_the_distance(monkeypatch):
    monkeypatch.setattr(approach, "TIMEOUT_SEC", 0.2)
    with pytest.raises(SkillFailed, match="Gave up approaching Theo"):
        build(ApproachPerson, FakePeople([person(3.0)]), FakeMobility()).execute(who="P3")


def test_approach_person_unwinds_and_brakes_when_a_stop_lands():
    mobility = FakeMobility()
    skill = build(ApproachPerson, FakePeople([person(3.0)]), mobility)
    skill._cancelled = True  # the latch a Stop sets, before the first sleep

    with pytest.raises(SkillCancelled):
        skill.execute(who="P3")
    assert mobility.stops == 1 and len(mobility.commands) == 1


def test_lost_message_says_which_kind_of_lost():
    assert "left or moved out of sight" in approach.lost_message("Theo", seen=False)
    assert "how far away" in approach.lost_message("Theo", seen=True)


# ---------- forget_person ----------


def test_forget_person_addresses_the_record_and_returns_the_confirmation():
    people = FakePeople([person(1.5)])
    assert build(ForgetPerson, people).execute(who="Theo") == "Done, I've forgotten Theo."
    assert people.forgotten == ["person_7f92a1b3"]


def test_forget_person_falls_back_to_the_tag_for_someone_with_no_record_yet():
    people = FakePeople([PersonInView(tag="P5")])
    build(ForgetPerson, people).execute(who="P5")
    assert people.forgotten == ["P5"]


def test_forget_person_passes_an_unseen_name_straight_to_the_node():
    people = FakePeople([], forget=(True, "Forgotten."))
    assert build(ForgetPerson, people).execute(who="Ana") == "Forgotten."
    assert people.forgotten == ["Ana"]


def test_forget_person_fails_with_the_reason_the_node_gave():
    people = FakePeople([], forget=(False, "I don't know anyone called Ana."))
    with pytest.raises(SkillFailed, match="don't know anyone called Ana"):
        build(ForgetPerson, people).execute(who="Ana")


def test_forget_person_still_says_something_when_the_node_says_nothing():
    with pytest.raises(SkillFailed, match="don't know anyone called Ana"):
        build(ForgetPerson, FakePeople([], forget=(False, ""))).execute(who="Ana")
    assert build(ForgetPerson, FakePeople([], forget=(True, ""))).execute(who="Ana") == "Done — I've forgotten Ana."
