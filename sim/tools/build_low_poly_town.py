#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Build the locally licensed Low Poly Town simulator environment.

The purchased CGTrader source stays under ``sim/viewer/assets`` and every
generated file stays in gitignored ``local-environments`` directories: the
MuJoCo meshes and Nav2 map under ``sim/assets``, the browser files under
``sim/viewer`` (served at /local-environments/, and outside ``sim/viewer/public``
so an asset-image refresh never wipes them), and the manifest under
``sim/environments.local``. This keeps the model usable in the simulator
without publishing retrievable model files from the open repository.

Usage (from the repository root):

    sim/.venv/bin/python sim/tools/build_low_poly_town.py \
      --source-obj sim/viewer/assets/low_poly_town/town.obj \
      --source-mtl sim/viewer/assets/low_poly_town/town.mtl
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import re
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

SIM = Path(__file__).resolve().parents[1]
PACK_ID = "low-poly-town"
PACK_DISPLAY_NAME = "Town Intersection"
PACK_ROOT = Path("local-environments") / PACK_ID

# The asphalt and painted markings share this authored Blender Z coordinate.
# OBJ/glTF export maps Blender Z to glTF Y, so subtracting it puts the visible
# driving surface at simulator ground level without changing the horizontal
# frame. The large gray base slab ends lower and is hidden a little further to
# keep it out of the road's depth plane.
ROAD_SURFACE_Y = 0.543620586
ROAD_MARKING_SOURCE_Y = 0.543620586
BASE_SLAB_SOURCE_TOP_Y = 0.489483
ROAD_MARKING_LIFT = 0.002
BASE_SLAB_CLEARANCE = 0.01
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

SIGNAL_ASPECTS = ("Red", "Yellow", "Green")
SIGNAL_OBJECT_PHASE = {
    "Traffic_Light.001_Cube.009": "NS",
    "Traffic_Light.002_Cube.010": "NS",
    "Traffic_Light.003_Cube.011": "NS",
    "Traffic_Light_Cube.008": "NS",
    "Traffic_Light.009_Cube.018": "NS",
    "Traffic_Light.011_Cube.024": "NS",
    "Traffic_Light.004_Cube.012": "EW",
    "Traffic_Light.005_Cube.013": "EW",
    "Traffic_Light.006_Cube.014": "EW",
    "Traffic_Light.007_Cube.019": "EW",
    "Traffic_Light.008_Cube.016": "EW",
    "Traffic_Light.010_Cube.020": "EW",
}


def isolate_signal_materials(obj_text: str, mtl_text: str) -> tuple[str, str]:
    """Split the source's shared lens colors into NS and EW materials."""
    current_phase: str | None = None
    counts = {(phase, aspect): 0 for phase in ("NS", "EW") for aspect in SIGNAL_ASPECTS}
    rewritten: list[str] = []
    for line in obj_text.splitlines():
        if line.startswith("o "):
            current_phase = SIGNAL_OBJECT_PHASE.get(line.removeprefix("o ").strip())
        if line.startswith("usemtl ") and current_phase is not None:
            aspect = line.removeprefix("usemtl ").strip()
            if aspect in SIGNAL_ASPECTS:
                counts[(current_phase, aspect)] += 1
                line = f"usemtl Signal_{current_phase}_{aspect}"
        rewritten.append(line)
    wrong = {key: count for key, count in counts.items() if count != 6}
    if wrong:
        raise RuntimeError(f"unexpected traffic-light material assignments: {wrong}")

    blocks: dict[str, list[str]] = {}
    current_material: str | None = None
    for line in mtl_text.splitlines():
        if line.startswith("newmtl "):
            current_material = line.removeprefix("newmtl ").strip()
            blocks[current_material] = []
        elif current_material is not None:
            blocks[current_material].append(line)
    missing = [aspect for aspect in SIGNAL_ASPECTS if aspect not in blocks]
    if missing:
        raise RuntimeError(f"town MTL is missing signal source materials: {missing}")

    aliases = ["", "# Simulator-only traffic phase materials (generated)."]
    for phase in ("NS", "EW"):
        for aspect in SIGNAL_ASPECTS:
            aliases.extend((f"newmtl Signal_{phase}_{aspect}", *blocks[aspect]))
    return "\n".join(rewritten) + "\n", mtl_text.rstrip() + "\n" + "\n".join(aliases) + "\n"


