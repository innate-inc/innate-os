#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build small, textured grocery props for BOTH MuJoCo and the browser.

    sim/.venv/bin/python sim/tools/build_pantry_props.py

Outputs are tracked beside each bundle: OBJ + PNG for the robot camera and
GLB for the viewer. No downloaded models, external fonts, or network access.
The same vertices/UVs feed both formats, in metres, Z-up, at the original
body origin. Collision dimensions, mass, spawn heights and goals are owned
by the sidecars and are deliberately not changed here.

Glass is opaque and tinted to suggest filled glass jars: transparent glass
would make RGB/depth disagree, and is unreliable in the offscreen renderer.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFont

SIM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM.parent / "ros2_ws/src/mars_bot/mars_sim_driver"))
from mars_sim_driver.props import load_props  # noqa: E402

# Contents, label ink, lid. Names here are product labels, never answers or
# hints about which bay an object belongs in (especially the misfiled jar).
PRODUCTS = {
    "pantry_jar_honey": ("HONEY", (176, 112, 25), (99, 61, 22), (192, 155, 78)),
    "pantry_jar_jam": ("BERRY JAM", (114, 30, 43), (115, 33, 51), (172, 48, 51)),
    "pantry_jar_curd": ("LEMON CURD", (215, 180, 68), (78, 96, 43), (173, 154, 72)),
    "pantry_jar_pick": ("PICKLES", (73, 97, 35), (41, 77, 51), (48, 89, 56)),
    "pantry_jar_new": ("BLUEBERRY", (49, 45, 78), (47, 58, 91), (68, 75, 99)),
    "pantry_jar_stray": ("BERRY JAM", (114, 30, 43), (115, 33, 51), (172, 48, 51)),
    "counter_jar_jam": ("BERRY JAM", (114, 30, 43), (115, 33, 51), (172, 48, 51)),
    "pantry_carton_oats": ("OATS", (226, 204, 158), (55, 87, 120), (55, 87, 120)),
    "pantry_carton_rice": ("RICE", (236, 223, 190), (110, 65, 43), (110, 65, 43)),
    "pantry_carton_tea": ("TEA", (220, 220, 182), (43, 84, 63), (43, 84, 63)),
    "pantry_carton_new": ("OATS", (226, 204, 158), (152, 67, 45), (152, 67, 45)),
    "pantry_tin_large": ("BISCUITS", (189, 151, 100), (39, 88, 83), (172, 181, 180)),
    "pantry_tin_small": ("MINTS", (203, 216, 203), (61, 104, 98), (172, 181, 180)),
}


def text(draw, xy, value, size, color):
    draw.text(xy, value, font=ImageFont.load_default(size=size), fill=color, anchor="mm")


def motif(draw, cx, cy, name, scale=1.0):
    """Simple packaging illustrations, legible without reading the fine print."""

    def ellipse(box, color):
        draw.ellipse(tuple((cx if i % 2 == 0 else cy) + v * scale for i, v in enumerate(box)), fill=color)

    if name == "HONEY":
        for dx, dy in ((-22, 0), (22, 0), (0, -36), (0, 36)):
            points = [
                (cx + (dx + 23 * math.cos(a * math.pi / 3)) * scale, cy + (dy + 23 * math.sin(a * math.pi / 3)) * scale)
                for a in range(6)
            ]
            draw.polygon(points, fill=(215, 162, 47), outline=(133, 87, 22), width=3)
    elif name in ("BERRY JAM", "BLUEBERRY"):
        berry = (137, 39, 57) if name == "BERRY JAM" else (58, 60, 102)
        for dx, dy in ((-25, 5), (14, 16), (0, -19)):
            ellipse((dx - 21, dy - 21, dx + 21, dy + 21), berry)
            ellipse((dx - 12, dy - 13, dx - 4, dy - 6), (208, 163, 157))
        ellipse((-7, -48, 30, -27), (76, 112, 57))
    elif name == "LEMON CURD":
        ellipse((-49, -28, 49, 28), (224, 183, 39))
        ellipse((-25, -46, 12, -28), (87, 121, 63))
    elif name == "PICKLES":
        for dx in (-26, 5):
            ellipse((dx - 13, -39, dx + 13, 39), (76, 113, 46))
            for dy in (-22, -4, 18):
                ellipse((dx - 4, dy, dx + 2, dy + 5), (163, 173, 90))
    elif name in ("OATS", "RICE"):
        for dx in (-22, 0, 22):
            draw.line(
                (cx + dx * scale, cy + 44 * scale, cx + dx * scale, cy - 38 * scale),
                fill=(137, 103, 54),
                width=max(1, round(3 * scale)),
            )
            for dy in (-30, -10, 10):
                ellipse((dx - 15, dy - 8, dx, dy + 7), (194, 155, 87))
                ellipse((dx, dy - 8, dx + 15, dy + 7), (215, 181, 119))
    elif name in ("TEA", "MINTS"):
        for dx, dy in ((-18, -12), (17, 11), (-10, 26)):
            ellipse((dx - 21, dy - 13, dx + 21, dy + 13), (78, 122, 70))
    else:
        for dx in (-24, 22):
            ellipse((dx - 26, -28, dx + 26, 24), (200, 158, 88))
            for dy in (-12, 5):
                ellipse((dx - 8, dy, dx - 4, dy + 4), (118, 87, 48))


