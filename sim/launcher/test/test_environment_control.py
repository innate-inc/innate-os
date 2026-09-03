# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""One lifecycle scenario covering switch, rollback, recovery, and supervised shutdown."""

import contextlib
import json
import sys
import uuid
from pathlib import Path

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))

import environment_control as control  # noqa: E402


class FakePack:
    def __init__(self, environment_id: str):
        self.id = environment_id
        self.display_name = environment_id.title()
        self.fingerprint = f"fingerprint-{environment_id}"

    def validate_assets(self):
        return None


def test_switch_mailbox_is_transactional_and_restart_fails_closed(monkeypatch, tmp_path):
    events: list[str] = []
    packs = {name: FakePack(name) for name in ("apartment", "town")}
    runtime_active = {"pack": packs["apartment"]}
    failures: dict[str, object] = {
        "target_ros": False,
        "rollback_world": False,
        "identity": True,
    }
    config: dict[str, object] = {"sim_repo": tmp_path / "sim", "os_repo": tmp_path}

    def load(_repo, environment_id, *, validate_assets=False):
        events.append(f"load:{environment_id}:{validate_assets}")
        return packs[environment_id]

    def select(selected_config, environment_id):
        pack = packs[environment_id]
        selected_config["environment"] = pack
        selected_config["environment_id"] = pack.id
        events.append(f"select:{pack.id}")
        return pack

    def activate(pack):
        runtime_active["pack"] = pack
        events.append(f"activate:{pack.id}")
        return True

    def world(selected_config):
        environment_id = selected_config["environment"].id
        events.append(f"world:{environment_id}")
        if failures["rollback_world"] and environment_id == "town":
            raise control.StackError("rollback world failed")
        return "host:8799", True

    def ros(selected_config, _env, **_kwargs):
        environment_id = selected_config["environment"].id
        events.append(f"ros:{environment_id}")
        if failures["target_ros"] and environment_id == "apartment":
            raise control.StackError("target ROS failed\r\x1b[31m")

    current = tmp_path / "control/requests/current.json"
    catalog = tmp_path / "control/status/catalog.json"
    catalog_writes: list[dict[str, object]] = []
    terminal_published_with_mailbox: list[bool] = []
    current.parent.mkdir(parents=True)
    catalog.parent.mkdir(parents=True)
    real_write_json = control._write_json

    def capture_catalog(path, payload):
        if path == catalog:
            catalog_writes.append(payload)
            switch = payload.get("switch")
            if isinstance(switch, dict) and switch.get("state") in {"ready", "failed"}:
                terminal_published_with_mailbox.append(current.exists())
        real_write_json(path, payload)

    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH", current)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_CATALOG_PATH", catalog)
    monkeypatch.setattr(control, "_write_json", capture_catalog)
    monkeypatch.setattr(control, "load_environment_pack", load)
    monkeypatch.setattr(control, "load_active_environment", lambda *_args, **_kwargs: runtime_active["pack"])
    monkeypatch.setattr(control, "select_environment", select)
    monkeypatch.setattr(control, "activate_environment", activate)
    monkeypatch.setattr(control, "_installed_packs", lambda _config: packs)
    monkeypatch.setattr(
        control,
        "collect_os_process_status",
        lambda _config: {"os_running": True, "os_session_running": True},
    )
    monkeypatch.setattr(control, "stop_os_session", lambda _config: events.append("stop:ros"))
    monkeypatch.setattr(control, "stop_world_server_and_wait", lambda: events.append("stop:world"))
    monkeypatch.setattr(control, "ensure_world_server", world)
    monkeypatch.setattr(control, "ensure_os_container", ros)
    monkeypatch.setattr(control, "build_os_env", lambda _config: tmp_path / "os.env")
    monkeypatch.setattr(control, "wait_for_os_runtime_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(control, "wait_for_virtual_mars", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(control, "ros_environment_is_current", lambda _config: failures["identity"])
    monkeypatch.setattr(control, "world_environment_is_current", lambda _config: failures["identity"])
    monkeypatch.setattr(control, "simulator_lifecycle_lock", lambda **_kwargs: contextlib.nullcontext())

    daemon = control.EnvironmentControlDaemon(config)
    daemon.active = packs["apartment"]

    def request(environment_id: str) -> str:
        request_id = str(uuid.uuid4())
        current.write_text(json.dumps({"request_id": request_id, "id": environment_id}), encoding="utf-8")
        assert control._read_request() == (request_id, environment_id)
        daemon._process_request(request_id, environment_id)
        assert not current.exists()
        return request_id

    success_id = request("town")
    ready = json.loads(catalog.read_text(encoding="utf-8"))
    assert ready == {
        "schema_version": 1,
        "active": {"id": "town", "display_name": "Town", "fingerprint": "fingerprint-town"},
        "environments": [
            {"id": "apartment", "display_name": "Apartment"},
            {"id": "town", "display_name": "Town"},
        ],
        "switch": {
            "request_id": success_id,
            "target": {"id": "town", "display_name": "Town"},
            "state": "ready",
            "message": "Town is ready.",
        },
    }
    pending = [snapshot for snapshot in catalog_writes if snapshot["switch"]["state"] in {"queued", "running"}]
    assert pending and {snapshot["active"]["id"] for snapshot in pending} == {"apartment"}
    assert terminal_published_with_mailbox == [True]
    ordered = [events.index(name) for name in ("stop:ros", "stop:world", "activate:town", "world:town", "ros:town")]
    assert ordered == sorted(ordered)

    stops = events.count("stop:ros")
    request("town")
    assert events.count("stop:ros") == stops, "a coherent target is a no-op"

    failures["target_ros"] = True
    failed_id = request("apartment")
    rolled_back = json.loads(catalog.read_text(encoding="utf-8"))
    assert rolled_back["active"]["id"] == "town"
    assert rolled_back["switch"]["request_id"] == failed_id
    assert rolled_back["switch"]["state"] == "failed"
    assert "target ROS failed" in rolled_back["switch"]["message"]
    assert "\r" not in rolled_back["switch"]["message"] and "\x1b" not in rolled_back["switch"]["message"]

    failures["rollback_world"] = True
    request("apartment")
    failed_closed = json.loads(catalog.read_text(encoding="utf-8"))
    assert failed_closed["active"] is None
    assert events[-2:] == ["stop:ros", "stop:world"]

    # Every startup mailbox is an interrupted operation, never a command to
    # replay. With no proven active runtime the replacement stops consumers.
    failures.update(target_ros=False, rollback_world=False, identity=False)
    stops = events.count("stop:ros")
    interrupted_id = str(uuid.uuid4())
    current.write_text(json.dumps({"request_id": interrupted_id, "id": "town"}), encoding="utf-8")
    daemon._fail_leftover_request()
    interrupted = json.loads(catalog.read_text(encoding="utf-8"))
    assert interrupted["active"] is None
    assert interrupted["switch"]["request_id"] == interrupted_id
    assert interrupted["switch"]["state"] == "failed"
    assert "interrupted" in interrupted["switch"]["message"]
    assert events.count("stop:ros") == stops + 1

    # A coherent active runtime is preserved, but the leftover request still
    # receives one terminal failure before its mailbox is removed.
    daemon.active = packs["apartment"]
    failures["identity"] = True
    preserved_stops = events.count("stop:ros")
    recovered_id = str(uuid.uuid4())
    current.write_text(json.dumps({"request_id": recovered_id, "id": "town"}), encoding="utf-8")
    daemon._fail_leftover_request()
    recovered = json.loads(catalog.read_text(encoding="utf-8"))
    assert recovered["active"]["id"] == "apartment"
    assert recovered["switch"]["request_id"] == recovered_id
    assert recovered["switch"]["state"] == "failed"
    assert "kept verified Apartment" in recovered["switch"]["message"]
    assert events.count("stop:ros") == preserved_stops
    writes = len(catalog_writes)
    daemon._fail_leftover_request()
    assert len(catalog_writes) == writes
    assert terminal_published_with_mailbox and all(terminal_published_with_mailbox)

    # The supervisor owns the singleton across worker crashes. A lifecycle
    # stop arriving during restart backoff must prevent another worker launch.
    singleton = tmp_path / "control/singleton.lock"
    intent = tmp_path / "control/intent.lock"
    stop = tmp_path / "control/stop"
    heartbeat = tmp_path / "control/heartbeat.json"
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH", singleton)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_INTENT_LOCK_PATH", intent)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_STOP_PATH", stop)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(
        control,
        "prepare_environment_control_directories",
        lambda: singleton.parent.mkdir(parents=True, exist_ok=True),
    )

    # Only the newest lifecycle command may authorize a new controller. This
    # is the startup-vs-shutdown interleaving that previously lost `down`.
    superseded = control.request_environment_control_daemon_stop()
    latest = control.request_environment_control_daemon_stop()
    with pytest.raises(control.StackError, match="superseded"):
        control.authorize_environment_control_daemon_start(superseded)
    with pytest.raises(control.StackError, match="superseded"):
        control.validate_environment_control_stop_request(superseded)
    assert json.loads(stop.read_text())["request_id"] == latest
    control.authorize_environment_control_daemon_start(latest)
    assert not stop.exists()

    # Failed-start cleanup waits for its own detached process and preserves a
    # newer command's token rather than replacing it with an internal one.
    newest = control.request_environment_control_daemon_stop()
    waited: list[float] = []

    class StartedProcess:
        def wait(self, *, timeout):
            waited.append(timeout)
            return 0

    control._stop_spawned_supervisor(StartedProcess())  # type: ignore[arg-type]
    assert waited == [5.0]
    assert json.loads(stop.read_text())["request_id"] == newest
    control.authorize_environment_control_daemon_start(newest)

    # The worker never consumes supervisor-owned stop intent, including on an
    # exceptional path before its request loop begins.
    worker_stop = control.request_environment_control_daemon_stop()
    stopped_worker = control.EnvironmentControlDaemon(config)
    stopped_worker.stop_event.set()
    assert stopped_worker._run() == 0
    assert json.loads(stop.read_text())["request_id"] == worker_stop
    control.authorize_environment_control_daemon_start(worker_stop)

    worker_runs: list[list[str]] = []

    def crashed_worker(command, **kwargs):
        assert kwargs["pass_fds"]
        assert control._lock_is_held()
        worker_runs.append(command)
        return control.subprocess.CompletedProcess(command, -9)

    lock_held_during_backoff: list[bool] = []
    stopped_during_backoff: list[str] = []

    def stop_in_backoff(_seconds):
        lock_held_during_backoff.append(control._lock_is_held())
        stopped_during_backoff.append(control.request_environment_control_daemon_stop())

    monkeypatch.setattr(control.subprocess, "run", crashed_worker)
    monkeypatch.setattr(control.time, "sleep", stop_in_backoff)
    assert control._supervise_environment_control_daemon(config) == 0
    assert lock_held_during_backoff == [True]
    assert json.loads(stop.read_text())["request_id"] == stopped_during_backoff[0]
    assert len(worker_runs) == 1
