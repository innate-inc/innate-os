# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import contextlib
import fcntl
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))

import environment_control as control  # noqa: E402


class FakePack:
    def __init__(self, environment_id: str, display_name: str, events: list[str]):
        self.id = environment_id
        self.display_name = display_name
        self.fingerprint = f"fingerprint-{environment_id}"
        self._events = events

    def validate_assets(self) -> None:
        self._events.append(f"validate:{self.id}")


def _install_switch_fakes(monkeypatch, events: list[str]):
    previous = FakePack("apartment", "Apartment", events)
    target = FakePack("gallery", "Gallery", events)
    packs = {previous.id: previous, target.id: target}
    config: dict[str, object] = {"sim_repo": Path("/sim"), "os_repo": Path("/os")}

    def load(_sim_repo, environment_id, *, validate_assets=False):
        events.append(f"load:{environment_id}")
        pack = packs[environment_id]
        if validate_assets:
            pack.validate_assets()
        return pack

    def select(selected_config, environment_id):
        pack = packs[environment_id]
        events.append(f"select:{pack.id}")
        selected_config["environment"] = pack
        selected_config["environment_id"] = pack.id
        return pack

    monkeypatch.setattr(control, "load_environment_pack", load)
    monkeypatch.setattr(control, "select_environment", select)
    monkeypatch.setattr(
        control,
        "_active_descriptor",
        lambda _sim_repo: {
            "schema_version": 1,
            "id": previous.id,
            "display_name": previous.display_name,
            "fingerprint": previous.fingerprint,
        },
    )
    monkeypatch.setattr(control, "_require_running_stack", lambda _config: events.append("require-running"))
    monkeypatch.setattr(control, "stop_os_session", lambda _config: events.append("stop:ros"))
    monkeypatch.setattr(control, "stop_world_server_and_wait", lambda: events.append("stop:world"))
    monkeypatch.setattr(control, "activate_environment", lambda pack: events.append(f"activate:{pack.id}"))
    monkeypatch.setattr(control, "build_os_env", lambda _config: Path("/tmp/os.env"))
    monkeypatch.setattr(control, "_wait_for_refreshed_runtime", lambda _config, _report: events.append("wait-ready"))
    return config, previous, target


def test_request_reader_is_strict_partial_safe_and_inode_safe(tmp_path):
    now = time.time()
    job_id = str(uuid.uuid4())
    path = tmp_path / "current.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "environment_id": "gallery",
                "created_at": now,
            }
        ),
        encoding="utf-8",
    )
    request = control._read_control_request(path, now=now)
    assert (request.job_id, request.environment_id) == (job_id, "gallery")

    replacement = tmp_path / "replacement.json"
    replacement.write_text("replacement", encoding="utf-8")
    os.replace(replacement, path)
    control._remove_control_request(path, request.file_identity)
    assert path.read_text(encoding="utf-8") == "replacement"

    path.write_bytes(b"")
    with pytest.raises(control.TransientControlRequest):
        control._read_control_request(path, now=time.time())

    path.write_text(
        f'{{"schema_version":1,"schema_version":1,"job_id":"{job_id}","environment_id":"gallery","created_at":{now}}}',
        encoding="utf-8",
    )
    os.utime(path, (now - 2, now - 2))
    with pytest.raises(control.InvalidControlRequest, match="valid JSON"):
        control._read_control_request(path, now=now)

    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(control.InvalidControlRequest, match="symlink"):
        control._read_control_request(path, now=now)


def test_catalog_is_sorted_validated_bounded_and_keeps_active(monkeypatch, tmp_path):
    events: list[str] = []
    apartment = FakePack("apartment", "Apartment", events)
    gallery = FakePack("gallery", "Gallery", events)
    packs = {apartment.id: apartment, gallery.id: gallery}
    catalog_path = tmp_path / "catalog.json"
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_CATALOG_PATH", catalog_path)
    monkeypatch.setattr(control, "CONTROL_ENVIRONMENT_LIMIT", 1)
    monkeypatch.setattr(control, "available_environment_ids", lambda _repo: ["gallery", "broken", "apartment"])

    def load(_repo, environment_id, *, validate_assets=False):
        assert validate_assets
        if environment_id == "broken":
            raise control.StackError("not installed")
        return packs[environment_id]

    monkeypatch.setattr(control, "load_environment_pack", load)
    monkeypatch.setattr(
        control,
        "_active_descriptor",
        lambda _repo: {
            "schema_version": 1,
            "id": "gallery",
            "display_name": "Gallery",
            "fingerprint": gallery.fingerprint,
        },
    )

    control.write_environment_catalog({"sim_repo": tmp_path})

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog["environments"] == [{"id": "gallery", "display_name": "Gallery"}]
    assert catalog["active"] == {
        "id": "gallery",
        "display_name": "Gallery",
        "fingerprint": gallery.fingerprint,
    }


