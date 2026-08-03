# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import json
import os
import sys
from pathlib import Path

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))

import runtime  # noqa: E402
from config import StackError  # noqa: E402


def _fake_work_layer(monkeypatch, digest: str = "sha256:work-v1") -> list[str]:
    monkeypatch.setattr(runtime, "assets_image_ref", lambda _config: "ghcr.io/example/assets:one")
    monkeypatch.setattr(runtime, "compute_geometry_inputs_hash", lambda _repo: "geometry-v1")
    monkeypatch.setattr(
        runtime.oci,
        "manifest_for_image",
        lambda _image: {"layers": [{"digest": digest}, {"digest": "sha256:viewer-v1"}]},
    )
    monkeypatch.setattr(runtime.oci, "split_ref", lambda _image: ("example/assets", "one"))
    monkeypatch.setattr(runtime.oci, "anon_token", lambda _repo: "token")
    fetches: list[str] = []

    def fetch_layer(_repo, layer_digest, handle, _token, *, label=""):
        fetches.append(layer_digest)
        handle.write(b"layer")

    def safe_extract(_blob, destination):
        work = Path(destination) / "work"
        for unit in runtime.SIM_ASSET_UNITS_DERIVED:
            (work / unit).mkdir(parents=True)
            (work / unit / "payload").write_text(unit, encoding="utf-8")
        (work / "humans").mkdir()
        (work / "humans" / "person.obj").write_text("person", encoding="utf-8")
        (work / "road-crossing" / "collision").mkdir(parents=True)
        (work / "road-crossing" / "collision" / "road.obj").write_text("road", encoding="utf-8")
        (work / "ATTRIBUTION.md").write_text("credits", encoding="utf-8")

    monkeypatch.setattr(runtime.oci, "fetch_layer", fetch_layer)
    monkeypatch.setattr(runtime.oci, "safe_extract", safe_extract)
    return fetches


def test_work_layer_installs_and_tracks_pack_specific_roots(monkeypatch, tmp_path):
    fetches = _fake_work_layer(monkeypatch)
    sim_repo = tmp_path / "sim"
    sim_repo.mkdir()
    config: dict[str, object] = {"sim_repo": sim_repo, "os_repo": tmp_path}

    runtime.ensure_sim_assets(config)
    runtime.ensure_sim_assets(config)

    assert fetches == ["sha256:work-v1"]
    assert (sim_repo / "assets/road-crossing/collision/road.obj").read_text() == "road"
    assert (sim_repo / "assets/ATTRIBUTION.md").read_text() == "credits"
    inventory = json.loads((sim_repo / "assets" / runtime.SIM_ASSET_INVENTORY_FILENAME).read_text())
    assert inventory["digest"] == "sha256:work-v1"
    assert inventory["roots"]["road-crossing"] == "directory"
    assert inventory["roots"]["ATTRIBUTION.md"] == "file"

    # Generic roots, unlike optional authored props, participate in the warm
    # install check and are repaired if somebody deletes one by hand.
    runtime.shutil.rmtree(sim_repo / "assets/road-crossing")
    runtime.ensure_sim_assets(config)
    assert fetches == ["sha256:work-v1", "sha256:work-v1"]
    assert (sim_repo / "assets/road-crossing/collision/road.obj").is_file()


def test_missing_authored_unit_preserves_the_existing_additive_behavior(monkeypatch, tmp_path):
    fetches = _fake_work_layer(monkeypatch)
    sim_repo = tmp_path / "sim"
    sim_repo.mkdir()
    config: dict[str, object] = {"sim_repo": sim_repo, "os_repo": tmp_path}
    runtime.ensure_sim_assets(config)

    runtime.shutil.rmtree(sim_repo / "assets/humans")
    runtime.ensure_sim_assets(config)

    assert fetches == ["sha256:work-v1"]


def test_warm_path_probe_requires_the_generic_root_inventory(monkeypatch, tmp_path):
    _fake_work_layer(monkeypatch)
    sim_repo = tmp_path / "sim"
    sim_repo.mkdir()
    config: dict[str, object] = {"sim_repo": sim_repo, "os_repo": tmp_path}

    assert runtime.sim_assets_install_is_current(config) is False
    runtime.ensure_sim_assets(config)
    assert runtime.sim_assets_install_is_current(config) is True

    runtime.shutil.rmtree(sim_repo / "assets/road-crossing")
    assert runtime.sim_assets_install_is_current(config) is False


def test_work_layer_failure_restores_the_previous_generation(monkeypatch, tmp_path):
    sim_repo = tmp_path / "sim"
    assets = sim_repo / "assets"
    for name in ("first", "second"):
        (assets / name).mkdir(parents=True)
        (assets / name / "value").write_text(f"old-{name}", encoding="utf-8")
    work = sim_repo / ".staging/work"
    for name in ("first", "second"):
        (work / name).mkdir(parents=True)
        (work / name / "value").write_text(f"new-{name}", encoding="utf-8")

    real_replace = os.replace

    def fail_on_second_source(source, destination):
        if Path(source) == work / "second":
            raise OSError("injected rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(runtime.os, "replace", fail_on_second_source)

    with pytest.raises(StackError, match="atomically"):
        runtime._install_sim_work_layer(sim_repo, work, digest="sha256:new")

    assert (assets / "first/value").read_text() == "old-first"
    assert (assets / "second/value").read_text() == "old-second"
    assert not (assets / runtime.SIM_ASSET_INVENTORY_FILENAME).exists()


def test_work_layer_rejects_reserved_control_roots_before_mutation(tmp_path):
    sim_repo = tmp_path / "sim"
    assets = sim_repo / "assets"
    assets.mkdir(parents=True)
    (assets / "keep").write_text("old", encoding="utf-8")
    work = sim_repo / ".staging/work"
    work.mkdir(parents=True)
    (work / ".active-environment.json").write_text("{}", encoding="utf-8")

    with pytest.raises(StackError, match="reserved or unsafe"):
        runtime._install_sim_work_layer(sim_repo, work, digest="sha256:new")

    assert (assets / "keep").read_text() == "old"
