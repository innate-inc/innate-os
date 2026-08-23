# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Memory recall as a capability: the SearchMemory action mapping, the SDK
accessor's begin/wait contract, and the result-image channel that carries a
skill's evidence frame back into the agent's events."""

from __future__ import annotations

import base64
import threading
from types import SimpleNamespace

from brain_client.brain.memory_search import SearchVerdict, verdict_text
from brain_client.brain.search_server import _to_result
from brain_client.core.state import BrainState, RunningSkill
from brain_client.memory.store import Memory
from brain_client.robot.spatial_memory import SpatialMemory
from brain_client.skills.registry import SkillRegistry
from brain_client.skills.runner import PrimitiveRunner
from brain_client.skills.types import SkillOutput, SkillResult

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"

FOUND = SearchVerdict(
    query="the kitchen",
    found=True,
    explanation="counter with a kettle",
    memory=Memory(id=3, x=1.5, y=-0.5, theta=1.57, stamp=1000.0),
    image=JPEG,
    latency_sec=1.2,
    cached=True,
)


# ---------- action result mapping ----------


def test_to_result_maps_a_found_verdict_completely():
    result = _to_result(FOUND)
    assert result.found and result.cached and result.error == ""
    assert (result.x, result.y, result.theta, result.seen_stamp) == (1.5, -0.5, 1.57, 1000.0)
    assert base64.b64decode(result.image_b64) == JPEG
    assert result.message == verdict_text(FOUND)
    assert "navigate_to_position" in result.message


def test_to_result_maps_errors_and_no_match():
    error = _to_result(SearchVerdict(query="x", found=False, error="network down"))
    assert not error.found and error.error == "network down" and error.image_b64 == ""
    assert "failed" in error.message
    no_match = _to_result(SearchVerdict(query="x", found=False, explanation="nothing like that"))
    assert not no_match.found and no_match.error == "" and no_match.seen_stamp == 0.0
    assert "nothing in the remembered views matches" in no_match.message


# ---------- the SDK accessor ----------


class FakeFuture:
    """A future whose callback fires immediately — the whole chain runs inline."""

    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value

    def add_done_callback(self, callback):
        callback(self)


def make_accessor(result_msg=None, *, accepted=True, server_up=True):
    accessor = SpatialMemory.__new__(SpatialMemory)
    accessor._logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    handle = SimpleNamespace(accepted=accepted, get_result_async=lambda: FakeFuture(SimpleNamespace(result=result_msg)))
    accessor._client = SimpleNamespace(
        wait_for_server=lambda timeout_sec: server_up,
        send_goal_async=lambda goal: FakeFuture(handle),
    )
    return accessor


def test_begin_delivers_a_typed_verdict_through_the_reader():
    result_msg = SimpleNamespace(
        found=True,
        message="Found it.",
        explanation="the counter",
        error="",
        x=1.5,
        y=-0.5,
        theta=1.57,
        seen_stamp=1000.0,
        image_b64=base64.b64encode(JPEG).decode(),
        latency_sec=1.2,
        cached=True,
    )
    reader = make_accessor(result_msg).begin("the kitchen")
    verdict = reader()
    assert verdict is not None and verdict.found and verdict.image == JPEG
    assert verdict.x == 1.5 and verdict.cached and verdict.message == "Found it."


def test_begin_unblocks_the_waiter_on_every_failure_path():
    rejected = make_accessor(accepted=False).begin("x")()
    assert rejected is not None and rejected.error == "memory search goal rejected"
    down = make_accessor(server_up=False).begin("x")()
    assert down is not None and "is the brain node running" in down.error
    # A reader with no verdict yet reads None — wait_for's contract.
    pending = SpatialMemory.__new__(SpatialMemory)
    pending._logger = SimpleNamespace(error=lambda *a: None)
    pending._client = SimpleNamespace(
        wait_for_server=lambda timeout_sec: True,
        send_goal_async=lambda goal: SimpleNamespace(add_done_callback=lambda cb: None),  # never resolves
    )
    assert pending.begin("x")() is None


# ---------- the result-image channel ----------


def test_skill_output_carries_an_optional_image():
    assert SkillOutput("done").image is None
    assert SkillOutput("found it", image=JPEG).image == JPEG


def make_runner(events: list):
    runner = PrimitiveRunner.__new__(PrimitiveRunner)
    state = BrainState()
    state.registry = SkillRegistry.from_metadata(
        [{"id": "innate-os/search_memory", "name": "search_memory", "type": "code"}]
    )
    state.primitive_running = RunningSkill(primitive_name="search_memory", skill_id="innate-os/search_memory")
    runner._state = state
    runner._goal_handle = SimpleNamespace()
    runner._generation = 0
    runner._slot_lock = threading.Lock()
    runner._system_claim = None
    runner._logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    runner._stop_robot = lambda: None
    runner._on_task_finished = lambda: None
    runner._chat = SimpleNamespace(publish_task_status=lambda **kwargs: None, emit=lambda *a, **k: None)
    runner.on_event = lambda status, name, detail=None, image=None: events.append((status, name, detail, image))
    return runner


def test_runner_decodes_the_result_image_into_the_brain_event():
    events: list = []
    runner = make_runner(events)
    result = SimpleNamespace(
        success=True,
        success_type=SkillResult.SUCCESS.value,
        skill_type="innate-os/search_memory",
        message="Found it.",
        image_b64=base64.b64encode(JPEG).decode(),
    )
    runner._on_result(SimpleNamespace(result=lambda: SimpleNamespace(result=result)), generation=0)
    ((status, name, detail, image),) = events
    assert status == "completed" and image == JPEG and detail == "Found it."


def test_runner_passes_no_image_when_the_result_has_none():
    events: list = []
    runner = make_runner(events)
    result = SimpleNamespace(
        success=True,
        success_type=SkillResult.SUCCESS.value,
        skill_type="innate-os/search_memory",
        message="nothing matched",
        image_b64="",
    )
    runner._on_result(SimpleNamespace(result=lambda: SimpleNamespace(result=result)), generation=0)
    ((status, _, _, image),) = events
    assert status == "completed" and image is None