def test_validate_before_stop_and_already_active_is_a_noop(monkeypatch):
    events: list[str] = []
    config, previous, _target = _install_switch_fakes(monkeypatch, events)
    monkeypatch.setattr(control, "ros_environment_is_current", lambda _config: True)
    monkeypatch.setattr(control, "world_environment_is_current", lambda _config: True)

    selected = control.switch_running_environment(
        config,
        previous.id,
        lambda phase, _message, _progress: events.append(f"report:{phase}"),
    )

    assert selected is previous
    assert "validate:apartment" in events
    assert "report:ready" in events
    assert not any(event.startswith("stop:") for event in events)


def test_successful_switch_orders_stop_activation_world_and_ros(monkeypatch):
    events: list[str] = []
    config, _previous, target = _install_switch_fakes(monkeypatch, events)
    monkeypatch.setattr(
        control,
        "ensure_world_server",
        lambda _config: (events.append(f"world:{_config['environment'].id}") or "host:8799", True),
    )
    monkeypatch.setattr(
        control,
        "ensure_os_container",
        lambda _config, _env, **_kwargs: events.append(
            f"ros:{_config['environment'].id}:preserve={_kwargs.get('preserve_container')}"
        ),
    )

    selected = control.switch_running_environment(config, target.id, lambda *_args: None)

    assert selected is target
    order = [
        events.index("stop:ros"),
        events.index("stop:world"),
        events.index("activate:gallery"),
        events.index("world:gallery"),
        events.index("ros:gallery:preserve=True"),
    ]
    assert order == sorted(order)


def test_target_failure_restores_previous_environment(monkeypatch):
    events: list[str] = []
    config, previous, target = _install_switch_fakes(monkeypatch, events)
    phases: list[str] = []
    monkeypatch.setattr(
        control,
        "ensure_world_server",
        lambda _config: (events.append(f"world:{_config['environment'].id}") or "host:8799", True),
    )

    def ensure_ros(selected_config, _env, **_kwargs):
        environment_id = selected_config["environment"].id
        events.append(f"ros:{environment_id}")
        if environment_id == target.id:
            raise control.StackError("target ROS failed")

    monkeypatch.setattr(control, "ensure_os_container", ensure_ros)

    with pytest.raises(control.SwitchTransactionError) as raised:
        control.switch_running_environment(
            config,
            target.id,
            lambda phase, _message, _progress: phases.append(phase),
        )

    assert raised.value.recovered_environment is previous
    assert "rolling_back" in phases
    assert events.index("activate:gallery") < events.index("activate:apartment")
    assert events[-2:] == ["ros:apartment", "wait-ready"]


def test_rollback_failure_stops_partial_ros_fail_closed(monkeypatch):
    events: list[str] = []
    config, previous, target = _install_switch_fakes(monkeypatch, events)

    def ensure_world(selected_config):
        environment_id = selected_config["environment"].id
        events.append(f"world:{environment_id}")
        if environment_id == previous.id:
            raise control.StackError("rollback world failed")
        return "host:8799", True

    def ensure_ros(selected_config, _env, **_kwargs):
        environment_id = selected_config["environment"].id
        events.append(f"ros:{environment_id}")
        if environment_id == target.id:
            raise control.StackError("target ROS failed")

    monkeypatch.setattr(control, "ensure_world_server", ensure_world)
    monkeypatch.setattr(control, "ensure_os_container", ensure_ros)

    with pytest.raises(control.SwitchTransactionError) as raised:
        control.switch_running_environment(config, target.id, lambda *_args: None)

    assert raised.value.recovered_environment is None
    assert isinstance(raised.value.recovery_error, control.StackError)
    assert events[-1] == "stop:ros"
    assert "activate:apartment" in events


