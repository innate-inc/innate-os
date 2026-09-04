# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Derive an environment pack from one glTF scene: MuJoCo collision hulls and
textured meshes under assets/, a lidar-consistent Nav2 map, and the browser's
glb and hull soup. Two steps, in the asset image's stage order
(sim/Dockerfile.assets) so the CoACD bake caches on its own:

    uv run tools/build_environment_pack.py decompose <id> <scene.glb> [--threshold M]
    uv run tools/build_environment_pack.py finish <id> <scene.glb> [viewer_out]

`finish` reads sim/environments/<id>/manifest.json: the nav map is rasterised
from the compiled world like export_nav_map.py, and the manifest's spawn must
land on known-free floor. The scene's floor -- the up-facing height carrying
the most area -- is moved to y = 0 for physics and viewer alike.
"""

import argparse
import json
import struct
import sys
from pathlib import Path

import decompose_rooms
import numpy as np
import trimesh
from build_viewer_physics import hull_soup
from decompose_rooms import decompose_room
from PIL import Image

SIM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM / "sandbox"))
import _driver_pkg  # noqa: E402,F401
import export_nav_map as nav  # noqa: E402

ASSETS = SIM / "assets"
DEFAULT_VIEWER_OUT = SIM / "viewer" / "public"
FLAT_SHEET_M = 0.02  # a part this thin in y is a floor plane; the MJCF ground plane stands in for it
Parts = dict[str, trimesh.Trimesh]


def load_parts(glb: Path) -> tuple[Parts, float]:
    """Every mesh part in the scene's Y-up world frame, plus the floor height."""
    scene = trimesh.load(glb, force="scene")
    parts = {name: geom.copy() for name, geom in scene.geometry.items()}
    for node in scene.graph.nodes_geometry:
        transform, name = scene.graph[node]
        parts[name].apply_transform(transform)
    whole = trimesh.util.concatenate(list(parts.values()))
    up = whole.face_normals[:, 1] > 0.95
    heights, index = np.unique(np.round(whole.triangles_center[up][:, 1], 3), return_inverse=True)
    floor_y = float(heights[np.bincount(index, weights=whole.area_faces[up]).argmax()])
    for geom in parts.values():
        geom.apply_translation([0.0, -floor_y, 0.0])
    return parts, floor_y


def decompose(pack_id: str, glb: Path, threshold: float) -> None:
    parts, floor_y = load_parts(glb)
    print(f"{pack_id}: floor was at y={floor_y:+.3f}, now at 0")
    decompose_rooms.THRESHOLD_M = threshold
    split, hulls = ASSETS / f"{pack_id}_split", ASSETS / f"{pack_id}_split_v2"
    split.mkdir(parents=True, exist_ok=True)
    for name, geom in parts.items():
        if geom.bounds[1][1] - geom.bounds[0][1] < FLAT_SHEET_M:
            continue
        obj = split / f"{name}.obj"
        geom.export(obj)
        print(f"{name}: {len(geom.faces)} faces")
        print(f"    -> {decompose_room(obj, hulls / name)} hulls")


def finish(pack_id: str, glb: Path, viewer_out: Path) -> None:
    parts, floor_y = load_parts(glb)
    write_visuals(pack_id, parts)
    write_nav_map(pack_id)
    write_viewer(pack_id, glb, -floor_y, viewer_out)


def write_visuals(pack_id: str, parts: Parts) -> None:
    """One textured OBJ + PNG per part: what the robot's cameras render and the
    lidar hits (world.find_visual_rooms). Like export_visual_rooms.py, except
    trimesh resolves these scenes' textures, so it can do the reading."""
    out = ASSETS / f"{pack_id}_visual"
    for name, geom in parts.items():
        material = geom.visual.material
        image = getattr(material, "baseColorTexture", None)
        if image is None:
            rgba = getattr(material, "baseColorFactor", None)
            image = Image.new("RGB", (4, 4), tuple(int(c) for c in (rgba if rgba is not None else (200,) * 4)[:3]))
        part_dir = out / name
        part_dir.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(part_dir / f"{name}.png")
        # No normals: MuJoCo derives them, and trimesh's would want scipy.
        lines = [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in geom.vertices]
        lines += [f"vt {u:.6f} {v:.6f}" for u, v in geom.visual.uv]
        lines += [f"f {a + 1}/{a + 1} {b + 1}/{b + 1} {c + 1}/{c + 1}" for a, b, c in geom.faces]
        (part_dir / f"{name}.obj").write_text("\n".join(lines) + "\n")
    print(f"{pack_id}: {len(parts)} textured parts in {out}")


