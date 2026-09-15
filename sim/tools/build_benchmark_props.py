#!/usr/bin/env python3
"""Recognizable benchmark props from one mesh in both renderers.

Run with sim/.venv/bin/python sim/tools/build_benchmark_props.py.
Geometry and packaging are authored here; no external models or fonts.
"""

import numpy as np
import trimesh
from build_gallery_props import can, loop, mug
from build_pantry_props import SIM, Model, load_props, text
from PIL import Image, ImageDraw


def material_image(color, label="", kind=""):
    image = Image.new("RGB", (1024, 512), (238, 229, 204))
    draw = ImageDraw.Draw(image)
    for i in range(3):
        cx = (i + 0.5) * 1024 / 3
        draw.rectangle((i * 1024 / 3 + 8, 8, (i + 1) * 1024 / 3 - 8, 376), fill=color)
        text(draw, (cx, 80), "INNATE", 27, (250, 239, 211))
        text(draw, (cx, 167), label, 31, (250, 239, 211))
        if kind == "medicine":
            draw.rectangle((cx - 15, 214, cx + 15, 311), fill=(250, 239, 211))
            draw.rectangle((cx - 49, 248, cx + 49, 278), fill=(250, 239, 211))
        else:
            draw.rectangle((cx - 100, 250, cx + 100, 255), fill=(250, 239, 211))
            text(draw, (cx, 310), "WORKSHOP" if kind == "can" else "ESSENTIALS", 21, (250, 239, 211))
    colors = [
        color,
        (191, 202, 207),
        (104, 116, 122),
        (24, 31, 36),
        color,
        (228, 193, 138),
        (102, 68, 43),
        (244, 232, 207),
    ]
    for i, c in enumerate(colors):
        draw.rectangle((i * 128, 400, (i + 1) * 128 - 1, 511), fill=c)
    return image


def block(model, half, pos, material):
    mesh = trimesh.creation.box(extents=np.array(half) * 2)
    model.surface(mesh.vertices + pos, mesh.faces, [((material + 0.5) / 8, 0.1)] * len(mesh.vertices))


def teapot():
    m = Model()
    m.lathe([(0.047, -0.055), (0.063, -0.046), (0.075, -0.010), (0.070, 0.025), (0.047, 0.040)], 4)
    m.lathe([(0.049, 0.040), (0.049, 0.044), (0.033, 0.048), (0.010, 0.049), (0.010, 0.055)], 4)
    loop(m, (-0.072, 0, -0.002), (0.030, 0.033), 0.007, 4)
    # A tapered rising spout, separate from the rounded body.
    verts = []
    faces = []
    for x, z, r in ((0.05, -0.018, 0.021), (0.087, 0.002, 0.015), (0.119, 0.037, 0.010)):
        for i in range(25):
            a = i * np.pi * 2 / 24
            verts.append((x, r * np.cos(a), z + r * np.sin(a)))
    for row in range(2):
        for i in range(24):
            k = row * 25 + i
            faces.extend(((k, k + 25, k + 26), (k, k + 26, k + 1)))
    m.surface(verts, faces, [(4.5 / 8, 0.1)] * len(verts))
    return m


def box_prop(prop, kind):
    m = Model()
    hx, hy, hz = prop.size
    if kind == "book":
        block(m, (hx * 0.94, hy * 0.93, hz * 0.78), (0, 0, 0), 7)
        for z in (-hz * 0.90, hz * 0.90):
            block(m, (hx, hy, hz * 0.10), (0, 0, z), 4)
        block(m, (hx * 0.045, hy, hz), (-hx * 0.955, 0, 0), 4)
        block(m, (hx * 0.50, hy * 0.13, hz * 0.015), (0, 0, hz * 1.015), 5)
    elif kind == "phone":
        block(m, prop.size, (0, 0, 0), 3)
        block(m, (hx * 0.89, hy * 0.84, hz * 0.04), (0, 0, hz * 1.04), 0)
        for x in (-hx * 0.3, 0, hx * 0.3):
            block(m, (hx * 0.08, hy * 0.12, hz * 0.02), (x, -hy * 0.46, hz * 1.10), 7)
    elif kind == "photo":
        block(m, prop.size, (0, 0, 0), 6)
        block(m, (hx * 0.88, hy * 0.08, hz * 0.88), (0, -hy * 1.08, 0), 7)
        block(m, (hx * 0.75, hy * 0.06, hz * 0.73), (0, -hy * 1.24, 0), 0)
        for x, z in ((-hx * 0.3, hz * 0.2), (hx * 0.3, hz * 0.05)):
            block(m, (hx * 0.13, hy * 0.04, hz * 0.15), (x, -hy * 1.35, z), 5)
            block(m, (hx * 0.20, hy * 0.04, hz * 0.20), (x, -hy * 1.35, z - hz * 0.35), 4)
    else:
        m.box(prop.size)
        if kind == "towels":
            for z in (-hz * 0.5, 0, hz * 0.5):
                block(m, (hx * 1.004, hy * 1.004, hz * 0.025), (0, 0, z), 7)
    return m


