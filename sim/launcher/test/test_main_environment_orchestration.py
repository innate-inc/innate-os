# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import json
import sys
from pathlib import Path

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))

import main as launcher_main  # noqa: E402


class FakePack:
    id = "gallery"
    display_name = "Gallery"
    fingerprint = "gallery-v2"

    def __init__(self, root: Path):
        self.assets_root = root / "assets"
        self.viewer_public_root = root / "viewer" / "public"

    def validate_assets(self) -> None:
        return None


class Step:
    ok = False

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False


def test_asset_warm_path_requires_the_expected_oci_ref(monkeypatch, tmp_path):
    pack = FakePack(tmp_path)
    pack.assets_root.mkdir(parents=True, exist_ok=True)
    pack.viewer_public_root.mkdir(parents=True)
    for marker in (pack.assets_root / ".assets-tag", pack.viewer_public_root / ".installed-tag"):
        marker.write_text("sha256:layer expected-ref inputs-hash\n", encoding="utf-8")
    monkeypatch.delenv("INNATE_SIM_ASSETS_IMAGE", raising=False)
    monkeypatch.setattr(launcher_main, "assets_image_ref", lambda _config: "expected-ref")
    monkeypatch.setattr(launcher_main, "sim_assets_install_is_current", lambda _config: True)

    assert launcher_main._environment_assets_are_current(
        {"sim_repo": tmp_path},
        pack,
        (pack.id, pack.fingerprint),
    )

    monkeypatch.setattr(launcher_main, "assets_image_ref", lambda _config: "next-ref")
    assert not launcher_main._environment_assets_are_current(
        {"sim_repo": tmp_path},
        pack,
        (pack.id, pack.fingerprint),
    )


def test_asset_warm_path_requires_complete_generic_work_inventory(monkeypatch, tmp_path):
    pack = FakePack(tmp_path)
    pack.assets_root.mkdir(parents=True)
    pack.viewer_public_root.mkdir(parents=True)
    (pack.assets_root / ".assets-tag").write_text("sha256:work expected-ref inputs-hash\n", encoding="utf-8")
    (pack.viewer_public_root / ".installed-tag").write_text(
        "sha256:viewer expected-ref inputs-hash\n", encoding="utf-8"
    )
    monkeypatch.delenv("INNATE_SIM_ASSETS_IMAGE", raising=False)
    monkeypatch.setattr(launcher_main, "assets_image_ref", lambda _config: "expected-ref")
    monkeypatch.setattr(launcher_main, "sim_assets_install_is_current", lambda _config: False)

    assert not launcher_main._environment_assets_are_current(
        {"sim_repo": tmp_path},
        pack,
        (pack.id, pack.fingerprint),
    )


