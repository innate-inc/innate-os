#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Generate Crossroads, an original, redistributable low-poly town square.

No downloaded scenery, textures, or local environment packs are inputs. All
dimensions below are metres in simulator Z-up coordinates. The same authored
convex primitives supply the viewer and MuJoCo; no approximate CoACD bake is
needed. The navigation map is then scanned from the compiled static world.

    cd sim && uv run tools/build_intersection.py [--viewer-out /out]
"""

from __future__ import annotations

import argparse
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np
import trimesh
from build_environment_pack import write_nav_map, write_visuals
from PIL import Image

SIM = Path(__file__).resolve().parents[1]
PACK_ID = "intersection"
Z_TO_Y = trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0])
PALETTE = {
    "asphalt": "485460",
    "paving": "d9d3c4",
    "curb": "eee6d5",
    "paint": "fff5da",
    "yellow": "edc35c",
    "metal": "303e48",
    "glass": "395e70",
    "glass-light": "73949e",
    "wood": "9f6547",
    "leaf": "729b79",
    "leaf-light": "97b18a",
    "soil": "66554a",
    "water": "70b9bf",
    "terracotta": "c77759",
    "sage": "96b2a1",
    "cream": "e4c99c",
    "blue": "7995ab",
    "roof": "52636e",
    "awning": "bd6050",
    "lamp": "ffe5ad",
    **{
        f"Signal_{group}_{aspect}": color
        for group in ("NS", "EW")
        for aspect, color in (("Red", "ff4b55"), ("Yellow", "ffd45a"), ("Green", "5ee27a"))
    },
}


def surface_texture(color: str) -> Image.Image:
    """Small, repeatable 2 m tiles, shared by the GLB and robot-camera OBJ.

    These are authored here, not sampled from third-party images. Fine grain
    gives way to the base palette in mipmaps instead of sparkling at distance.
    """
    rgb = np.array(tuple(bytes.fromhex(PALETTE[color])), dtype=float)
    if color.startswith("Signal_") or color in {"glass", "glass-light", "lamp", "metal"}:
        return Image.new("RGB", (4, 4), tuple(rgb.astype(int)))
    rng = np.random.default_rng(14)
    y, x = np.mgrid[:256, :256]
    detail = rng.normal(0, 2, (256, 256))
    if color == "asphalt":
        detail *= 2.5
    elif color == "paving":
        # Half-metre stone slabs; subtly different stones, recessed joints.
        detail += rng.uniform(-5, 5, (4, 4))[y // 64, x // 64]
        detail[(x % 64 < 2) | (y % 64 < 2)] -= 28
    elif color in {"terracotta", "blue", "sage", "cream"}:
        # Painted brick still needs readable mortar. The cream corner uses
        # larger limestone blocks; all façades have actual surface structure.
        h, w = (64, 128) if color == "cream" else (32, 64)
        row, column = y // h, (x + (y // h % 2) * (w // 2)) % 256 // w
        detail += rng.uniform(-7, 7, (256 // h, 256 // w))[row, column]
        joints = (y % h < 2) | ((x + (y // h % 2) * (w // 2)) % w < 2)
        detail[joints] += 25 if color == "terracotta" else -30
    elif color == "wood":
        detail += 9 * np.sin(x * np.pi / 8 + 0.7 * np.sin(y * np.pi / 64))
        detail[x % 64 < 2] -= 24
    elif color in {"leaf", "leaf-light", "soil"}:
        mottling = Image.fromarray(rng.integers(0, 255, (32, 32), dtype=np.uint8))
        detail += (np.asarray(mottling.resize((256, 256), Image.Resampling.BILINEAR)).astype(float) - 128) * 0.15
    elif color == "water":
        detail = 5 * np.sin((x + y) * np.pi / 32) + 3 * np.sin((x - y) * np.pi / 16)
    elif color == "roof":
        detail[y % 32 < 2] -= 14
    return Image.fromarray(np.clip(rgb + detail[:, :, None], 0, 255).astype(np.uint8))


def surface_uv(mesh: trimesh.Trimesh) -> np.ndarray:
    """Box-project in Z-up world metres, before the export-axis conversion.

    Vertices are unmerged so a corner can have a different UV on each face;
    walls keep vertical grain and horizontal faces share aligned paving.
    """
    axes = np.argmax(np.abs(mesh.face_normals), axis=1)
    uv = np.empty((len(mesh.vertices), 2))
    for normal_axis, plane in enumerate(((1, 2), (0, 2), (0, 1))):
        indices = mesh.faces[axes == normal_axis].ravel()
        uv[indices] = mesh.vertices[indices][:, plane] / 2
    return uv


class Square:
    def __init__(self):
        self.parts: dict[str, list[trimesh.Trimesh]] = defaultdict(list)
        self.hulls: list[trimesh.Trimesh] = []

    def add(self, mesh, color, solid=True):
        if solid:
            self.hulls.append(mesh.copy())
        if color is not None:  # None makes a collision-only boundary
            self.parts[color].append(mesh)

    def box(self, center, size, color, solid=True):
        mesh = trimesh.creation.box(extents=size)
        mesh.apply_translation(center)
        self.add(mesh, color, solid)

    def cylinder(self, center, radius, height, color, solid=True, rotation=0):
        mesh = trimesh.creation.cylinder(radius=radius, height=height, sections=12)
        if rotation:
            mesh.apply_transform(trimesh.transformations.rotation_matrix(rotation, [1, 0, 0]))
        mesh.apply_translation(center)
        self.add(mesh, color, solid)

    def tree(self, x, y, scale=1.0):
        self.cylinder((x, y, 0.3), 0.8, 0.4, "curb")
        self.cylinder((x, y, 0.51), 0.68, 0.025, "soil", False)
        self.cylinder((x, y, 1.5), 0.16, 2.2, "wood")
        for z, r, color in ((3.1, 1.15, "leaf"), (3.95, 0.85, "leaf-light")):
            mesh = trimesh.creation.icosphere(subdivisions=1, radius=r)
            mesh.apply_scale([scale, scale, scale * 1.15])
            mesh.apply_translation([x, y, z])
            self.add(mesh, color)
        # Smaller asymmetrical lobes soften the two stacked canopy shapes.
        # Visual-only: keep the original convex collision envelope and cost.
        for dx, dy, z, r in ((-0.65, 0.2, 3.0, 0.65), (0.55, -0.4, 3.25, 0.75), (0.15, 0.5, 3.65, 0.65)):
            mesh = trimesh.creation.icosphere(subdivisions=1, radius=r * scale)
            mesh.apply_scale([1, 0.9, 1.15])
            mesh.apply_translation([x + dx * scale, y + dy * scale, z])
            self.add(mesh, "leaf-light" if dx > 0 else "leaf", False)

    def building(self, x, y, w, d, height, color):
        self.box((x, y, height / 2 + 0.1), (w, d, height), color)
        self.box((x, y, 0.32), (w + 0.15, d + 0.15, 0.44), "curb")
        self.box((x, y, height + 0.18), (w + 0.4, d + 0.4, 0.22), "roof")
        self.box((x + 0.6, y + 0.3, height + 0.5), (1.2, 1.4, 0.45), "metal")
        # Both street-facing elevations, with windows proud of the wall so
        # there are no coplanar decal faces (including at shallow angles).
        sx, sy = -np.sign(x), -np.sign(y)
        for z in np.arange(1.55, height - 0.3, 2.1):
            for offset in np.arange(-w / 2 + 1, w / 2 - 0.5, 1.7):
                self.box((x + offset, y + sy * (d / 2 + 0.035), z), (1.05, 0.05, 1.25), "glass", False)
                self.box((x + offset, y + sy * (d / 2 + 0.09), z - 0.67), (1.2, 0.2, 0.09), "curb", False)
            for offset in np.arange(-d / 2 + 1, d / 2 - 0.5, 1.7):
                self.box((x + sx * (w / 2 + 0.035), y + offset, z), (0.05, 1.05, 1.25), "glass-light", False)
        self.box((x, y + sy * (d / 2 + 0.45), 2.55), (w * 0.85, 1.0, 0.16), "awning", False)
        self.box((x, y + sy * (d / 2 + 0.045), 1.05), (0.85, 0.065, 1.85), "wood", False)

    def sidewalks(self):
        # MARS has an x/y/yaw-only base, so every drivable surface must be
        # at z=0. Raised border curbs stop short of the flush crossing cuts.
        for sx in (-1, 1):
            for sy in (-1, 1):
                # The world's single ground plane supplies floor contact.
                # A second hull here adds friction contacts that pin wheels.
                self.box((sx * 11.75, sy * 11.75, -0.1), (16.5, 16.5, 0.2), "paving", False)
                for a, b in ((3.5, 4.5), (6.1, 20)):
                    self.box((sx * 3.55, sy * (a + b) / 2, 0.08), (0.1, b - a, 0.16), "curb")
                    self.box((sx * (a + b) / 2, sy * 3.55, 0.08), (b - a, 0.1, 0.16), "curb")

    def signal(self, x, y, direction, group):
        # Head faces the approaching lane; opposite approaches share aspects.
        self.cylinder((x, y, 1.9), 0.075, 3.6, "metal")
        self.box((x, y, 0.25), (0.3, 0.3, 0.3), "curb")
        arm = trimesh.creation.box(extents=(2.2, 0.12, 0.12))
        arm.apply_translation([-1, 0, 3.65])
        head = trimesh.creation.box(extents=(0.46, 0.3, 1.15))
        head.apply_translation([-2, 0, 3.25])
        transform = trimesh.transformations.rotation_matrix(direction, [0, 0, 1])
        transform[:3, 3] = [x, y, 0]
        for mesh in (arm, head):
            mesh.apply_transform(transform)
            self.add(mesh, "metal")
        for z, aspect in ((3.59, "Red"), (3.25, "Yellow"), (2.91, "Green")):
            lens = trimesh.creation.cylinder(radius=0.135, height=0.04, sections=12)
            lens.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
            lens.apply_translation([-2, -0.175, z])
            lens.apply_transform(transform)
            self.add(lens, f"Signal_{group}_{aspect}", False)


def design() -> Square:
    square = Square()
    # Tile the cross without overlapping the sidewalks or each other. A full
    # asphalt slab 5 mm below paving z-fights through it in distant overviews.
    square.box((0, 0, -0.1), (7, 40, 0.2), "asphalt", False)
    for sign in (-1, 1):
        square.box((sign * 11.75, 0, -0.1), (16.5, 7, 0.2), "asphalt", False)
    square.sidewalks()
    for sign in (-1, 1):
        for along in np.arange(8.5, 19.5, 2.8):
            for across in (-0.12, 0.12):
                square.box((across, sign * along, 0.004), (0.07, 1.6, 0.008), "yellow", False)
                square.box((sign * along, across, 0.004), (1.6, 0.07, 0.008), "yellow", False)
        for cross in np.arange(-3.1, 3.2, 0.7):
            square.box((cross, sign * 5.3, 0.004), (0.38, 1.5, 0.008), "paint", False)
            square.box((sign * 5.3, cross, 0.004), (1.5, 0.38, 0.008), "paint", False)
        # Right-hand traffic: stop lines precede crossings and front bumpers.
        square.box((sign * 1.8, -sign * 6.5, 0.004), (3.2, 0.22, 0.008), "paint", False)
        square.box((-sign * 6.5, -sign * 1.8, 0.004), (0.22, 3.2, 0.008), "paint", False)
        # Keep the continuous collision boundary, but leave all four road
        # mouths visually open. The visible fence stops at the sidewalks.
        square.box((sign * 20.1, 0, 0.35), (0.2, 40.4, 0.7), None)
        square.box((0, sign * 20.1, 0.35), (40, 0.2, 0.7), None)
        for side in (-1, 1):
            square.box((sign * 20.1, side * 11.85, 0.35), (0.2, 16.7, 0.7), "curb", False)
            square.box((side * 11.75, sign * 20.1, 0.35), (16.5, 0.2, 0.7), "curb", False)
    square.signal(3.9, -6.8, 0, "NS")
    square.signal(-3.9, 6.8, np.pi, "NS")
    square.signal(-6.8, -3.9, -np.pi / 2, "EW")
    square.signal(6.8, 3.9, np.pi / 2, "EW")
    for spec in (
        (-13, -13, 9, 8, 7.6, "terracotta"),
        (13, -14, 9, 6, 5.5, "sage"),
        (-14, 12.5, 7, 10, 9.7, "cream"),
        (14, 16, 10, 5, 5.1, "blue"),
    ):
        square.building(*spec)
    # The fourth corner opens into a plaza instead of repeating a block.
    square.cylinder((11, 9, 0.3), 1.8, 0.4, "curb")
    square.cylinder((11, 9, 0.51), 1.55, 0.025, "water", False)
    square.cylinder((11, 9, 0.85), 0.45, 0.7, "curb")
    for x, y, scale in (
        (7, 11, 1),
        (16, 8, 1.2),
        (-7, 16, 0.9),
        (-7, -9, 0.85),
        (7, -17, 0.95),
        (-17, -7, 1),
        (17, -7, 0.9),
        (-17, 6, 1),
    ):
        square.tree(x, y, scale)
    for x, y in ((8, 7), (15, 11), (-6.5, 10), (10, -7)):
        for dx in (-0.65, 0.65):
            square.box((x + dx, y, 0.35), (0.12, 0.5, 0.5), "metal")
        square.box((x, y, 0.64), (1.8, 0.55, 0.12), "wood")
        square.box((x, y + 0.25, 0.97), (1.8, 0.1, 0.6), "wood")
    for x, y in ((4.2, 14), (-4.2, -14), (14, -4.2), (-14, 4.2)):
        square.cylinder((x, y, 2.0), 0.065, 3.8, "metal")
        square.box((x, y, 3.95), (0.45, 0.45, 0.2), "lamp", False)
        square.box((x, y, 4.1), (0.6, 0.6, 0.1), "metal", False)
    return square


def build(viewer_out: Path = SIM / "viewer/public", assets_dir: Path = SIM / "assets") -> None:
    square = design()
    parts = {}
    for color, meshes in square.parts.items():
        mesh = trimesh.util.concatenate(meshes)
        mesh.unmerge_vertices()  # intentional flat shading, even on foliage
        uv = surface_uv(mesh)
        mesh.apply_transform(Z_TO_Y)
        # Texture pixels are sRGB in glTF; bare baseColorFactor values are
        # linear. Encoding the palette as textures keeps it consistent
        # with the MuJoCo PNGs instead of washing out under the daylight rig.
        texture = surface_texture(color)
        material = trimesh.visual.material.PBRMaterial(
            name=color,
            baseColorFactor=[255, 255, 255, 255],
            baseColorTexture=texture,
            metallicFactor=0,
            roughnessFactor=1,
        )
        mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=material)
        parts[color.lower().replace("_", "-")] = mesh
    models = viewer_out / "models" / PACK_ID
    models.mkdir(parents=True, exist_ok=True)
    trimesh.Scene(parts).export(models / f"{PACK_ID}.glb")
    # Only this generated pack tree is ours; renamed parts must not survive
    # into world.find_visual_rooms() or the static lidar scan below.
    visual_root = assets_dir / f"{PACK_ID}_visual"
    if visual_root.exists():
        shutil.rmtree(visual_root)
    write_visuals(PACK_ID, parts, assets_dir)
    collision_root = assets_dir / f"{PACK_ID}_split_v2" / PACK_ID
    collision_root.mkdir(parents=True, exist_ok=True)
    # This tool owns this generated directory. Remove stale hulls on rebuild.
    for path in collision_root.glob(f"{PACK_ID}_collision_*.obj"):
        path.unlink()
    soup = []
    for index, mesh in enumerate(square.hulls):
        mesh.apply_transform(Z_TO_Y)
        mesh.export(collision_root / f"{PACK_ID}_collision_{index:03d}.obj")
        soup.append(mesh.triangles.reshape(-1, 3))
    collisions = viewer_out / "physics" / f"{PACK_ID}_collisions"
    collisions.mkdir(parents=True, exist_ok=True)
    (collisions / "hulls.f32").write_bytes(np.concatenate(soup).astype("<f4").tobytes())
    (collisions / "manifest.json").write_text("[]\n")
    write_nav_map(PACK_ID, assets_dir, include_collision_hulls=True)
    print(f"Crossroads: {len(parts)} materials, {len(square.hulls)} convex hulls; no external source assets")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer-out", type=Path, default=SIM / "viewer/public")
    parser.add_argument("--assets-dir", type=Path, default=SIM / "assets")
    args = parser.parse_args()
    build(args.viewer_out, args.assets_dir)
