# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS boundary and immediate map-transition gating. Requires the built messages."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("rclpy")
from brain_messages.srv import MapNotes as MapNotesService  # noqa: E402

from brain_client.memory.note_bridge import NoteBridge  # noqa: E402
from brain_client.memory.notes import MapNotes  # noqa: E402
from brain_client.memory.recorder import MemoryRecorder  # noqa: E402


def test_bridge_handles_invalid_json_and_round_trips_store(tmp_path):
    identity = SimpleNamespace(map_name="home.yaml", fingerprint="abc")
    store = MapNotes(tmp_path, lambda: identity)
    published = []
    node = SimpleNamespace(
        create_publisher=lambda *a: SimpleNamespace(publish=published.append),
        create_service=lambda *a: None,
        create_timer=lambda *a: None,
    )
    bridge = NoteBridge(node, store)
    try:
        response = bridge.call(MapNotesService.Request(request="[]"), MapNotesService.Response())
        assert json.loads(response.response) == {"ok": False, "error": "INVALID_ARGUMENT"}
        response = bridge.call(
            MapNotesService.Request(request=json.dumps({"operation": "snapshot"})), MapNotesService.Response()
        )
        assert json.loads(response.response)["notes"] == []
        obs = store.observe((1, 2, 0), b"jpeg")
        result = store.execute(
            "write_map_note",
            {"title": "Mug", "text": "Table", "certainty": "observed", "observation_id": obs.id},
            ref=obs.map_ref,
            call_id="create",
            observation=obs,
        )
        note = result["note"]
        request = {
            "operation": "remove_map_note",
            "map_ref": obs.map_ref,
            "request_id": "remove",
            "arguments": {"note_id": note["id"], "expected_revision": 1},
        }
        response = bridge.call(MapNotesService.Request(request=json.dumps(request)), MapNotesService.Response())
        result = json.loads(response.response)
        assert result["ok"] and result["snapshot"]["notes"] == []
        assert json.loads(published[-1].data)["revision"] == 2
    finally:
        store.close()


def test_recorder_detaches_notes_before_its_next_periodic_tick():
    snapshot = SimpleNamespace(map_name="old.yaml")
    recorder = object.__new__(MemoryRecorder)
    recorder._store = SimpleNamespace(snapshot=lambda: snapshot)
    recorder._nav_mode = "navigation"
    recorder._map_name = "old.yaml"
    recorder._confident = lambda: True
    assert recorder.note_map_snapshot() is snapshot and recorder.can_anchor_note()
    recorder._map_name = "new.yaml"
    assert recorder.note_map_snapshot() is None and not recorder.can_anchor_note()
    snapshot.map_name = "new.yaml"
    assert recorder.note_map_snapshot() is snapshot
    recorder._nav_mode = "mapping"
    assert recorder.note_map_snapshot() is None and not recorder.can_anchor_note()
