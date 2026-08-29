# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import json
import sys
from pathlib import Path

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))

import runtime  # noqa: E402
from config import StackError  # noqa: E402
from environment import activate_environment, load_environment_pack, select_environment  # noqa: E402


def manifest(environment_id: str = "test-room", *, split_viewer: bool = False) -> dict[str, object]:
    viewer: dict[str, object] = {
        "type": "split-glb" if split_viewer else "glb",
        "collision_dir": "packs/test/hulls",
    }
    if split_viewer:
        viewer |= {
            "manifest": "packs/test/rooms/manifest.json",
            "base_dir": "packs/test/rooms",
        }
    else:
        viewer["model"] = "packs/test/scene.glb"
    return {
        "schema_version": 1,
        "id": environment_id,
        "display_name": "Test Room",
        "coordinate_system": "gltf-y-up-meters",
        "physics": {"collision_dir": "packs/test/collisions", "visual_dir": "packs/test/visual"},
        "viewer": viewer,
        "navigation": {"map_yaml": "packs/test/map.yaml", "map_image": "packs/test/map.pgm"},
        "spawn": {"x": 1.25, "y": -2.5, "yaw_degrees": 45},
    }


def make_sim_repo(tmp_path: Path, raw_manifest: dict[str, object] | None = None) -> Path:
    sim_repo = tmp_path / "sim"
    (sim_repo / "environments").mkdir(parents=True)
    raw = raw_manifest or manifest()
    environment_id = str(raw.get("id", "test-room"))
    (sim_repo / "environments" / f"{environment_id}.json").write_text(json.dumps(raw), encoding="utf-8")
    return sim_repo


def install_assets(sim_repo: Path, *, split_viewer: bool = False) -> None:
    for relative in ("packs/test/collisions", "packs/test/visual"):
        (sim_repo / "assets" / relative).mkdir(parents=True)
    (sim_repo / "viewer/public/packs/test/hulls").mkdir(parents=True)
    for relative in ("packs/test/map.yaml", "packs/test/map.pgm"):
        path = sim_repo / "assets" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("data", encoding="utf-8")
    if split_viewer:
        rooms = sim_repo / "viewer/public/packs/test/rooms"
        rooms.mkdir(parents=True)
        (rooms / "manifest.json").write_text('{"rooms": []}', encoding="utf-8")
    else:
        model = sim_repo / "viewer/public/packs/test/scene.glb"
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(b"glb")


def write_layer_markers(sim_repo: Path, work: str = "sha256:work-v1", viewer: str = "sha256:viewer-v1") -> None:
    assets_marker = sim_repo / "assets/.assets-tag"
    viewer_marker = sim_repo / "viewer/public/.installed-tag"
    assets_marker.parent.mkdir(parents=True, exist_ok=True)
    viewer_marker.parent.mkdir(parents=True, exist_ok=True)
    assets_marker.write_text(f"{work} ghcr.io/example/assets:one geometry-hash\n", encoding="utf-8")
    viewer_marker.write_text(f"{viewer} ghcr.io/example/assets:one geometry-hash\n", encoding="utf-8")


def test_checked_in_apartment_pack_is_the_progressive_compatibility_default():
    sim_repo = Path(__file__).resolve().parents[2]
    pack = load_environment_pack(sim_repo, "apartment")

    assert pack.id == "apartment"
    assert pack.collision_dir.as_posix() == "apartment_split_v2"
    assert pack.viewer_type == "split-glb"
    assert pack.viewer_model is None
    assert pack.viewer_manifest == Path("models/apartment/manifest.json")
    assert pack.viewer_base_dir == Path("models/apartment")
    assert pack.spawn_pose == (-4.34, -0.17, -89.8)


def test_cli_selection_overrides_configured_environment(tmp_path):
    sim_repo = make_sim_repo(tmp_path)
    apartment = manifest("apartment")
    (sim_repo / "environments/apartment.json").write_text(json.dumps(apartment), encoding="utf-8")
    config: dict[str, object] = {"sim_repo": sim_repo, "environment_id": "apartment"}

    selected = select_environment(config, "test-room")

    assert selected.id == "test-room"
    assert config["environment"] is selected


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("physics", "collision_dir"),
        ("physics", "visual_dir"),
        ("viewer", "model"),
        ("viewer", "collision_dir"),
        ("navigation", "map_yaml"),
        ("navigation", "map_image"),
    ],
)
def test_manifest_paths_cannot_escape_their_asset_root(tmp_path, section, key):
    raw = manifest()
    raw[section][key] = "../outside"  # type: ignore[index]
    sim_repo = make_sim_repo(tmp_path, raw)

    with pytest.raises(StackError, match="contained relative path"):
        load_environment_pack(sim_repo, "test-room")


