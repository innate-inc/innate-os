# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from seed_sim_environment import EnvironmentSeedError, seed_sim_environment  # noqa: E402


def write_descriptor(
    assets: Path,
    navigation: dict[str, object],
    *,
    environment_id: str = "apartment",
    fingerprint: str = "apartment-assets-v1",
) -> Path:
    descriptor = assets / ".active-environment.json"
    descriptor.write_text(
        json.dumps(
            {
                "id": environment_id,
                "fingerprint": fingerprint,
                "navigation": navigation,
            }
        ),
        encoding="utf-8",
    )
    return descriptor


def test_seeds_selected_map_and_persists_it(tmp_path):
    assets = tmp_path / "assets"
    source = assets / "packs" / "apartment" / "nav"
    source.mkdir(parents=True)
    (source / "apartment.yaml").write_text("image: apartment.pgm\n", encoding="utf-8")
    (source / "apartment.pgm").write_bytes(b"P5\n1 1\n255\n\0")
    descriptor = write_descriptor(
        assets,
        {
            "map_yaml": "packs/apartment/nav/apartment.yaml",
            "map_image": "packs/apartment/nav/apartment.pgm",
        },
    )
    data = tmp_path / "data"

    selected_yaml, selected_image = seed_sim_environment(descriptor, assets, data)

    assert selected_yaml == data / "maps" / "apartment.yaml"
    assert selected_yaml.read_text(encoding="utf-8") == 'image: "apartment.pgm"\n'
    assert selected_image.read_bytes() == b"P5\n1 1\n255\n\0"
    assert (data / ".last_map").read_text(encoding="utf-8") == "apartment.yaml\n"
    assert json.loads((data / ".last_environment").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "id": "apartment",
        "fingerprint": "apartment-assets-v1",
    }


def test_same_environment_preserves_an_available_user_selected_map(tmp_path):
    assets = tmp_path / "assets"
    source = assets / "packs" / "apartment" / "nav"
    source.mkdir(parents=True)
    (source / "apartment.yaml").write_text("image: apartment.pgm\n", encoding="utf-8")
    (source / "apartment.pgm").write_bytes(b"P5\n1 1\n255\n\0")
    descriptor = write_descriptor(
        assets,
        {
            "map_yaml": "packs/apartment/nav/apartment.yaml",
            "map_image": "packs/apartment/nav/apartment.pgm",
        },
    )
    data = tmp_path / "data"
    seed_sim_environment(descriptor, assets, data)
    (data / "maps" / "my-apartment-map.yaml").write_text("image: custom.pgm\n", encoding="utf-8")
    (data / ".last_map").write_text("my-apartment-map.yaml\n", encoding="utf-8")

    seed_sim_environment(descriptor, assets, data)

    assert (data / ".last_map").read_text(encoding="utf-8") == "my-apartment-map.yaml\n"
    assert (data / "maps" / "apartment.yaml").is_file()


def test_first_environment_pack_launch_preserves_legacy_apartment_map(tmp_path):
    assets = tmp_path / "assets"
    source = assets / "map"
    source.mkdir(parents=True)
    (source / "apartment.yaml").write_text("image: apartment.pgm\n", encoding="utf-8")
    (source / "apartment.pgm").write_bytes(b"P5\n1 1\n255\n\0")
    descriptor = write_descriptor(
        assets,
        {"map_yaml": "map/apartment.yaml", "map_image": "map/apartment.pgm"},
    )
    data = tmp_path / "data"
    (data / "maps").mkdir(parents=True)
    (data / "maps" / "custom.yaml").write_text("image: custom.pgm\n", encoding="utf-8")
    (data / ".last_map").write_text("custom.yaml\n", encoding="utf-8")

    seed_sim_environment(descriptor, assets, data)

    assert (data / ".last_map").read_text(encoding="utf-8") == "custom.yaml\n"
    assert (data / ".last_environment").is_file()


@pytest.mark.parametrize(
    ("environment_id", "fingerprint"),
    [("gallery", "apartment-assets-v1"), ("apartment", "apartment-assets-v2")],
)
def test_changed_environment_identity_resets_to_pack_map(tmp_path, environment_id, fingerprint):
    assets = tmp_path / "assets"
    source = assets / "packs" / "apartment" / "nav"
    source.mkdir(parents=True)
    (source / "apartment.yaml").write_text("image: apartment.pgm\n", encoding="utf-8")
    (source / "apartment.pgm").write_bytes(b"P5\n1 1\n255\n\0")
    navigation = {
        "map_yaml": "packs/apartment/nav/apartment.yaml",
        "map_image": "packs/apartment/nav/apartment.pgm",
    }
    descriptor = write_descriptor(assets, navigation)
    data = tmp_path / "data"
    seed_sim_environment(descriptor, assets, data)
    (data / "maps" / "custom.yaml").write_text("image: custom.pgm\n", encoding="utf-8")
    (data / ".last_map").write_text("custom.yaml\n", encoding="utf-8")
    write_descriptor(
        assets,
        navigation,
        environment_id=environment_id,
        fingerprint=fingerprint,
    )

    seed_sim_environment(descriptor, assets, data)

    assert (data / ".last_map").read_text(encoding="utf-8") == "apartment.yaml\n"
    marker = json.loads((data / ".last_environment").read_text(encoding="utf-8"))
    assert (marker["id"], marker["fingerprint"]) == (environment_id, fingerprint)


