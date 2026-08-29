#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Build the locally licensed Low Poly Town simulator environment.

The purchased CGTrader source stays under ``sim/viewer/assets`` and every
generated file stays in gitignored ``local-environments`` directories. This
keeps the model usable in the simulator without publishing retrievable model
files from the open repository.

Usage (from the repository root):

    sim/.venv/bin/python sim/tools/build_low_poly_town.py \
      --source-obj sim/viewer/assets/low_poly_town/town.obj \
      --source-mtl sim/viewer/assets/low_poly_town/town.mtl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from collections import deque
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

SIM = Path(__file__).resolve().parents[1]
PACK_ID = "low-poly-town"
PACK_DISPLAY_NAME = "Town Intersection"
PACK_ROOT = Path("local-environments") / PACK_ID

# The authored road mesh sits on top of the town's base slab at this Blender Z
# coordinate. OBJ/glTF export maps Blender Z to glTF Y, so subtracting it puts
# the asphalt at simulator ground level without changing the horizontal frame.
ROAD_SURFACE_Y = 0.489483
ROAD_MARKING_SOURCE_Y = 0.543620586
ROAD_MARKING_LIFT = 0.002
# The rendered sidewalks are about 10 cm above the road after the town is
# grounded. Keep the collision proxy faithful to that visible step so MARS
# does not encounter the previous, artificial 22 cm curb.
CURB_COLLISION_HEIGHT = 0.10

MAP_RESOLUTION = 0.05
MAP_ORIGIN_X = -15.0
MAP_ORIGIN_Y = -15.0
MAP_MAX_X = 15.0
MAP_MAX_Y = 15.0
PGM_OCCUPIED = 0
PGM_UNKNOWN = 205
PGM_FREE = 254

SPAWN_X = 1.6
SPAWN_Y = -9.0
SPAWN_YAW_DEGREES = 90.0

# (world x, world y). Four mast-and-arm signals sit just inside the corners;
# the remaining pedestrian signals sit along the curb. Only their narrow masts
# collide: the overhead signal arms deliberately remain passable underneath.
TRAFFIC_LIGHT_CENTERS = (
    (-6.5135, -3.8356),
    (-3.6347, -3.6142),
    (-3.8561, -6.4930),
    (6.7733, -3.8356),
    (4.1159, -6.4930),
    (3.8945, -3.6142),
    (6.7733, 4.1364),
    (4.1159, 6.7938),
    (3.8945, 3.9150),
    (-6.5135, 4.1364),
    (-3.8561, 6.7938),
    (-3.6347, 3.9150),
)


def _safe_reset(directory: Path, expected_parent: Path) -> None:
    directory = directory.resolve()
    expected_parent = expected_parent.resolve()
    if directory.parent != expected_parent:
        raise RuntimeError(f"refusing to replace unexpected generated directory: {directory}")
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)


def _load_authored_scene(source_obj: Path, source_mtl: Path) -> trimesh.Scene:
    if not source_obj.is_file() or not source_mtl.is_file():
        raise FileNotFoundError(
            "Low Poly Town source is not installed. Copy the purchased OBJ and MTL to "
            "sim/viewer/assets/low_poly_town/town.obj and town.mtl."
        )

    # The marketplace download names the files with '+' while the OBJ points
    # at a space-separated MTL name. Stage canonical names so every resolver,
    # including trimesh's, loads the 25 authored materials deterministically.
    with tempfile.TemporaryDirectory(prefix="innate-low-poly-town-") as temporary:
        staged = Path(temporary)
        staged_obj = staged / "town.obj"
        staged_mtl = staged / "town.mtl"
        shutil.copy2(source_mtl, staged_mtl)
        text = source_obj.read_text(encoding="utf-8")
        text, replacements = re.subn(r"(?m)^mtllib\s+.*$", "mtllib town.mtl", text, count=1)
        if replacements != 1:
            text = f"mtllib town.mtl\n{text}"
        staged_obj.write_text(text, encoding="utf-8")
        loaded = trimesh.load(staged_obj, force="scene", process=False, maintain_order=True)

    if not isinstance(loaded, trimesh.Scene) or len(loaded.geometry) < 20:
        raise RuntimeError("the Low Poly Town OBJ did not load as the expected multi-material scene")
    if not np.allclose(loaded.extents, (29.129641, 9.330295, 28.345164), atol=0.15):
        raise RuntimeError(f"unexpected Low Poly Town dimensions: {loaded.extents}")

    for mesh in loaded.geometry.values():
        mesh.apply_translation((0.0, -ROAD_SURFACE_Y, 0.0))
    return loaded


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "material"


