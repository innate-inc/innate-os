# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""One filesystem scenario covering pack activation and Nav2 seeding."""

import json
import sys
from pathlib import Path

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
for directory in (LAUNCHER_DIR, ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import runtime as launcher_runtime  # noqa: E402
from config import StackError  # noqa: E402
from environment import (  # noqa: E402
    activate_environment,
    load_active_environment,
    load_environment_pack,
    select_environment,
)
from seed_sim_environment import seed_sim_environment  # noqa: E402


def _install_pack(sim_repo: Path, environment_id: str, *, local: bool = False) -> Path:
    root = f"{'local-environments' if local else 'packs'}/{environment_id}"
    manifest: dict[str, object] = {
        "schema_version": 1,
        "id": environment_id,
        "display_name": environment_id.title(),
        "coordinate_system": "gltf-y-up-meters",
        "physics": {"collision_dir": f"{root}/collision", "visual_dir": f"{root}/visual"},
        "viewer": {"type": "glb", "model": f"{root}/scene.glb", "collision_dir": f"{root}/hulls"},
        "navigation": {"map_yaml": f"{root}/map.yaml", "map_image": f"{root}/map.pgm"},
        "spawn": {"x": 1.25, "y": -2.5, "yaw_degrees": 45},
    }
    if local:
        manifest["attribution"] = {"generated_assets_sha256": "a" * 64}
    directory = sim_repo / ("environments.local" if local else "environments")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{environment_id}.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    for relative in ("collision", "visual"):
        (sim_repo / "assets" / root / relative).mkdir(parents=True, exist_ok=True)
    map_root = sim_repo / "assets" / root
    (map_root / "map.yaml").write_text("image: source.pgm\nresolution: 0.05\n", encoding="utf-8")
    (map_root / "map.pgm").write_text("P2\n1 1\n1\n0\n", encoding="utf-8")
    viewer_root = sim_repo / "viewer/public" / root
    (viewer_root / "hulls").mkdir(parents=True, exist_ok=True)
    (viewer_root / "scene.glb").write_bytes(b"glb")
    return path


def test_environment_activation_and_nav2_seed_follow_one_manifest_identity(tmp_path, monkeypatch):
    checked_in = load_environment_pack(Path(__file__).resolve().parents[2], "apartment")
    assert (checked_in.viewer_type, checked_in.spawn_pose) == ("split-glb", (-4.34, -0.17, -89.8))

    sim_repo = tmp_path / "sim"
    _install_pack(sim_repo, "apartment")
    local_manifest = _install_pack(sim_repo, "town", local=True)
    for unit in launcher_runtime.SIM_ASSET_UNITS_DERIVED:
        (sim_repo / "assets" / unit).mkdir(parents=True, exist_ok=True)
    (sim_repo / "assets/.assets-tag").write_text("sha256:physics-v1 assets-ref geometry-v1\n", encoding="utf-8")
    (sim_repo / "viewer/public/.installed-tag").write_text(
        "sha256:viewer-v1 assets-ref geometry-v1\n", encoding="utf-8"
    )
    bundle = sim_repo / "viewer/dist-lib"
    bundle.mkdir(parents=True)
    (bundle / "index.js").write_text("export {};\n", encoding="utf-8")
    (bundle / ".installed-tag").write_text("sha256:bundle published-ref\n", encoding="utf-8")
    config: dict[str, object] = {
        "sim_repo": sim_repo,
        "os_repo": tmp_path / "repo",
        "environment_id": "apartment",
    }
    monkeypatch.setattr(launcher_runtime, "assets_image_ref", lambda _config: "assets-ref")
    monkeypatch.setattr(launcher_runtime, "viewer_image_ref", lambda _config: "published-ref")
    monkeypatch.setattr(launcher_runtime, "resolve_local_viewer_image", lambda _repo: "local-ref")
    monkeypatch.setattr(launcher_runtime, "compute_geometry_inputs_hash", lambda _repo: "geometry-v1")
    monkeypatch.setattr(launcher_runtime, "compute_ros_install_validation_hash", lambda _repo: "ros-v1")
    monkeypatch.setattr(launcher_runtime, "ROS_INSTALL_STATE_PATH", tmp_path / "ros-install.inputs.sha256")
    launcher_runtime.ROS_INSTALL_STATE_PATH.write_text("ros-v1\n", encoding="utf-8")

    apartment = load_environment_pack(sim_repo, "apartment", validate_assets=True)
    assert launcher_runtime.simulator_install_is_current(config, apartment)
    apartment.collision_path.rmdir()
    assert not launcher_runtime.simulator_install_is_current(config, apartment)

    fetched: list[str] = []

    def extract_fixture(_blob, staging):
        work = staging / "work"
        for unit in launcher_runtime.SIM_ASSET_UNITS_DERIVED:
            (work / unit).mkdir(parents=True)
        root = work / "packs/apartment"
        for relative in ("collision", "visual"):
            (root / relative).mkdir(parents=True)
        (root / "map.yaml").write_text("image: map.pgm\n", encoding="utf-8")
        (root / "map.pgm").write_text("P2\n1 1\n1\n0\n", encoding="utf-8")

    monkeypatch.setattr(
        launcher_runtime.oci,
        "manifest_for_image",
        lambda _image: {"layers": [{"digest": "sha256:physics-v2"}, {"digest": "sha256:viewer-v2"}]},
    )
    monkeypatch.setattr(launcher_runtime.oci, "split_ref", lambda _image: ("repo", "tag"))
    monkeypatch.setattr(launcher_runtime.oci, "anon_token", lambda _repo: "token")
    monkeypatch.setattr(
        launcher_runtime.oci,
        "fetch_layer",
        lambda _repo, digest, _output, _token, **_kwargs: fetched.append(digest),
    )
    monkeypatch.setattr(launcher_runtime.oci, "safe_extract", extract_fixture)
    launcher_runtime.ensure_sim_assets(config, apartment)
    assert fetched == ["sha256:physics-v2"] and apartment.collision_path.is_dir()

    town = select_environment(config, "town")
    assert activate_environment(town) is True
    assert activate_environment(town) is False
    assert load_active_environment(sim_repo, validate_assets=True) == town

    data = tmp_path / "data"
    selected_yaml, selected_image = seed_sim_environment(town.active_path, town.assets_root, data)
    assert selected_yaml.read_text(encoding="utf-8").startswith('image: "map.pgm"')
    assert selected_image.read_text(encoding="utf-8").startswith("P2")
    custom = data / "maps/custom.yaml"
    custom.write_text("image: custom.pgm\n", encoding="utf-8")
    (data / ".last_map").write_text("custom.yaml\n", encoding="utf-8")
    seed_sim_environment(town.active_path, town.assets_root, data)
    assert (data / ".last_map").read_text(encoding="utf-8") == "custom.yaml\n"

    # The builder's manifest marker, not recursive asset polling, advances a
    # local pack generation and resets Nav2 to that pack's default map.
    (sim_repo / "viewer/public/local-environments/town/scene.glb").write_bytes(b"new bytes")
    assert load_environment_pack(sim_repo, "town").fingerprint == town.fingerprint
    manifest = json.loads(local_manifest.read_text(encoding="utf-8"))
    manifest["attribution"]["generated_assets_sha256"] = "b" * 64
    local_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    changed = load_environment_pack(sim_repo, "town", validate_assets=True)
    assert changed.fingerprint != town.fingerprint
    activate_environment(changed)
    seed_sim_environment(changed.active_path, changed.assets_root, data)
    assert (data / ".last_map").read_text(encoding="utf-8") == "map.yaml\n"

    manifest["physics"]["collision_dir"] = "../outside"
    local_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(StackError, match="contained relative path"):
        load_environment_pack(sim_repo, "town")