def test_same_environment_repairs_a_stale_saved_map(tmp_path):
    assets = tmp_path / "assets"
    source = assets / "map"
    source.mkdir(parents=True)
    (source / "world.yaml").write_text("image: world.pgm\n", encoding="utf-8")
    (source / "world.pgm").write_bytes(b"P5\n1 1\n255\n\0")
    descriptor = write_descriptor(
        assets,
        {"map_yaml": "map/world.yaml", "map_image": "map/world.pgm"},
    )
    data = tmp_path / "data"
    seed_sim_environment(descriptor, assets, data)
    (data / ".last_map").write_text("missing.yaml\n", encoding="utf-8")

    seed_sim_environment(descriptor, assets, data)

    assert (data / ".last_map").read_text(encoding="utf-8") == "world.yaml\n"


def test_repoints_yaml_to_manifest_selected_image_basename(tmp_path):
    assets = tmp_path / "assets"
    source = assets / "packs" / "gallery" / "nav"
    image = assets / "packs" / "gallery" / "textures" / "floor.pgm"
    source.mkdir(parents=True)
    image.parent.mkdir(parents=True)
    (source / "gallery.yaml").write_text("image: old/subdir/map.pgm\nresolution: 0.05\n", encoding="utf-8")
    image.write_bytes(b"P5\n1 1\n255\n\0")
    descriptor = write_descriptor(
        assets,
        {
            "map_yaml": "packs/gallery/nav/gallery.yaml",
            "map_image": "packs/gallery/textures/floor.pgm",
        },
    )

    selected_yaml, _selected_image = seed_sim_environment(descriptor, assets, tmp_path / "data")

    assert selected_yaml.read_text(encoding="utf-8") == 'image: "floor.pgm"\nresolution: 0.05\n'


@pytest.mark.parametrize("field", ["map_yaml", "map_image"])
def test_requires_both_navigation_files(tmp_path, field):
    assets = tmp_path / "assets"
    assets.mkdir()
    values = {"map_yaml": "map/world.yaml", "map_image": "map/world.pgm"}
    values.pop(field)
    descriptor = write_descriptor(assets, values)

    with pytest.raises(EnvironmentSeedError, match=rf"navigation\.{field}"):
        seed_sim_environment(descriptor, assets, tmp_path / "data")


@pytest.mark.parametrize("field", ["id", "fingerprint"])
def test_requires_environment_identity(tmp_path, field):
    assets = tmp_path / "assets"
    assets.mkdir()
    descriptor = assets / ".active-environment.json"
    payload = {
        "id": "apartment",
        "fingerprint": "apartment-assets-v1",
        "navigation": {"map_yaml": "map/world.yaml", "map_image": "map/world.pgm"},
    }
    payload.pop(field)
    descriptor.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EnvironmentSeedError, match=rf"non-empty {field}"):
        seed_sim_environment(descriptor, assets, tmp_path / "data")


def test_rejects_navigation_path_that_escapes_assets(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("image: outside.pgm\n", encoding="utf-8")
    descriptor = write_descriptor(
        assets,
        {"map_yaml": "../outside.yaml", "map_image": "map/world.pgm"},
    )

    with pytest.raises(EnvironmentSeedError, match="escapes the simulator assets directory"):
        seed_sim_environment(descriptor, assets, tmp_path / "data")


def test_requires_referenced_navigation_files_to_exist(tmp_path):
    assets = tmp_path / "assets"
    source = assets / "map"
    source.mkdir(parents=True)
    (source / "world.yaml").write_text("image: world.pgm\n", encoding="utf-8")
    descriptor = write_descriptor(
        assets,
        {"map_yaml": "map/world.yaml", "map_image": "map/world.pgm"},
    )

    with pytest.raises(EnvironmentSeedError, match="navigation.map_image not found"):
        seed_sim_environment(descriptor, assets, tmp_path / "data")


def test_rejects_symlink_that_escapes_assets(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("image: outside.pgm\n", encoding="utf-8")
    (assets / "linked.yaml").symlink_to(outside)
    descriptor = write_descriptor(
        assets,
        {"map_yaml": "linked.yaml", "map_image": "map/world.pgm"},
    )

    with pytest.raises(EnvironmentSeedError, match="escapes the simulator assets directory"):
        seed_sim_environment(descriptor, assets, tmp_path / "data")