def atlas(name, kind):
    product, contents, ink, lid = PRODUCTS[name]
    im = Image.new("RGB", (1024, 512), (238, 230, 207))
    draw = ImageDraw.Draw(im)
    # Top 3/4: label; bottom strip: materials sampled by geometry UVs.
    repeats = 1 if kind == "carton" else 3
    width = 1024 / repeats
    for i in range(repeats):
        x0, x1 = int(i * width), int((i + 1) * width)
        cx = (x0 + x1) / 2
        draw.rectangle((x0 + 7, 8, x1 - 7, 373), fill=(242, 233, 211), outline=ink, width=4)
        draw.rectangle((x0 + 14, 15, x1 - 14, 63), fill=ink)
        text(draw, (cx, 38), "FIELD & PANTRY", 21 if repeats > 1 else 34, (249, 239, 214))
        text(draw, (cx, 110), product, 35 if repeats > 1 else 70, ink)
        motif(draw, cx, 223, product, 1.05 if repeats > 1 else 1.4)
        text(draw, (cx, 325), "SMALL BATCH" if kind == "jar" else "PANTRY ESSENTIALS", 19 if repeats > 1 else 26, ink)
        draw.line((x0 + 30, 294, x1 - 30, 294), fill=ink, width=2)
    colors = [contents, lid, (190, 199, 185), (224, 227, 212), (94, 104, 98), ink, (198, 178, 143), (238, 230, 207)]
    for i, color in enumerate(colors):
        draw.rectangle((i * 128, 400, (i + 1) * 128 - 1, 511), fill=color)
    # A subtle contents/glass texture lives in the first swatch. No alpha:
    # robot depth and RGB must see the same physical envelope.
    rng = np.random.default_rng(7)
    for _ in range(240):
        x, y = int(rng.integers(0, 128)), int(rng.integers(402, 510))
        shade = float(rng.uniform(0.85, 1.15))
        draw.ellipse((x, y, x + 2, y + 2), fill=tuple(min(255, int(c * shade)) for c in contents))
    return im


class Model:
    def __init__(self):
        self.vertices, self.faces, self.uv = [], [], []

    def surface(self, vertices, faces, uv):
        start = len(self.vertices)
        self.vertices.extend(vertices)
        self.faces.extend([[start + i for i in face] for face in faces])
        self.uv.extend(uv)

    def lathe(self, profile, material, *, label=False, knurl=False, caps=True):
        n = 64
        vertices, uv, faces = [], [], []
        for j, (radius, z) in enumerate(profile):
            for i in range(n + 1):
                angle = i / n * 2 * math.pi
                r = radius * (1 - 0.018 * (i % 2)) if knurl else radius
                vertices.append((r * math.cos(angle), r * math.sin(angle), z))
                uv.append(
                    (i / n, 0.25 + 0.75 * j / (len(profile) - 1))
                    if label
                    else ((material + 0.1 + 0.8 * i / n) / 8, 0.015 + 0.18 * j / (len(profile) - 1))
                )
        for j in range(len(profile) - 1):
            for i in range(n):
                a, b = j * (n + 1) + i, (j + 1) * (n + 1) + i
                faces.extend(((a, a + 1, b + 1), (a, b + 1, b)))
        if caps:
            for j, up in ((0, False), (len(profile) - 1, True)):
                center = len(vertices)
                vertices.append((0, 0, profile[j][1]))
                uv.append(((material + 0.5) / 8, 0.1))
                for i in range(n):
                    a = j * (n + 1) + i
                    faces.append((center, a, a + 1) if up else (center, a + 1, a))
        self.surface(vertices, faces, uv)

    def box(self, half, *, material=6):
        mesh = trimesh.creation.box(extents=np.array(half) * 2)
        # Separate faces so packaging does not smear round corners.
        mesh.unmerge_vertices()
        vertices, uv = mesh.vertices.tolist(), []
        hx, _hy, hz = half
        for (x, _y, z), normal in zip(mesh.vertices, np.repeat(mesh.face_normals, 3, axis=0), strict=True):
            if abs(normal[1]) > 0.5:  # front and back product panels
                uv.append((0.5 + (-x if normal[1] > 0 else x) / (2 * hx), 0.25 + 0.75 * (z + hz) / (2 * hz)))
            else:
                uv.append(((material + 0.5) / 8, 0.1))
        self.surface(vertices, mesh.faces.tolist(), uv)

    def mesh(self, size):
        mesh = trimesh.Trimesh(vertices=np.array(self.vertices) * size, faces=self.faces, process=False)
        return mesh


