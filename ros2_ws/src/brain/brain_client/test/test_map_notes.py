# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Scratchpad persistence and real Astra turn integration; no network or ROS."""

import json
from dataclasses import replace
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import test_local_brain
from test_local_brain import run_turn
from test_openai_context import call_item, completed

from brain_client.brain.context import ToolCall
from brain_client.core.state import RunningSkill
from brain_client.memory.note_map import NoteMapRenderer
from brain_client.memory.notes import NOTE_TOOL_NAMES, MapNotes

agent_factory = test_local_brain.agent_factory


@pytest.fixture
def pad(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    cv2.imwrite(str(maps / "home.pgm"), np.full((100, 120), 254, np.uint8))
    (maps / "home.yaml").write_text("image: home.pgm\nresolution: 0.1\norigin: [-2, -2, 0]\n")
    identity = SimpleNamespace(map_name="home.yaml", fingerprint="a" * 64)
    store = MapNotes(tmp_path, lambda: identity, NoteMapRenderer(tmp_path))
    yield store, identity
    store.close()


def write(store, observation=None, call_id="create", **overrides):
    observation = observation or store.observe((1, 1, 0), b"camera-evidence")
    args = {
        "note_id": None,
        "expected_revision": None,
        "title": "Red mug",
        "text": "On the kitchen table",
        "certainty": "observed",
        "observation_id": observation.id,
        **overrides,
    }
    return (
        store.execute("write_map_note", args, ref=observation.map_ref, call_id=call_id, observation=observation),
        args,
        observation,
    )


def test_persistence_retries_conflicts_evidence_and_removal(pad, tmp_path):
    store, identity = pad
    created, args, obs = write(store)
    assert created["ok"]
    note = created["note"]
    assert store.execute("write_map_note", args, ref=obs.map_ref, call_id="create", observation=obs) == created
    assert len(store.snapshot()["notes"]) == 1
    assert (
        store.execute("write_map_note", {**args, "text": "different"}, ref=obs.map_ref, call_id="create")["error"]
        == "CALL_ID_REUSED"
    )
    second = MapNotes(tmp_path, lambda: identity, NoteMapRenderer(tmp_path))
    try:
        assert second.snapshot()["notes"] == [note]
        update = {
            **args,
            "note_id": note["id"],
            "expected_revision": 1,
            "observation_id": None,
            "text": "Moved to the left side",
        }
        result = second.execute("write_map_note", update, ref=obs.map_ref, call_id="edit")
        assert result["note"]["revision"] == 2
        assert result["note"]["x"] == 1
        assert store.execute("write_map_note", update, ref=obs.map_ref, call_id="stale")["error"] == "REVISION_CONFLICT"
        read = store.execute(
            "read_map_notes", {"note_ids": [note["id"]], "include_evidence": True}, ref=obs.map_ref, call_id="read"
        )
        assert read["images"] == [(note["id"], b"camera-evidence")]
        removal = {"note_id": note["id"], "expected_revision": 2}
        removed = store.execute("remove_map_note", removal, ref=obs.map_ref, call_id="delete")
        assert removed["ok"] and store.snapshot()["notes"] == []
        assert store.execute("remove_map_note", removal, ref=obs.map_ref, call_id="delete") == removed
        assert (
            second.execute("write_map_note", update, ref=obs.map_ref, call_id="resurrect")["error"] == "NOTE_NOT_FOUND"
        )
        assert note["id"] not in store.context(obs)[0]
    finally:
        second.close()


def test_map_changes_invalid_anchors_and_cancelled_write_do_not_commit(pad):
    store, identity = pad
    obs = store.observe((1, 1, 0), b"evidence")
    assert write(store, replace(obs, monotonic=obs.monotonic - 121))[0]["error"] == "OBSERVATION_EXPIRED"
    assert write(store, replace(obs, pose=None))[0]["error"] == "OBSERVATION_EXPIRED"
    assert write(store, replace(obs, pose=(float("nan"), 1, 0)))[0]["error"] == "INVALID_ANCHOR"
    assert write(store, obs, map_point=[1, 1])[0]["error"] == "INVALID_ARGUMENT"
    assert write(store, obs, unexpected=True)[0]["error"] == "INVALID_ARGUMENT"
    identity.fingerprint = "b" * 64
    assert write(store, obs)[0]["error"] == "MAP_CHANGED"
    identity.fingerprint = "a" * 64
    _, args, _ = write(store, obs, title="rollback", call_id="first")
    args["note_id"] = None
    checks = iter([True, False])
    result = store.execute(
        "write_map_note", args, ref=obs.map_ref, call_id="cancel", observation=obs, valid=lambda: next(checks)
    )
    assert result["error"] == "TURN_CANCELLED"
    assert len(store.snapshot()["notes"]) == 1
    identity.map_name = ".mapping"
    assert store.snapshot()["notes"] == []
    assert store.context(store.observe(None, None))[1] is None


def test_operator_points_rotation_context_budget_and_render_cache(pad):
    store, _ = pad
    obs = store.observe((1, 1, 0), b"evidence")
    args = {"title": "Door", "text": "Entry", "certainty": "uncertain", "map_point": [3, 2]}
    result = store.execute("write_map_note", args, ref=obs.map_ref, call_id="operator", operator=True)
    assert result["note"]["anchor"] == "map_point"
    assert not result["note"]["has_evidence"]
    assert (
        store.execute(
            "write_map_note", {**args, "map_point": [999, 2]}, ref=obs.map_ref, call_id="outside", operator=True
        )["error"]
        == "INVALID_ANCHOR"
    )
    for i in range(8):
        write(store, obs, call_id=f"write{i}", text="x" * 800)
    text, image = store.context(obs)
    payload = json.loads(text.split("\n")[1])
    assert len(payload["notes"]) == 5 and payload["total"] == 9
    assert all(len(n["text"]) <= 180 for n in payload["notes"])
    assert cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR) is not None
    assert store.context(obs)[1] is image
    assert store.context(replace(obs, pose=(2, 1, 0)))[1] != image
    grid = (np.zeros((100, 100), np.uint8), 0.1, (0, 0, np.pi / 2))
    assert store.renderer._pixel((-1, 2), grid) == pytest.approx((20, 90))