def build(prop):
    name = prop.name
    if "cup_" in name or "mug_" in name:
        kind = "cup"
        model = mug()
        scale = (prop.size[0] / 0.025,) * 2 + (prop.size[1] / 0.0279,)
        color = (
            (25, 81, 173)
            if "blue" in name
            else (35, 121, 69)
            if "green" in name
            else (232, 181, 35)
            if "hidden" in name
            else (193, 27, 31)
        )
        texture = material_image(color)
    elif "teapot" in name:
        kind = "teapot"
        model = teapot()
        scale = (1, 1, 1)
        texture = material_image((32, 111, 69) if name.endswith("teapot") else (62, 78, 90))
    elif "medicine" in name:
        kind = "medicine"
        model = Model()
        scale = (1, 1, 1)
        model.lathe([(0.019, -0.0314), (0.024, -0.027), (0.025, 0.013), (0.019, 0.019), (0.018, 0.026)], 7)
        model.lathe([(0.0251, -0.020), (0.0251, 0.012)], 0, label=True, caps=False)
        model.lathe([(0.019, 0.024), (0.020, 0.026), (0.020, 0.0314)], 7, knurl=True)
        texture = material_image((154, 36, 36), "MEDICINE", kind)
    elif len(prop.size) == 2:
        kind = "can"
        model = can()
        scale = (prop.size[0] / 0.033,) * 2 + (prop.size[1] / 0.0575,)
        texture = material_image(
            (182, 112, 33) if "gauge" in name else (26, 88, 125), "OIL" if "gauge" in name else "PAINT", kind
        )
    else:
        kind = "book" if "book" in name else name.split("_")[-1]
        model = box_prop(prop, kind)
        scale = (1, 1, 1)
        color = {
            "book": (164, 29, 30),
            "phone": (28, 80, 118),
            "photo": (93, 137, 133),
            "documents": (44, 88, 117),
            "towels": (127, 162, 154),
        }[kind]
        texture = material_image(color, kind.upper(), kind)
    mesh = model.mesh(scale)
    mesh.visual = trimesh.visual.TextureVisuals(
        uv=np.array(model.uv),
        material=trimesh.visual.material.PBRMaterial(
            name=name, baseColorTexture=texture, metallicFactor=0.18 if kind == "can" else 0, roughnessFactor=0.4
        ),
    )
    objects = prop.root.parent / "objects"
    texture.save(objects / f"{name}_basecolor.png", optimize=True)
    obj = trimesh.exchange.obj.export_obj(mesh, include_texture=True, write_texture=False)
    (objects / f"{name}.obj").write_text(
        "\n".join(line for line in obj.rstrip().splitlines() if not line.startswith(("mtllib ", "usemtl "))) + "\n"
    )
    glb = prop.root.parent / "viewer" / prop.viewer["glb"].lstrip("/")
    glb.parent.mkdir(parents=True, exist_ok=True)
    glb.write_bytes(mesh.export(file_type="glb"))
    print(name, mesh.extents.round(4))


def main():
    for world in ("rounds", "workshop", "counter", "blaze"):
        for prop in load_props([SIM / "bundles" / world / "props"]).values():
            if prop.name != "counter_jar_jam":
                build(prop)


if __name__ == "__main__":
    main()