def test_up_stops_a_partial_ros_session_before_asset_checks(monkeypatch, tmp_path):
    events: list[str] = []
    pack = FakePack(tmp_path)
    config: dict[str, object] = {
        "sim_repo": tmp_path,
        "brain_backend": "configured",
    }
    running_checks = iter((False, False))

    monkeypatch.setattr(launcher_main, "select_environment", lambda _config, _override=None: pack)
    monkeypatch.setattr(launcher_main, "print_banner", lambda: None)
    monkeypatch.setattr(launcher_main, "ensure_docker_available", lambda **_kwargs: None)
    monkeypatch.setattr(launcher_main, "ensure_uv_available", lambda: None)
    monkeypatch.setattr(launcher_main, "report_configured_keys", lambda _config: None)
    monkeypatch.setattr(launcher_main, "ensure_workspace_dirs", lambda _config: None)
    monkeypatch.setattr(launcher_main, "remove_superseded_containers", lambda: None)
    monkeypatch.setattr(launcher_main, "refuse_if_another_checkout_is_running", lambda: None)
    monkeypatch.setattr(launcher_main, "_active_environment_identity", lambda _config: (pack.id, pack.fingerprint))
    monkeypatch.setattr(
        launcher_main,
        "collect_os_process_status",
        lambda _config: {"os_session_running": True},
    )
    monkeypatch.setattr(launcher_main, "runtime_already_running", lambda _config: next(running_checks))
    monkeypatch.setattr(launcher_main, "_environment_assets_are_current", lambda *_args: True)
    monkeypatch.setattr(launcher_main, "stop_os_session", lambda _config: events.append("stop-ros"))
    monkeypatch.setattr(launcher_main, "world_server_running", lambda: True)
    monkeypatch.setattr(
        launcher_main,
        "_stop_local_world_before_asset_refresh",
        lambda: events.append("stop-world"),
    )
    monkeypatch.setattr(launcher_main, "ensure_sim_assets", lambda _config: events.append("sim-assets"))
    monkeypatch.setattr(launcher_main, "ensure_viewer_public_assets", lambda _config: events.append("viewer-assets"))
    monkeypatch.setattr(launcher_main, "ensure_sim_viewer_bundle", lambda _config, **_kwargs: None)
    monkeypatch.setattr(launcher_main, "activate_environment", lambda _pack: False)
    monkeypatch.setattr(launcher_main, "build_os_env", lambda _config: tmp_path / "os.env")
    monkeypatch.setattr(launcher_main, "ensure_skill_assets", lambda _config: None)
    monkeypatch.setattr(launcher_main, "ensure_world_server", lambda _config: ("world:8799", True))
    monkeypatch.setattr(launcher_main, "ensure_os_container", lambda _config, _env, **_kwargs: None)
    monkeypatch.setattr(launcher_main, "wait_for_os_runtime_ready", lambda _config, **_kwargs: True)
    monkeypatch.setattr(launcher_main, "wait_for_virtual_mars", lambda _config, **_kwargs: True)
    monkeypatch.setattr(launcher_main, "print_startup_checks", lambda _config, **_kwargs: True)
    monkeypatch.setattr(launcher_main, "show_runtime_dashboard", lambda _config, **_kwargs: None)
    monkeypatch.setattr(launcher_main, "live_step", lambda *_args, **_kwargs: Step())
    monkeypatch.setattr(launcher_main, "log", lambda _message: None)
    monkeypatch.setattr(launcher_main, "success", lambda _message: None)

    launcher_main.cmd_up(config, watch=False)

    assert events == ["stop-ros", "stop-world", "sim-assets", "viewer-assets"]


def test_assets_refresh_preserves_live_pack_and_quiesces_before_mutation(monkeypatch, tmp_path):
    descriptor = tmp_path / "assets" / launcher_main.ACTIVE_ENVIRONMENT_FILENAME
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        json.dumps({"schema_version": 1, "id": "gallery", "fingerprint": "gallery-v1"}),
        encoding="utf-8",
    )
    config: dict[str, object] = {"sim_repo": tmp_path}
    pack = FakePack(tmp_path)
    events: list[str] = []
    overrides: list[str | None] = []

    monkeypatch.setattr(launcher_main, "get_config", lambda: config)
    monkeypatch.setattr(launcher_main, "refuse_if_another_checkout_is_running", lambda: None)
    monkeypatch.setattr(
        launcher_main,
        "collect_os_process_status",
        lambda _config: {"os_session_running": True},
    )
    monkeypatch.setattr(launcher_main, "world_server_running", lambda: True)

    def select(_config, override=None):
        overrides.append(override)
        return pack

    monkeypatch.setattr(launcher_main, "select_environment", select)
    monkeypatch.setattr(launcher_main, "stop_os_session", lambda _config: events.append("stop-ros"))
    monkeypatch.setattr(
        launcher_main,
        "_stop_local_world_before_asset_refresh",
        lambda: events.append("stop-world"),
    )
    monkeypatch.setattr(launcher_main, "ensure_sim_assets", lambda _config: events.append("sim-assets"))
    monkeypatch.setattr(launcher_main, "ensure_viewer_public_assets", lambda _config: events.append("viewer-assets"))
    monkeypatch.setattr(launcher_main, "activate_environment", lambda _pack: None)
    monkeypatch.setattr(launcher_main, "ensure_world_server", lambda _config: ("world:8799", True))
    monkeypatch.setattr(launcher_main, "build_os_env", lambda _config: tmp_path / "os.env")
    monkeypatch.setattr(launcher_main, "ensure_os_container", lambda _config, _env: events.append("start-ros"))
    monkeypatch.setattr(launcher_main, "wait_for_os_runtime_ready", lambda _config, **_kwargs: True)
    monkeypatch.setattr(launcher_main, "wait_for_virtual_mars", lambda _config, **_kwargs: True)
    monkeypatch.setattr(launcher_main, "success", lambda _message: None)
    monkeypatch.setattr(sys, "argv", ["innate-sim", "assets"])

    assert launcher_main.main() == 0
    assert overrides == ["gallery", "gallery"]
    assert events == ["stop-ros", "stop-world", "sim-assets", "viewer-assets", "start-ros"]