def test_singleton_lock_and_stale_pid_are_fail_safe(monkeypatch, tmp_path):
    singleton = tmp_path / "controller.lock"
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH", singleton)
    monkeypatch.setattr(control, "prepare_environment_control_directories", lambda: None)
    daemon = control.EnvironmentControlDaemon({})
    called = False

    def run_as_owner():
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(daemon, "_run_as_owner", run_as_owner)
    with singleton.open("a+", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert daemon.run() == 1
    assert not called

    pid_path = tmp_path / "controller.pid"
    heartbeat_path = tmp_path / "heartbeat.json"
    log_path = tmp_path / "controller.log"
    pid_path.write_text(json.dumps({"schema_version": 1, "pid": 4242}), encoding="utf-8")
    heartbeat_path.write_text(
        json.dumps({"schema_version": 1, "pid": 4242, "updated_at": time.time() - 60}),
        encoding="utf-8",
    )
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_PID_PATH", pid_path)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_LOG_PATH", log_path)
    monkeypatch.setattr(control, "_process_alive", lambda pid: pid == 4242)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(control.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    class ExitedProcess:
        pid = 9999

        @staticmethod
        def poll():
            return 1

        @staticmethod
        def terminate():
            return None

    monkeypatch.setattr(control.subprocess, "Popen", lambda *_args, **_kwargs: ExitedProcess())
    monkeypatch.setattr(control, "tail_file", lambda *_args, **_kwargs: "controller lock held")

    with pytest.raises(control.StackError, match="did not start"):
        control.ensure_environment_control_daemon({"os_repo": tmp_path})
    assert kills == []


def test_fresh_reused_pid_with_free_singleton_is_never_signalled(monkeypatch, tmp_path):
    pid_path = tmp_path / "controller.pid"
    heartbeat_path = tmp_path / "heartbeat.json"
    singleton_path = tmp_path / "controller.lock"
    pid_path.write_text(json.dumps({"schema_version": 1, "pid": 4242}), encoding="utf-8")
    heartbeat_path.write_text(
        json.dumps({"schema_version": 1, "pid": 4242, "updated_at": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_PID_PATH", pid_path)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH", singleton_path)
    monkeypatch.setattr(control, "_process_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(
        control,
        "_controller_process_matches",
        lambda _pid: pytest.fail("a free singleton lock must make PID metadata untrusted"),
    )
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(control.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    assert control.request_environment_control_daemon_stop() is None
    assert kills == []
    assert not pid_path.exists()
    assert not heartbeat_path.exists()


@pytest.mark.parametrize("stale_request", [False, True])
def test_replayed_running_job_is_failed_closed(monkeypatch, tmp_path, stale_request):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    current_path = tmp_path / "current.json"
    job_id = str(uuid.uuid4())
    created_at = time.time() - (301 if stale_request else 0)
    current_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "environment_id": "gallery",
                "created_at": created_at,
            }
        ),
        encoding="utf-8",
    )
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "target": {"id": "gallery", "display_name": "Gallery"},
                "state": "running",
                "phase": "starting_ros",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH", current_path)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(control, "_target_display_name", lambda *_args: "Gallery")
    monkeypatch.setattr(control, "simulator_lifecycle_lock", lambda **_kwargs: contextlib.nullcontext())
    monkeypatch.setattr(control, "write_environment_catalog", lambda _config: None)
    events: list[str] = []
    monkeypatch.setattr(control, "stop_os_session", lambda _config: events.append("stop:ros"))
    monkeypatch.setattr(control, "stop_world_server_and_wait", lambda: events.append("stop:world"))

    if stale_request:
        with pytest.raises(control.InvalidControlRequest) as raised:
            control._read_control_request(current_path)
        control._write_invalid_request_status({}, raised.value)
    else:
        request = control._read_control_request(current_path)
        control._process_request({}, request)

    status = json.loads((jobs_dir / f"{job_id}.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["phase"] == "failed"
    assert status["finished_at"] >= status["started_at"]
    assert events == ["stop:ros", "stop:world"]
    assert not current_path.exists()


def test_replayed_job_keeps_a_repaired_coherent_runtime(monkeypatch, tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    current_path = tmp_path / "current.json"
    job_id = str(uuid.uuid4())
    current_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "environment_id": "gallery",
                "created_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    (jobs_dir / f"{job_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "target": {"id": "gallery", "display_name": "Gallery"},
                "state": "running",
                "phase": "starting_ros",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH", current_path)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(control, "_target_display_name", lambda *_args: "Gallery")
    monkeypatch.setattr(control, "write_environment_catalog", lambda _config: None)
    repaired = FakePack("gallery", "Gallery", [])
    monkeypatch.setattr(control, "_healthy_active_environment", lambda _config: repaired)
    monkeypatch.setattr(
        control,
        "stop_os_session",
        lambda _config: pytest.fail("a coherent repaired runtime must not be stopped"),
    )
    monkeypatch.setattr(
        control,
        "stop_world_server_and_wait",
        lambda: pytest.fail("a coherent repaired runtime must not be stopped"),
    )

    control._process_request({}, control._read_control_request(current_path))

    status = json.loads((jobs_dir / f"{job_id}.json").read_text(encoding="utf-8"))
    assert status["state"] == "ready"
    assert status["fingerprint"] == repaired.fingerprint
    assert not current_path.exists()


def test_pre_activation_failure_only_reports_a_proven_recovery(monkeypatch, tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    current_path = tmp_path / "current.json"
    job_id = str(uuid.uuid4())
    current_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "environment_id": "gallery",
                "created_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH", current_path)
    monkeypatch.setattr(control, "ENVIRONMENT_CONTROL_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(control, "_target_display_name", lambda *_args: "Gallery")
    monkeypatch.setattr(control, "simulator_lifecycle_lock", lambda **_kwargs: contextlib.nullcontext())
    monkeypatch.setattr(control, "write_environment_catalog", lambda _config: None)
    monkeypatch.setattr(
        control,
        "switch_running_environment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(control.StackError("target validation failed")),
    )
    recovered = FakePack("apartment", "Apartment", [])
    monkeypatch.setattr(control, "_healthy_active_environment", lambda _config: recovered)

    control._process_request({}, control._read_control_request(current_path))

    status = json.loads((jobs_dir / f"{job_id}.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert status["recovered_environment"] == {
        "id": recovered.id,
        "display_name": recovered.display_name,
        "fingerprint": recovered.fingerprint,
    }
    assert "still ready" in status["message"]


def test_recovery_identity_requires_live_ros_processes_and_world(monkeypatch, tmp_path):
    pack = FakePack("apartment", "Apartment", [])
    monkeypatch.setattr(
        control,
        "_active_descriptor",
        lambda _repo: {
            "schema_version": 1,
            "id": pack.id,
            "display_name": pack.display_name,
            "fingerprint": pack.fingerprint,
        },
    )
    monkeypatch.setattr(control, "load_environment_pack", lambda *_args, **_kwargs: pack)
    monkeypatch.setattr(control, "select_environment", lambda config, _id: config.update(environment=pack) or pack)
    monkeypatch.setattr(control, "world_server_running", lambda: True)
    monkeypatch.setattr(control, "ros_environment_is_current", lambda _config: True)
    monkeypatch.setattr(control, "world_environment_is_current", lambda _config: True)
    processes = {
        "os_running": True,
        "os_session_running": True,
        "rosbridge_process_live": True,
        "brain_process_live": False,
        "sim_driver_process_live": True,
    }
    monkeypatch.setattr(control, "collect_os_process_status", lambda _config: processes)

    assert control._healthy_active_environment({"sim_repo": tmp_path}) is None
    processes["brain_process_live"] = True
    assert control._healthy_active_environment({"sim_repo": tmp_path}) is pack


def test_error_text_matches_proxy_control_character_rules():
    error = control._bounded_error(RuntimeError("ROS failed\r\x1b[31m"))
    assert "\r" not in error and "\x1b" not in error
    assert all(ord(character) >= 0x20 or character in "\n\t" for character in error)