def jar():
    model = Model()
    model.lathe(
        [
            (0.036, -0.054),
            (0.041, -0.052),
            (0.043, -0.048),
            (0.0418, -0.042),
            (0.0418, 0.025),
            (0.0405, 0.031),
            (0.036, 0.037),
            (0.033, 0.040),
            (0.033, 0.046),
        ],
        0,
    )
    # Glass foot and neck visible above/below the paper label.
    model.lathe([(0.036, -0.054), (0.041, -0.052), (0.043, -0.049), (0.042, -0.047)], 2)
    model.lathe([(0.0332, 0.038), (0.0337, 0.040), (0.0337, 0.045)], 2)
    model.lathe([(0.04205, -0.032), (0.04205, 0.022)], 7, label=True, caps=False)
    # Rolled metal rim, ribbed screw-cap edge and shallow stamped top.
    model.lathe([(0.034, 0.042), (0.039, 0.043), (0.040, 0.045), (0.040, 0.051), (0.038, 0.054)], 1, knurl=True)
    model.lathe([(0.0355, 0.0535), (0.0355, 0.054)], 1)
    return model


def tin():
    model = Model()
    model.lathe(
        [(0.068, -0.028), (0.0725, -0.025), (0.0705, -0.022), (0.0705, 0.022), (0.0725, 0.025), (0.069, 0.028)], 2
    )
    model.lathe([(0.0707, -0.020), (0.0707, 0.020)], 7, label=True, caps=False)
    model.lathe([(0.060, 0.027), (0.065, 0.028), (0.069, 0.028)], 4)
    model.lathe([(0.058, 0.027), (0.060, 0.0275)], 2)
    # Recessed lid and embossed concentric rings, visually unlike a jar cap.
    model.lathe([(0.0005, 0.0268), (0.058, 0.0268)], 2)
    return model


def build(prop):
    kind = "jar" if "_jar_" in prop.name else "carton" if "_carton_" in prop.name else "tin"
    if kind == "jar":
        model, scale = jar(), (prop.size[0] / 0.043, prop.size[0] / 0.043, prop.size[1] / 0.054)
    elif kind == "tin":
        model, scale = tin(), (prop.size[0] / 0.0725, prop.size[0] / 0.0725, prop.size[1] / 0.028)
    else:
        model, scale = Model(), (1, 1, 1)
        model.box(prop.size)
    mesh = model.mesh(scale)
    texture = atlas(prop.name, kind)
    mesh.visual = trimesh.visual.TextureVisuals(
        uv=np.array(model.uv),
        material=trimesh.visual.material.PBRMaterial(
            name=prop.name,
            baseColorTexture=texture,
            metallicFactor=0.25 if kind == "tin" else 0.05,
            roughnessFactor=0.65 if kind == "carton" else 0.32,
        ),
    )
    bundle = prop.root.parent
    objects = bundle / "objects"
    objects.mkdir(exist_ok=True)
    texture.save(objects / f"{prop.name}_basecolor.png", optimize=True)
    # MuJoCo reads the OBJ UVs; material/texture are supplied by Prop.assets_xml.
    obj = trimesh.exchange.obj.export_obj(mesh, include_texture=True, write_texture=False)
    obj = "\n".join(line for line in obj.rstrip().splitlines() if not line.startswith(("mtllib ", "usemtl "))) + "\n"
    (objects / f"{prop.name}.obj").write_text(obj)
    viewer = bundle / "viewer" / "models" / "groceries"
    viewer.mkdir(parents=True, exist_ok=True)
    # preNormalized in the sidecar keeps these Z-up body-local vertices exact.
    (viewer / f"{prop.name}.glb").write_bytes(mesh.export(file_type="glb"))
    print(f"{prop.name}: {len(mesh.faces)} triangles, bounds {mesh.extents.round(4)}")


def main():
    for bundle in ("pantry", "counter"):
        for prop in load_props([SIM / "bundles" / bundle / "props"]).values():
            if prop.name in PRODUCTS:
                build(prop)


if __name__ == "__main__":
    main()
