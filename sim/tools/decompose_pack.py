# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The CoACD bake of an environment pack: one OBJ and its convex hulls per mesh
part of a glTF scene, under sim/assets/<id>_split and <id>_split_v2.

    uv run tools/decompose_pack.py <id> <scene.glb> [--threshold M]

The scene's floor -- the up-facing height carrying the most area -- is moved to
y = 0, and flat floor sheets are skipped: the MJCF ground plane stands in for
them. build_environment_pack.py derives everything else from the same scene.
This file is the bake layer's whole cache key in sim/Dockerfile.assets, so it
holds only what changes the hulls.
"""

import argparse
from collections import Counter
from pathlib import Path

import decompose_rooms
import numpy as np
import trimesh
from decompose_rooms import decompose_room

SIM = Path(__file__).resolve().parents[1]
ASSETS = SIM / "assets"
FLAT_SHEET_M = 0.02
Parts = dict[str, trimesh.Trimesh]


def load_parts(glb: Path) -> tuple[Parts, float]:
    """Every mesh part in the scene's Y-up world frame, plus the floor height."""
    scene = trimesh.load(glb, force="scene")
    placements = [(node, *scene.graph[node]) for node in scene.graph.nodes_geometry]
    uses = Counter(geometry for _node, _transform, geometry in placements)
    parts: Parts = {}
    for node, transform, geometry in placements:  # one part per placement: instanced geometry is several
        part = scene.geometry[geometry].copy()
        part.apply_transform(transform)
        parts[geometry if uses[geometry] == 1 else f"{geometry}__{node}"] = part
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pack_id")
    parser.add_argument("glb", type=Path)
    parser.add_argument("--threshold", type=float, default=decompose_rooms.THRESHOLD_M, help="CoACD concavity, metres")
    args = parser.parse_args()
    decompose(args.pack_id, args.glb, args.threshold)


if __name__ == "__main__":
    main()
