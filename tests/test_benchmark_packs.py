"""The benchmark worlds as environment packs: manifest, map and staging.

A pack that disagrees with its bundle is the failure this guards: the manifest
carries a spawn and a map name the launcher and Nav2 read without ever
loading the world, so if either drifts from the room sidecar the robot boots
on one pose and the map was rasterised from another.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ros2_ws/src/mars_bot/mars_sim_driver"))
sys.path.insert(0, str(ROOT / "sim" / "launcher"))

from mars_sim_driver.environments import Environment  # noqa: E402
from mars_sim_driver.statics import load_rooms  # noqa: E402

BUNDLES = sorted(path.name for path in (ROOT / "sim/bundles").iterdir() if (path / "rooms").is_dir())


def test_every_bundle_is_a_pack():
    assert BUNDLES, "no bundles found"
    for name in BUNDLES:
        assert (ROOT / "sim/environments" / name / "manifest.json").is_file(), name


@pytest.mark.parametrize("name", BUNDLES)
def test_pack_binds_its_bundle_and_carries_its_map(name):
    pack = Environment.load(name, ROOT / "sim/assets")
    assert pack.bundle == (ROOT / "sim/bundles" / name).resolve()
    assert pack.rooms_dir is not None and pack.rooms_dir.is_dir()
    assert pack.challenges_dir is not None and pack.challenges_dir.is_dir()
    assert pack.viewer["type"] == "primitives"
    # The spawn the launcher and the pack builder read is the room's own.
    room = next(iter(load_rooms([pack.rooms_dir]).values()))
    assert room.spawn is not None
    assert pack.spawn == pytest.approx(room.spawn)
    # The map beside the manifest is the one the manifest names, and the yaml
    # points at the pgm next to it (map_server resolves the image relatively).
    assert pack.pack_dir is not None
    yaml_path = pack.pack_dir / "map" / pack.map_name
    assert yaml_path.is_file(), yaml_path
    image = next(
        line.split(":", 1)[1].strip() for line in yaml_path.read_text().splitlines() if line.startswith("image:")
    )
    assert (yaml_path.parent / image).is_file()
    assert image == f"{name}.pgm" and pack.map_name == f"{name}.yaml"


def test_a_pack_needs_meshes_or_a_bundle(tmp_path, monkeypatch):
    from mars_sim_driver import environments, world

    fake = tmp_path / "sim" / "environments" / "hollow"
    fake.mkdir(parents=True)
    (tmp_path / "ros2_ws").mkdir()
    fake.joinpath("manifest.json").write_text(
        json.dumps(
            {"navigation": {"map_yaml": "map/hollow.yaml"}, "viewer": {}, "spawn": {"x": 0, "y": 0, "yaw_degrees": 0}}
        )
    )
    monkeypatch.setattr(world, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(environments.world, "repo_root", lambda: tmp_path)
    with pytest.raises(ValueError, match="physics.*bundle"):
        Environment.load("hollow", tmp_path / "sim/assets")


def test_up_stages_every_carried_map_for_the_container(tmp_path):
    """The launch script inside the container copies Nav2's maps from
    sim/assets/map; a pack's tracked map has to be there before it runs,
    whichever pack was selected, since the 3D view switches packs live."""
    import runtime

    sim_repo = tmp_path / "sim"
    for pack in ("alpha", "beta"):
        maps = sim_repo / "environments" / pack / "map"
        maps.mkdir(parents=True)
        (maps / f"{pack}.yaml").write_text(f"image: {pack}.pgm\n")
        (maps / f"{pack}.pgm").write_bytes(b"P5 1 1 255\n\xff")
        (maps / "notes.txt").write_text("not a map")
    (sim_repo / "environments" / "alpha" / "manifest.json").write_text(
        json.dumps({"navigation": {"map_yaml": "map/alpha.yaml"}})
    )
    published = sim_repo / "assets" / "map"
    published.mkdir(parents=True)
    (published / "sim_apartment.yaml").write_text("image: sim_apartment.pgm\n")

    os_repo = tmp_path / "os"
    config = {"os_repo": os_repo, "sim_repo": sim_repo, "environment_id": "alpha"}
    runtime._seed_nav_map(config)

    assert sorted(path.name for path in published.iterdir()) == [
        "alpha.pgm",
        "alpha.yaml",
        "beta.pgm",
        "beta.yaml",
        "sim_apartment.yaml",
    ]
    assert (published / "beta.pgm").read_bytes() == b"P5 1 1 255\n\xff"
    assert (os_repo / "data" / ".last_map").read_text() == "alpha.yaml\n"
    # Idempotent: a second boot rewrites nothing that already matches.
    assert runtime._stage_pack_maps(sim_repo) == []