def write_nav_map(pack_id: str) -> None:
    from mars_sim_driver.core import VirtualMars
    from mars_sim_driver.environments import Environment

    environment = Environment.load(pack_id, ASSETS)
    sim = VirtualMars(environment=environment)
    grid, ox, oy = sim.lidar_occupancy_grid(nav.RESOLUTION)
    # The grid's bounds come from mesh bounding spheres, which long flat floor
    # planes inflate to twice the building; keep one metre around the known.
    rows, cols = np.nonzero(grid != -1)
    margin = int(1.0 / nav.RESOLUTION)
    r0, r1 = max(rows.min() - margin, 0), min(rows.max() + margin + 1, grid.shape[0])
    c0, c1 = max(cols.min() - margin, 0), min(cols.max() + margin + 1, grid.shape[1])
    grid = grid[r0:r1, c0:c1].copy()
    ox, oy = ox + c0 * nav.RESOLUTION, oy + r0 * nav.RESOLUTION
    grid[0, :] = grid[-1, :] = grid[:, 0] = grid[:, -1] = -1

    spawn_x, spawn_y, _yaw = environment.spawn
    if nav._cell_value(grid, ox, oy, spawn_x, spawn_y) != 0:
        raise RuntimeError(f"{pack_id}: the manifest's spawn ({spawn_x:.2f}, {spawn_y:.2f}) is not known-free floor")
    img = np.where(grid == 100, nav.PGM_OCCUPIED, np.where(grid == 0, nav.PGM_FREE, nav.PGM_UNKNOWN))
    img = img.astype(np.uint8)[::-1]
    nav._validate_pgm_roundtrip(grid, img)
    out = ASSETS / "map"
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out / f"{pack_id}.pgm")
    (out / f"{pack_id}.yaml").write_text(
        f"image: {pack_id}.pgm\nmode: trinary\nresolution: {nav.RESOLUTION}\norigin: [{ox:.4f}, {oy:.4f}, 0.0]\n"
        f"negate: 0\noccupied_thresh: {nav.OCCUPIED_THRESHOLD}\nfree_thresh: {nav.FREE_THRESHOLD}\n"
    )
    print(f"{pack_id}: nav map {grid.shape[1]}x{grid.shape[0]} @ {nav.RESOLUTION} m, origin ({ox:.2f}, {oy:.2f})")


def write_viewer(pack_id: str, glb: Path, dy: float, viewer_out: Path) -> None:
    models = viewer_out / "models" / pack_id
    models.mkdir(parents=True, exist_ok=True)
    write_shifted_glb(glb, models / f"{pack_id}.glb", dy)
    collisions = viewer_out / "physics" / f"{pack_id}_collisions"
    collisions.mkdir(parents=True, exist_ok=True)
    hull_dir = ASSETS / f"{pack_id}_split_v2"
    names = sorted(path.relative_to(hull_dir).as_posix() for path in hull_dir.glob("*/*_collision_*.obj"))
    (collisions / "hulls.f32").write_bytes(hull_soup(hull_dir, names).tobytes())
    (collisions / "manifest.json").write_text("[]\n")  # the soup is the whole overlay; no per-hull fallback
    print(f"{pack_id}: viewer glb and {len(names)}-hull soup in {viewer_out}")


def write_shifted_glb(src: Path, dst: Path, dy: float) -> None:
    """Copy a GLB with its scene roots raised by dy, editing only the JSON chunk
    so every mesh, texture and UV set survives byte for byte."""
    data = src.read_bytes()
    _magic, version, _length = struct.unpack_from("<4sII", data, 0)
    json_length = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20 : 20 + json_length])
    binary = data[20 + json_length :]
    for index in gltf["scenes"][gltf.get("scene", 0)]["nodes"]:
        node = gltf["nodes"][index]
        if "matrix" in node:  # column-major; a node with a matrix ignores translation
            node["matrix"][13] += dy
        else:
            translation = node.get("translation", [0.0, 0.0, 0.0])
            node["translation"] = [translation[0], translation[1] + dy, translation[2]]
    payload = json.dumps(gltf, separators=(",", ":")).encode()
    payload += b" " * (-len(payload) % 4)
    header = struct.pack("<4sII", b"glTF", version, 20 + len(payload) + len(binary))
    dst.write_bytes(header + struct.pack("<I4s", len(payload), b"JSON") + payload + binary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=("decompose", "finish"))
    parser.add_argument("pack_id")
    parser.add_argument("glb", type=Path)
    parser.add_argument("viewer_out", type=Path, nargs="?", default=DEFAULT_VIEWER_OUT)
    parser.add_argument("--threshold", type=float, default=decompose_rooms.THRESHOLD_M, help="CoACD concavity, metres")
    args = parser.parse_args()
    if args.step == "decompose":
        decompose(args.pack_id, args.glb, args.threshold)
    else:
        finish(args.pack_id, args.glb, args.viewer_out)


if __name__ == "__main__":
    main()