def _write_obj(mesh: trimesh.Trimesh, path: Path) -> None:
    compact = mesh.copy()
    compact.remove_unreferenced_vertices()
    lines = [*(f"v {x:.8g} {y:.8g} {z:.8g}" for x, y, z in compact.vertices)]
    lines.extend(f"f {a + 1} {b + 1} {c + 1}" for a, b, c in compact.faces)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _lift_road_markings(scene: trimesh.Scene) -> None:
    """Separate the coplanar white markings from the asphalt depth plane."""
    candidates: list[trimesh.Trimesh] = []
    expected_height = ROAD_MARKING_SOURCE_Y - ROAD_SURFACE_Y
    for mesh in scene.geometry.values():
        material = getattr(mesh.visual, "material", None)
        if (
            str(getattr(material, "name", "")).casefold() == "white"
            and mesh.extents[1] < 0.0002
            and abs(float(mesh.centroid[1]) - expected_height) < 0.0002
        ):
            candidates.append(mesh)

    if len(candidates) != 1:
        raise RuntimeError(f"expected one flat white road-marking mesh, found {len(candidates)}")
    candidates[0].apply_translation((0.0, ROAD_MARKING_LIFT, 0.0))


def _prepare_daylight_materials(scene: trimesh.Scene) -> None:
    """Convert the OBJ's linear diffuse colors for our sRGB renderers.

    The marketplace preview was authored through Blender color management, but
    OBJ ``Kd`` values are linear light. Treating those values as already-sRGB
    makes the town nearly black in both Three.js and MuJoCo. Keep the authored
    palette while encoding it for display and use a matte low-poly finish.
    """
    for index, mesh in enumerate(scene.geometry.values()):
        source = getattr(mesh.visual, "material", None)
        linear = np.asarray(getattr(source, "main_color", (160, 160, 160, 255))[:3], dtype=np.float64) / 255.0
        srgb = np.where(
            linear <= 0.0031308,
            linear * 12.92,
            1.055 * np.power(linear, 1.0 / 2.4) - 0.055,
        )
        # Full sRGB encoding is too bright under the simulator's high-exposure
        # daylight rig. Split the difference with the authored linear value:
        # dark colors remain legible without washing out roads and façades.
        display = 0.5 * (linear + srgb)
        color = np.concatenate((np.clip(np.rint(display * 255.0), 0, 255), [255])).astype(np.uint8)
        name = str(getattr(source, "name", "") or f"material-{index}")
        mesh.visual.material = trimesh.visual.material.PBRMaterial(
            name=name,
            baseColorFactor=color,
            metallicFactor=0.0,
            roughnessFactor=0.9,
        )


def _write_browser_scene(scene: trimesh.Scene, viewer_pack_root: Path) -> None:
    payload = scene.export(file_type="glb")
    if not isinstance(payload, bytes) or not payload.startswith(b"glTF"):
        raise RuntimeError("failed to export a binary glTF town scene")
    (viewer_pack_root / "scene.glb").write_bytes(payload)


def _write_mujoco_visuals(scene: trimesh.Scene, visual_root: Path) -> int:
    used_names: set[str] = set()
    for index, mesh in enumerate(scene.geometry.values()):
        material = getattr(mesh.visual, "material", None)
        material_name = str(getattr(material, "name", "") or f"material-{index}")
        base_name = _slug(material_name)
        name = base_name
        suffix = 2
        while name in used_names:
            name = f"{base_name}-{suffix}"
            suffix += 1
        used_names.add(name)

        room = visual_root / name
        room.mkdir()
        _write_obj(mesh, room / f"{name}.obj")
        color = tuple(int(channel) for channel in getattr(material, "main_color", (160, 160, 160, 255))[:3])
        Image.new("RGB", (4, 4), color).save(room / f"{name}.png")
    return len(used_names)