def test_astra_turn_gets_fresh_text_and_image_and_tools_bypass_skill_slot(pad, agent_factory, monkeypatch):
    store, _ = pad
    requests = []

    def transport(model, body):
        requests.append(body)
        assert body["service_tier"] == "priority"
        system = next(item for item in body["input"] if item.get("role") == "developer")["content"][0]["text"]
        assert "Quietly maintain your map notes, including while idle" in system
        assert all(name in system for name in NOTE_TOOL_NAMES)
        assert system.index("Map scratchpad:") < system.index("Your directive:")
        live = next(
            item
            for item in body["input"]
            if item.get("role") == "user"
            and any("Current map scratchpad" in p.get("text", "") for p in item.get("content", []))
        )
        parts = live["content"]
        data = json.loads(parts[0]["text"].split("\n")[1])
        assert any(p["type"] == "input_image" for p in parts)
        if len(requests) == 1:
            assert data["notes"] == []
            return [
                completed(
                    call_item(
                        "note1",
                        "write_map_note",
                        json.dumps(
                            {
                                "note_id": None,
                                "expected_revision": None,
                                "title": "Blue bowl",
                                "text": "On the shelf",
                                "certainty": "observed",
                                "observation_id": data["observation_id"],
                            }
                        ),
                    )
                )
            ]
        assert data["notes"][0]["title"] == "Blue bowl"
        return [completed(call_item())]

    from brain_client.brain import agent as module

    monkeypatch.setattr(module, "pick_openai_transport", lambda proxy: (transport, "test"))
    agent, state = agent_factory(
        brain_provider="openai",
        openai_model="gpt-6-astra",
        openai_reasoning_effort="low",
        openai_service_tier="priority",
    )
    agent._map_notes = store
    agent._pose.current_pose_xyt = lambda: (1, 1, 0)
    state.primitive_running = RunningSkill("wave", "local/wave")
    run_turn(agent)
    assert len(requests) == 1 and store.snapshot()["notes"][0]["title"] == "Blue bowl"
    assert state.primitive_running.primitive_name == "wave"
    declarations = {t["name"]: t for t in requests[0]["tools"]}
    for name in NOTE_TOOL_NAMES:
        assert declarations[name]["strict"] is True
        schema = declarations[name]["parameters"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    run_turn(agent)
    assert len(requests) == 2 and agent.error_streak == 0
    assert "Current map scratchpad" not in json.dumps(agent._context._history)
    note = store.snapshot()["notes"][0]
    evidence = json.loads(
        agent._dispatch(ToolCall("read_map_notes", {"note_ids": [note["id"]], "include_evidence": True}, "read1"))
    )
    assert evidence["ok"] and agent._events[-1].image
    state.is_brain_active = False
    assert "deactivating" in agent._dispatch(
        ToolCall("remove_map_note", {"note_id": note["id"], "expected_revision": 1}, "delete1")
    )
    assert len(store.snapshot()["notes"]) == 1


@pytest.mark.parametrize("provider,has_store", [("gemini", True), ("openai", False)])
def test_scratchpad_prompt_is_absent_when_note_tools_are_unavailable(
    pad, agent_factory, monkeypatch, provider, has_store
):
    from brain_client.brain import agent as module

    requests = []

    def transport(model, body):
        requests.append(body)
        if provider == "openai":
            return [completed(call_item())]
        return [test_local_brain.model_response(test_local_brain.call_part("wait", {}))]

    monkeypatch.setattr(module, "pick_openai_transport", lambda proxy: (transport, "test"))
    agent, _ = agent_factory(brain_provider=provider, openai_model="gpt-6-astra", openai_reasoning_effort="low")
    agent._map_notes = pad[0] if has_store else None
    if provider == "gemini":
        agent._context._transport = transport
    run_turn(agent)
    assert len(requests) == 1
    assert all(name not in json.dumps(requests[0]) for name in NOTE_TOOL_NAMES)
    assert "Map scratchpad:" not in json.dumps(requests[0])


def test_geometry_change_namespaces_notes_and_near_query_is_bounded(pad, tmp_path):
    store, _ = pad
    created, _, obs = write(store)
    args = {"near": {"x": 1, "y": 1, "radius_m": 0.5}}
    assert store.execute("read_map_notes", args, ref=obs.map_ref, call_id="read")["notes"] == [created["note"]]
    assert (
        store.execute("read_map_notes", {"near": {"x": 5, "y": 5, "radius_m": 0.5}}, ref=obs.map_ref, call_id="read")[
            "notes"
        ]
        == []
    )
    assert (
        store.execute("read_map_notes", {"near": {"x": True, "y": 0, "radius_m": 1}}, ref=obs.map_ref, call_id="read")[
            "error"
        ]
        == "INVALID_ARGUMENT"
    )
    (tmp_path / "maps" / "home.yaml").write_text("image: home.pgm\nresolution: 0.1\norigin: [0, 0, 1]\n")
    assert store.snapshot()["notes"] == []
    assert store.execute("read_map_notes", {}, ref=obs.map_ref, call_id="read")["error"] == "MAP_CHANGED"
