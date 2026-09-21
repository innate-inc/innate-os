"""XPBD defaults, explicit legacy fallback, dependency resolution and reuse."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim" / "launcher"))
runtime = importlib.import_module("runtime")


def test_cloth_mode_and_native_module_invalidate_server_digest(tmp_path, monkeypatch):
    config = {"os_repo": tmp_path, "sim_repo": tmp_path / "sim"}
    monkeypatch.delenv("INNATE_SIM_CLOTH_BACKEND", raising=False)
    monkeypatch.delenv("INNATE_SIM_XPBD_MODULE_DIR", raising=False)
    default = runtime._world_model_sources_digest(config)
    monkeypatch.setenv("INNATE_SIM_CLOTH_BACKEND", "xpbd")
    xpbd = runtime._world_model_sources_digest(config)
    assert xpbd == default
    monkeypatch.setenv("INNATE_SIM_CLOTH_BACKEND", "mujoco")
    assert runtime._world_model_sources_digest(config) != default
    monkeypatch.delenv("INNATE_SIM_CLOTH_BACKEND")
    native = tmp_path / "sim/.xpbd/lib"
    native.mkdir(parents=True)
    (native / "pypbd.test.so").write_bytes(b"first build")
    first = runtime._world_model_sources_digest(config)
    (native / "pypbd.test.so").write_bytes(b"second build")
    assert first != xpbd and first != runtime._world_model_sources_digest(config)


def test_world_server_resolves_only_requested_extra(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        runtime.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(pid=123)
    )
    monkeypatch.setattr(runtime, "_world_server_ping", lambda _: True)
    monkeypatch.setattr(runtime, "_render_scale_args", lambda: [])
    for name in ("WORLD_SERVER_LOG_PATH", "WORLD_SERVER_PID_PATH", "WORLD_SERVER_PORTS_PATH"):
        monkeypatch.setattr(runtime, name, tmp_path / name)
    monkeypatch.delenv("INNATE_SIM_CLOTH_BACKEND", raising=False)
    runtime._start_world_server("uv", tmp_path, environment_id="apartment", bind="127.0.0.1", mujoco_gl=None)
    assert calls[-1][0][4:6] == ["--extra", "xpbd"]
    monkeypatch.setenv("INNATE_SIM_CLOTH_BACKEND", " xpbd ")
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    runtime._start_world_server("uv", tmp_path, environment_id="apartment", bind="127.0.0.1", mujoco_gl=None)
    assert calls[-1][0][4:6] == ["--extra", "xpbd"]
    assert calls[-1][1]["env"]["OMP_NUM_THREADS"] == "1"
    monkeypatch.setenv("INNATE_SIM_CLOTH_BACKEND", "  ")
    runtime._start_world_server("uv", tmp_path, environment_id="apartment", bind="127.0.0.1", mujoco_gl=None)
    assert calls[-1][0][4:6] == ["--extra", "xpbd"]
    monkeypatch.setenv("INNATE_SIM_CLOTH_BACKEND", "mujoco")
    runtime._start_world_server("uv", tmp_path, environment_id="apartment", bind="127.0.0.1", mujoco_gl=None)
    assert "--extra" not in calls[-1][0]
    assert "OMP_NUM_THREADS" not in calls[-1][1]["env"]