def test_activation_validates_monolith_assets_and_writes_shared_descriptor(tmp_path):
    sim_repo = make_sim_repo(tmp_path)
    write_layer_markers(sim_repo)
    pack = load_environment_pack(sim_repo, "test-room")
    with pytest.raises(StackError, match="installed incompletely"):
        activate_environment(pack)

    install_assets(sim_repo)
    pack = load_environment_pack(sim_repo, "test-room")
    assert activate_environment(pack) is True
    assert activate_environment(pack) is False
    active = json.loads(pack.active_path.read_text(encoding="utf-8"))
    assert active["id"] == "test-room"
    assert active["fingerprint"] == pack.fingerprint
    assert active["viewer"] == {
        "type": "glb",
        "model": "packs/test/scene.glb",
        "collision_dir": "packs/test/hulls",
    }


def test_activation_validates_progressive_viewer_assets(tmp_path):
    raw = manifest(split_viewer=True)
    sim_repo = make_sim_repo(tmp_path, raw)
    install_assets(sim_repo, split_viewer=True)
    write_layer_markers(sim_repo)

    pack = load_environment_pack(sim_repo, "test-room")
    assert activate_environment(pack) is True
    viewer = json.loads(pack.active_path.read_text(encoding="utf-8"))["viewer"]
    assert viewer == {
        "type": "split-glb",
        "manifest": "packs/test/rooms/manifest.json",
        "base_dir": "packs/test/rooms",
        "collision_dir": "packs/test/hulls",
    }


def test_installed_layer_digests_participate_in_runtime_fingerprint(tmp_path):
    sim_repo = make_sim_repo(tmp_path)
    write_layer_markers(sim_repo)
    first = load_environment_pack(sim_repo, "test-room")

    # A ref-only retag names the same layer bytes and must not restart a world.
    (sim_repo / "assets/.assets-tag").write_text(
        "sha256:work-v1 ghcr.io/example/assets:retagged other-hash\n", encoding="utf-8"
    )
    retagged = load_environment_pack(sim_repo, "test-room")
    assert retagged.fingerprint == first.fingerprint

    write_layer_markers(sim_repo, viewer="sha256:viewer-v2")
    second = load_environment_pack(sim_repo, "test-room")
    assert second.fingerprint != first.fingerprint


def test_ros_session_marker_tracks_the_selected_environment(tmp_path, monkeypatch):
    sim_repo = make_sim_repo(tmp_path)
    pack = load_environment_pack(sim_repo, "test-room")
    marker = tmp_path / "ros-environment.fingerprint"
    monkeypatch.setattr(runtime, "ROS_ENVIRONMENT_STATE_PATH", marker)
    config: dict[str, object] = {"environment": pack}

    assert runtime.ros_environment_is_current(config) is False
    marker.write_text(f"{pack.fingerprint}\n", encoding="utf-8")
    assert runtime.ros_environment_is_current(config) is True
    marker.write_text("another-environment\n", encoding="utf-8")
    assert runtime.ros_environment_is_current(config) is False


def test_activation_rejects_asset_symlink_that_escapes_its_root(tmp_path):
    sim_repo = make_sim_repo(tmp_path)
    install_assets(sim_repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    collision_path = sim_repo / "assets/packs/test/collisions"
    collision_path.rmdir()
    collision_path.symlink_to(outside)

    with pytest.raises(StackError, match="escape their roots"):
        activate_environment(load_environment_pack(sim_repo, "test-room"))


def test_unknown_environment_lists_available_packs(tmp_path):
    sim_repo = make_sim_repo(tmp_path)
    with pytest.raises(StackError, match="Available environments: test-room"):
        load_environment_pack(sim_repo, "missing")


def test_local_environment_is_discoverable_but_cannot_shadow_a_tracked_pack(tmp_path):
    sim_repo = make_sim_repo(tmp_path)
    local = manifest("local-town")
    local_directory = sim_repo / "environments.local"
    local_directory.mkdir()
    (local_directory / "local-town.json").write_text(json.dumps(local), encoding="utf-8")

    local_pack = load_environment_pack(sim_repo, "local-town")
    assert local_pack.manifest_path == local_directory / "local-town.json"
    assert local_pack.is_local

    shadow = manifest("test-room")
    shadow["display_name"] = "Shadowed"
    (local_directory / "test-room.json").write_text(json.dumps(shadow), encoding="utf-8")
    tracked_pack = load_environment_pack(sim_repo, "test-room")
    assert tracked_pack.display_name == "Test Room"
    assert not tracked_pack.is_local