@dataclass
class _GeneratedOutput:
    staged: Path
    target: Path
    backup_directory: Path | None = None
    backup: Path | None = None
    published: bool = False


def _path_exists(path: Path) -> bool:
    """Like exists(), but a broken generated symlink still needs replacing."""
    return path.exists() or path.is_symlink()


def _remove_generated_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _staging_directory(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f".{PACK_ID}.build-", dir=parent))
    # mkdtemp intentionally creates 0700 directories. Once renamed, these are
    # served by containers that may not share the invoking user's uid.
    directory.chmod(0o755)
    return directory


def _publish_generated_outputs(outputs: list[_GeneratedOutput]) -> None:
    """Replace every generated target and restore all old targets on error.

    Each rename stays on one filesystem and is atomic. The manifest is passed
    last by build(), so a successful publish exposes the commit marker only
    after both asset trees are in place.
    """
    try:
        for output in outputs:
            output.target.parent.mkdir(parents=True, exist_ok=True)
            if _path_exists(output.target):
                output.backup_directory = Path(
                    tempfile.mkdtemp(prefix=f".{output.target.name}.backup-", dir=output.target.parent)
                )
                output.backup = output.backup_directory / output.target.name
                output.target.replace(output.backup)
            # Mark the slot before rename so KeyboardInterrupt between the
            # atomic filesystem operation and Python bookkeeping still removes
            # a newly published target during rollback.
            output.published = True
            output.staged.replace(output.target)
    except BaseException as publish_error:
        rollback_errors: list[str] = []
        for output in reversed(outputs):
            try:
                if output.published and _path_exists(output.target):
                    _remove_generated_path(output.target)
                if output.backup is not None and _path_exists(output.backup):
                    if _path_exists(output.target):
                        _remove_generated_path(output.target)
                    output.backup.replace(output.target)
            except OSError as rollback_error:
                rollback_errors.append(f"{output.target}: {rollback_error}")

        for output in outputs:
            if output.backup_directory is not None and output.backup_directory.is_dir():
                try:
                    output.backup_directory.rmdir()
                except OSError:
                    # A failed restore deliberately leaves its backup intact.
                    pass

        if rollback_errors:
            details = "; ".join(rollback_errors)
            raise RuntimeError(
                f"town asset publish failed ({publish_error}); rollback was incomplete: {details}"
            ) from publish_error
        raise

    for output in outputs:
        if output.backup_directory is not None:
            shutil.rmtree(output.backup_directory, ignore_errors=True)