def _box_mesh(
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    bottom: float,
    top: float,
) -> trimesh.Trimesh:
    # glTF/OBJ Y is up. Simulator world Y maps to negative glTF Z, matching
    # Blender's standard Y-up export and the driver's +90-degree X rotation.
    vertices = np.asarray(
        [
            (xmin, bottom, -ymin),
            (xmax, bottom, -ymin),
            (xmax, bottom, -ymax),
            (xmin, bottom, -ymax),
            (xmin, top, -ymin),
            (xmax, top, -ymin),
            (xmax, top, -ymax),
            (xmin, top, -ymax),
        ],
        dtype=np.float64,
    )
    faces = np.asarray(
        [
            (0, 2, 1),
            (0, 3, 2),
            (4, 5, 6),
            (4, 6, 7),
            (0, 1, 5),
            (0, 5, 4),
            (1, 2, 6),
            (1, 6, 5),
            (2, 3, 7),
            (2, 7, 6),
            (3, 0, 4),
            (3, 4, 7),
        ],
        dtype=np.int64,
    )
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def _collision_proxies() -> list[tuple[str, trimesh.Trimesh]]:
    proxies = [
        ("southwest-curb", _box_mesh(-14.04, -4.30, -14.02, -4.28, -0.05, CURB_COLLISION_HEIGHT)),
        ("southeast-curb", _box_mesh(4.56, 14.30, -14.02, -4.28, -0.05, CURB_COLLISION_HEIGHT)),
        ("northwest-curb", _box_mesh(-14.04, -4.30, 4.58, 14.32, -0.05, CURB_COLLISION_HEIGHT)),
        ("northeast-curb", _box_mesh(4.56, 14.30, 4.58, 14.32, -0.05, CURB_COLLISION_HEIGHT)),
        ("west-boundary", _box_mesh(-14.35, -14.04, -14.32, 14.62, -0.05, 0.75)),
        ("east-boundary", _box_mesh(14.30, 14.61, -14.32, 14.62, -0.05, 0.75)),
        ("south-boundary", _box_mesh(-14.04, 14.30, -14.33, -14.02, -0.05, 0.75)),
        ("north-boundary", _box_mesh(-14.04, 14.30, 14.32, 14.63, -0.05, 0.75)),
    ]
    for index, (x, y) in enumerate(TRAFFIC_LIGHT_CENTERS):
        proxies.append((f"traffic-light-{index:02d}", _box_mesh(x - 0.18, x + 0.18, y - 0.18, y + 0.18, 0.12, 2.65)))
    return proxies


def _write_collision_proxies(collision_root: Path, viewer_collision_root: Path) -> int:
    room = collision_root / "town"
    room.mkdir()
    names: list[str] = []
    soup: list[np.ndarray] = []
    for index, (_label, mesh) in enumerate(_collision_proxies()):
        name = f"town_collision_{index:03d}.obj"
        _write_obj(mesh, room / name)
        _write_obj(mesh, viewer_collision_root / name)
        names.append(name)
        soup.append(mesh.vertices[mesh.faces].astype(np.float32).reshape(-1, 3))

    (viewer_collision_root / "manifest.json").write_text(json.dumps(names, indent=2) + "\n", encoding="utf-8")
    (viewer_collision_root / "hulls.f32").write_bytes(np.concatenate(soup).astype(np.float32).tobytes())
    return len(names)


def _cell_index(grid: np.ndarray, x: float, y: float) -> tuple[int, int]:
    return (
        int(np.floor((y - MAP_ORIGIN_Y) / MAP_RESOLUTION)),
        int(np.floor((x - MAP_ORIGIN_X) / MAP_RESOLUTION)),
    )


def _reachable_free_cells(grid: np.ndarray, start: tuple[int, int]) -> set[tuple[int, int]]:
    reached = {start}
    pending = deque([start])
    while pending:
        row, col = pending.popleft()
        for candidate in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
            next_row, next_col = candidate
            if (
                0 <= next_row < grid.shape[0]
                and 0 <= next_col < grid.shape[1]
                and candidate not in reached
                and grid[candidate] == 0
            ):
                reached.add(candidate)
                pending.append(candidate)
    return reached


def _write_navigation_map(map_root: Path) -> tuple[int, int]:
    columns = round((MAP_MAX_X - MAP_ORIGIN_X) / MAP_RESOLUTION)
    rows = round((MAP_MAX_Y - MAP_ORIGIN_Y) / MAP_RESOLUTION)
    xs = MAP_ORIGIN_X + (np.arange(columns) + 0.5) * MAP_RESOLUTION
    ys = MAP_ORIGIN_Y + (np.arange(rows) + 0.5) * MAP_RESOLUTION
    x, y = np.meshgrid(xs, ys)

    grid = np.full((rows, columns), -1, dtype=np.int8)
    town = (x >= -14.04) & (x <= 14.30) & (y >= -14.02) & (y <= 14.32)
    grid[town] = 100
    horizontal_road = (x >= -13.90) & (x <= 14.16) & (y >= -4.10) & (y <= 4.40)
    vertical_road = (x >= -4.12) & (x <= 4.38) & (y >= -13.88) & (y <= 14.18)
    grid[town & (horizontal_road | vertical_road)] = 0

    for pole_x, pole_y in TRAFFIC_LIGHT_CENTERS:
        pole = (np.abs(x - pole_x) <= 0.24) & (np.abs(y - pole_y) <= 0.24)
        grid[pole] = 100

    spawn = _cell_index(grid, SPAWN_X, SPAWN_Y)
    if grid[spawn] != 0:
        raise RuntimeError("town spawn is not on known-free road")
    reachable = _reachable_free_cells(grid, spawn)
    if len(reachable) != int(np.count_nonzero(grid == 0)):
        raise RuntimeError("town navigation roads are not one connected component")

    image = np.where(grid == 100, PGM_OCCUPIED, np.where(grid == 0, PGM_FREE, PGM_UNKNOWN)).astype(np.uint8)
    Image.fromarray(image[::-1]).save(map_root / "town.pgm")
    (map_root / "town.yaml").write_text(
        "image: town.pgm\n"
        "mode: trinary\n"
        f"resolution: {MAP_RESOLUTION}\n"
        f"origin: [{MAP_ORIGIN_X:.4f}, {MAP_ORIGIN_Y:.4f}, 0.0]\n"
        "negate: 0\n"
        "occupied_thresh: 0.65\n"
        "free_thresh: 0.196\n",
        encoding="utf-8",
    )
    return columns, rows


