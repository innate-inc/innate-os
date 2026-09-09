#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Generate Nowhere: a featureless white room where the onboarding begins.

A 20 m square of white floor inside white walls, so lidar and the nav map have
something to see while the cameras see nothing at all. The MuJoCo visuals also
get a white ceiling (not exported to the browser glb) so the robot's cameras
never show sky above the walls.

    cd sim && uv run tools/build_void.py [--viewer-out /out] [--assets-dir /assets]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
import trimesh
from build_environment_pack import write_nav_map, write_visuals
from build_intersection import Z_TO_Y, Square, surface_uv
from build_viewer_physics import hull_soup
from PIL import Image

SIM = Path(__file__).resolve().parents[1]
PACK_ID = "void"
HALF = 10.0  # interior half-width, metres
WALL = 0.5
HEIGHT = 4.0
FLOOR = 0.1


def design() -> tuple[Square, trimesh.Trimesh]:
    room = Square()
    # Visual only, like every pack's floor: a floor hull under the ground plane jams the base.
    room.box((0, 0, -FLOOR / 2), (2 * HALF + 2 * WALL, 2 * HALF + 2 * WALL, FLOOR), "void-floor", solid=False)
    span = 2 * HALF + 2 * WALL
    for x, y, size in (
        (HALF + WALL / 2, 0, (WALL, span, HEIGHT)),
        (-HALF - WALL / 2, 0, (WALL, span, HEIGHT)),
        (0, HALF + WALL / 2, (span, WALL, HEIGHT)),
        (0, -HALF - WALL / 2, (span, WALL, HEIGHT)),
    ):
        room.box((x, y, HEIGHT / 2), size, "void-wall")
    ceiling = trimesh.creation.box(extents=(span, span, 0.05))
    ceiling.apply_translation((0, 0, HEIGHT + 0.025))
    return room, ceiling


def textured(mesh: trimesh.Trimesh, name: str, grey: int) -> trimesh.Trimesh:
    mesh = mesh.copy()
    mesh.unmerge_vertices()
    uv = surface_uv(mesh)
    mesh.apply_transform(Z_TO_Y)
    material = trimesh.visual.material.PBRMaterial(
        name=name,
        baseColorFactor=[255, 255, 255, 255],
        baseColorTexture=Image.new("RGB", (4, 4), (grey, grey, grey)),
        metallicFactor=0,
        roughnessFactor=1,
    )
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=material)
    return mesh


def build(viewer_out: Path = SIM / "viewer/public", assets_dir: Path = SIM / "assets") -> None:
    room, ceiling = design()
    parts = {
        # 235 reads white through the viewer's tone curve (which tops out near 237) yet
        # leaves a shadow room to read; 255 would clip every shading step flat.
        color: textured(trimesh.util.concatenate(meshes), color, 235 if color == "void-floor" else 255)
        for color, meshes in room.parts.items()
    }
    models = viewer_out / "models" / PACK_ID
    models.mkdir(parents=True, exist_ok=True)
    trimesh.Scene(parts).export(models / f"{PACK_ID}.glb")

    visual_root = assets_dir / f"{PACK_ID}_visual"
    if visual_root.exists():
        shutil.rmtree(visual_root)
    write_visuals(PACK_ID, {**parts, "void-ceiling": textured(ceiling, "void-ceiling", 255)}, assets_dir)

    collision_root = assets_dir / f"{PACK_ID}_split_v2" / PACK_ID
    collision_root.mkdir(parents=True, exist_ok=True)
    for path in collision_root.glob(f"{PACK_ID}_collision_*.obj"):
        path.unlink()
    names = []
    for index, hull in enumerate(room.hulls):
        hull.apply_transform(Z_TO_Y)
        name = f"{PACK_ID}_collision_{index:03d}.obj"
        hull.export(collision_root / name)
        names.append(name)
    collisions = viewer_out / "physics" / f"{PACK_ID}_collisions"
    collisions.mkdir(parents=True, exist_ok=True)
    (collisions / "hulls.f32").write_bytes(hull_soup(collision_root, names).astype(np.float32).tobytes())
    (collisions / "manifest.json").write_text("[]\n")
    write_nav_map(PACK_ID, assets_dir, include_collision_hulls=True)
    print(f"Nowhere: {len(parts)} materials, {len(room.hulls)} convex hulls")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer-out", type=Path, default=SIM / "viewer/public")
    parser.add_argument("--assets-dir", type=Path, default=SIM / "assets")
    args = parser.parse_args()
    build(args.viewer_out, args.assets_dir)