def _cross_2d(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    return float((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))


def _triangulate_face(tokens: list[str], vertices: list[tuple[float, float, float]]) -> list[list[str]]:
    """Ear-clip one planar OBJ polygon instead of using a concave-unsafe fan."""
    if len(tokens) == 3:
        return [tokens]

    positions = np.asarray([vertices[int(token.split("/", 1)[0]) - 1] for token in tokens], dtype=np.float64)
    normal = np.zeros(3, dtype=np.float64)
    for point, following in zip(positions, np.roll(positions, -1, axis=0), strict=True):
        normal += np.cross(point, following)
    normal_length = float(np.linalg.norm(normal))
    if normal_length < 1e-12:
        return []
    spatial_scale = max(float(np.ptp(positions, axis=0).max()), 1.0)
    plane_distance = np.abs((positions - positions[0]) @ (normal / normal_length))
    if float(plane_distance.max()) > spatial_scale * 1e-6:
        # A few tiny bevel polygons in the source are intentionally folded in
        # 3D. They have no single 2D interior, so retain their authored fan.
        return [[tokens[0], tokens[index], tokens[index + 1]] for index in range(1, len(tokens) - 1)]

    projected = np.delete(positions, int(np.argmax(np.abs(normal))), axis=1)
    scale = max(float(np.ptp(projected[:, 0])), float(np.ptp(projected[:, 1])), 1.0)
    epsilon = scale * scale * 1e-12
    signed_area = sum(_cross_2d(projected[0], projected[i], projected[i + 1]) for i in range(1, len(tokens) - 1))
    orientation = 1.0 if signed_area > 0 else -1.0
    remaining = list(range(len(tokens)))
    triangles: list[list[str]] = []

    def inside_triangle(point: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> bool:
        return (
            orientation * _cross_2d(a, b, point) >= -epsilon
            and orientation * _cross_2d(b, c, point) >= -epsilon
            and orientation * _cross_2d(c, a, point) >= -epsilon
        )

    while len(remaining) > 3:
        for offset, current in enumerate(remaining):
            previous = remaining[offset - 1]
            following = remaining[(offset + 1) % len(remaining)]
            a, b, c = projected[[previous, current, following]]
            if orientation * _cross_2d(a, b, c) <= epsilon:
                continue
            if any(
                inside_triangle(projected[other], a, b, c)
                for other in remaining
                if other not in {previous, current, following}
            ):
                continue
            triangles.append([tokens[previous], tokens[current], tokens[following]])
            del remaining[offset]
            break
        else:
            raise RuntimeError(f"cannot ear-clip OBJ polygon with {len(tokens)} vertices")

    triangles.append([tokens[index] for index in remaining])
    return triangles


def _triangulate_obj(text: str) -> tuple[str, int]:
    """Triangulate authored n-gons without bridging their concave cutouts."""
    vertices = [tuple(map(float, line.split()[1:4])) for line in text.splitlines() if line.startswith("v ")]
    output: list[str] = []
    polygon_count = 0
    for line in text.splitlines():
        if not line.startswith("f "):
            output.append(line)
            continue
        tokens = line.split()[1:]
        triangles = _triangulate_face(tokens, vertices)
        polygon_count += int(len(tokens) > 3)
        output.extend("f " + " ".join(triangle) for triangle in triangles)
    return "\n".join(output) + "\n", polygon_count


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
        text = source_obj.read_text(encoding="utf-8")
        text, replacements = re.subn(r"(?m)^mtllib\s+.*$", "mtllib town.mtl", text, count=1)
        if replacements != 1:
            text = f"mtllib town.mtl\n{text}"
        text, material_text = isolate_signal_materials(text, source_mtl.read_text(encoding="utf-8"))
        text, polygon_count = _triangulate_obj(text)
        if polygon_count < 1_000:
            raise RuntimeError(f"expected the authored town to contain many n-gons, found {polygon_count}")
        staged_obj.write_text(text, encoding="utf-8")
        staged_mtl.write_text(material_text, encoding="utf-8")
        loaded = trimesh.load(staged_obj, force="scene", process=False, maintain_order=True)

    if not isinstance(loaded, trimesh.Scene) or len(loaded.geometry) < 20:
        raise RuntimeError("the Low Poly Town OBJ did not load as the expected multi-material scene")
    if not np.allclose(loaded.extents, (29.129641, 9.330295, 28.345164), atol=0.15):
        raise RuntimeError(f"unexpected Low Poly Town dimensions: {loaded.extents}")

    for mesh in loaded.geometry.values():
        mesh.apply_translation((0.0, -ROAD_SURFACE_Y, 0.0))
    expected_signal_faces = 864
    signal_faces: dict[str, int] = {}
    for mesh in loaded.geometry.values():
        name = str(getattr(getattr(mesh.visual, "material", None), "name", ""))
        if name.startswith("Signal_"):
            signal_faces[name] = len(mesh.faces)
    expected_names = {f"Signal_{phase}_{aspect}" for phase in ("NS", "EW") for aspect in SIGNAL_ASPECTS}
    if set(signal_faces) != expected_names or any(count != expected_signal_faces for count in signal_faces.values()):
        raise RuntimeError(f"unexpected generated traffic signal meshes: {signal_faces}")
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


def _lower_base_slab(scene: trimesh.Scene) -> None:
    """Move the hidden town slab below coplanar asphalt and landscaping."""
    candidates: list[trimesh.Trimesh] = []
    expected_top = BASE_SLAB_SOURCE_TOP_Y - ROAD_SURFACE_Y
    for mesh in scene.geometry.values():
        material = getattr(mesh.visual, "material", None)
        if (
            str(getattr(material, "name", "")).casefold() == "gray"
            and mesh.extents[0] > 25.0
            and mesh.extents[2] > 25.0
            and abs(float(mesh.bounds[1, 1]) - expected_top) < 0.0002
        ):
            candidates.append(mesh)

    if len(candidates) != 1:
        raise RuntimeError(f"expected one full-town gray base slab, found {len(candidates)}")
    candidates[0].apply_translation((0.0, -BASE_SLAB_CLEARANCE, 0.0))


def _apply_flat_shading(scene: trimesh.Scene) -> None:
    """Replace the OBJ's corrupt shared normals with low-poly face normals."""
    for mesh in scene.geometry.values():
        mesh.unmerge_vertices()
        mesh.vertex_normals = np.repeat(mesh.face_normals, 3, axis=0)


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
    path = manifest_directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _build(source_obj: Path, source_mtl: Path) -> None:
    assets_pack_root = SIM / "assets" / PACK_ROOT
    viewer_pack_root = SIM / "viewer" / PACK_ROOT
    manifest_directory = SIM / "environments.local"
    manifest_path = manifest_directory / PACK_ID / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # Source validation and every in-memory rewrite happen before touching the
    # last working pack. A bad download therefore cannot turn a usable local
    # environment into two empty output directories plus a stale manifest.
    scene = _load_authored_scene(source_obj, source_mtl)
    _lower_base_slab(scene)
    _lift_road_markings(scene)
    _apply_flat_shading(scene)
    _prepare_daylight_materials(scene)

    staging_roots: list[Path] = []
    try:
        assets_stage = _staging_directory(assets_pack_root.parent)
        staging_roots.append(assets_stage)
        viewer_stage = _staging_directory(viewer_pack_root.parent)
        staging_roots.append(viewer_stage)
        manifest_stage = _staging_directory(manifest_directory)
        staging_roots.append(manifest_stage)

        collision_root = assets_stage / "collisions"
        visual_root = assets_stage / "visual"
        map_root = assets_stage / "map"
        viewer_collision_root = viewer_stage / "collisions"
        for directory in (collision_root, visual_root, map_root, viewer_collision_root):
            directory.mkdir(parents=True)

        _write_browser_scene(scene, viewer_stage)
        material_count = _write_mujoco_visuals(scene, visual_root)
        collision_count = _write_collision_proxies(collision_root, viewer_collision_root)
        map_width, map_height = _write_navigation_map(map_root)
        generated_digest = _tree_digest(assets_stage, viewer_stage)
        staged_manifest = _write_local_manifest(source_obj, generated_digest, manifest_stage)

        _publish_generated_outputs(
            [
                _GeneratedOutput(assets_stage, assets_pack_root),
                _GeneratedOutput(viewer_stage, viewer_pack_root),
                _GeneratedOutput(staged_manifest, manifest_path),
            ]
        )
    finally:
        # Published stage paths no longer exist. Failed builds are removed here;
        # rollback backups are separate and are retained if restoration failed.
        for staged in staging_roots:
            with contextlib.suppress(OSError):
                if _path_exists(staged):
                    _remove_generated_path(staged)

    print(
        f"built {PACK_DISPLAY_NAME}: {material_count} visual materials, "
        f"{collision_count} convex collision proxies, {map_width}x{map_height} Nav2 map"
    )
    print(f"manifest: {manifest_path}")


def build(source_obj: Path, source_mtl: Path) -> None:
    lock_path = SIM / "environments.local" / f".{PACK_ID}.build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another {PACK_DISPLAY_NAME} build is already running") from exc
        _build(source_obj, source_mtl)


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