def _tree_digest(*directories: Path) -> str:
    digest = hashlib.sha256()
    for directory in directories:
        for path in sorted(candidate for candidate in directory.rglob("*") if candidate.is_file()):
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _write_local_manifest(source_obj: Path, generated_digest: str, manifest_directory: Path) -> Path:
    source_digest = hashlib.sha256(source_obj.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "id": PACK_ID,
        "display_name": PACK_DISPLAY_NAME,
        "coordinate_system": "gltf-y-up-meters",
        "physics": {
            "collision_dir": f"{PACK_ROOT.as_posix()}/collisions",
            "visual_dir": f"{PACK_ROOT.as_posix()}/visual",
        },
        "viewer": {
            "type": "glb",
            "model": f"{PACK_ROOT.as_posix()}/scene.glb",
            "collision_dir": f"{PACK_ROOT.as_posix()}/collisions",
        },
        "navigation": {
            "map_yaml": f"{PACK_ROOT.as_posix()}/map/town.yaml",
            "map_image": f"{PACK_ROOT.as_posix()}/map/town.pgm",
        },
        "spawn": {"x": SPAWN_X, "y": SPAWN_Y, "yaw_degrees": SPAWN_YAW_DEGREES},
        "attribution": {
            "name": "Low Poly Town",
            "author": "im-blendin-it",
            "license": "CGTrader Royalty Free License (local licensed asset; do not redistribute)",
            "source": (
                "https://www.cgtrader.com/3d-models/exterior/cityscape/"
                "low-poly-town-6f87eee7-7975-46ca-99c0-33aedbc0b4c4"
            ),
            "source_sha256": source_digest,
            "generated_assets_sha256": generated_digest,
        },
    }
    manifest_directory.mkdir(parents=True, exist_ok=True)
    path = manifest_directory / f"{PACK_ID}.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build(source_obj: Path, source_mtl: Path) -> None:
    assets_pack_root = SIM / "assets" / PACK_ROOT
    viewer_pack_root = SIM / "viewer" / "public" / PACK_ROOT
    manifest_directory = SIM / "environments.local"
    _safe_reset(assets_pack_root, SIM / "assets" / PACK_ROOT.parent)
    _safe_reset(viewer_pack_root, SIM / "viewer" / "public" / PACK_ROOT.parent)

    collision_root = assets_pack_root / "collisions"
    visual_root = assets_pack_root / "visual"
    map_root = assets_pack_root / "map"
    viewer_collision_root = viewer_pack_root / "collisions"
    for directory in (collision_root, visual_root, map_root, viewer_collision_root):
        directory.mkdir(parents=True)

    scene = _load_authored_scene(source_obj, source_mtl)
    _lift_road_markings(scene)
    _prepare_daylight_materials(scene)
    _write_browser_scene(scene, viewer_pack_root)
    material_count = _write_mujoco_visuals(scene, visual_root)
    collision_count = _write_collision_proxies(collision_root, viewer_collision_root)
    map_width, map_height = _write_navigation_map(map_root)
    generated_digest = _tree_digest(assets_pack_root, viewer_pack_root)
    manifest_path = _write_local_manifest(source_obj, generated_digest, manifest_directory)

    print(
        f"built {PACK_DISPLAY_NAME}: {material_count} visual materials, "
        f"{collision_count} convex collision proxies, {map_width}x{map_height} Nav2 map"
    )
    print(f"manifest: {manifest_path}")


def parse_args() -> argparse.Namespace:
    source_root = SIM / "viewer" / "assets" / "low_poly_town"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-obj", type=Path, default=source_root / "town.obj")
    parser.add_argument("--source-mtl", type=Path, default=source_root / "town.mtl")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build(args.source_obj.resolve(), args.source_mtl.resolve())


if __name__ == "__main__":
    main()
