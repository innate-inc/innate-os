#!/usr/bin/env python3
"""Build Gallery's matching textured robot-camera OBJ and browser GLB props.

Run with sim/.venv/bin/python sim/tools/build_gallery_props.py. Geometry is
Z-up, in metres, at the physics body origin; no external assets are needed.
"""

import math

import numpy as np
import trimesh
from build_pantry_props import SIM, Model, load_props, text
from PIL import Image, ImageDraw


def atlas():
    image = Image.new("RGB", (1024, 512), (27, 86, 153))
    draw = ImageDraw.Draw(image)
    for i in range(3):
        cx = (i + 0.5) * 1024 / 3
        draw.rectangle((i * 1024 / 3, 258, (i + 1) * 1024 / 3, 285), fill=(231, 216, 178))
        text(draw, (cx, 120), "STUDIO", 49, (248, 237, 211))
        text(draw, (cx, 185), "BLUE", 29, (248, 237, 211))
        text(draw, (cx, 329), "SPARKLING WATER", 20, (248, 237, 211))
    colors = [
        (27, 86, 153),
        (188, 198, 202),
        (109, 119, 128),
        (48, 57, 64),
        (183, 43, 35),
        (220, 75, 53),
        (119, 29, 25),
        (246, 228, 198),
    ]
    for i, color in enumerate(colors):
        draw.rectangle((i * 128, 400, (i + 1) * 128 - 1, 511), fill=color)
    return image


def loop(model, center, radii, tube, material, *, horizontal=False):
    vertices, faces, uv = [], [], []
    for i in range(49):
        a = i * math.tau / 48
        for j in range(13):
            b = j * math.tau / 12
            x = (radii[0] + tube * math.cos(b)) * math.cos(a)
            y = tube * math.sin(b)
            z = (radii[1] + tube * math.cos(b)) * math.sin(a)
            if horizontal:
                y, z = z, -y
            vertices.append(np.array(center) + (x, y, z))
            uv.append(((material + 0.5) / 8, 0.1))
            if i < 48 and j < 12:
                k = i * 13 + j
                faces.extend(((k, k + 1, k + 14), (k, k + 14, k + 13)))
    model.surface(vertices, faces, uv)


def can():
    model = Model()
    model.lathe(
        [(0.028, -0.0575), (0.032, -0.0555), (0.033, -0.052), (0.033, 0.043), (0.029, 0.051), (0.029, 0.055)], 1
    )
    model.lathe([(0.0331, -0.049), (0.0331, 0.041)], 0, label=True, caps=False)
    model.lathe([(0.029, 0.051), (0.0305, 0.053), (0.0305, 0.056), (0.0285, 0.0575), (0.027, 0.055)], 1)
    model.lathe([(0.0265, 0.0538), (0.0265, 0.054)], 2)
    loop(model, (0, 0, 0.055), (0.006, 0.010), 0.0018, 1, horizontal=True)
    return model


def mug():
    model = Model()
    # Continuous outer wall, rolled lip and inner wall; the opening is hollow.
    model.lathe(
        [
            (0.020, -0.0279),
            (0.023, -0.026),
            (0.025, 0.023),
            (0.0245, 0.0279),
            (0.0215, 0.0279),
            (0.021, 0.023),
            (0.0185, -0.020),
        ],
        4,
        caps=False,
    )
    model.lathe([(0.0185, -0.021), (0.0185, -0.020)], 6)
    loop(model, (0.027, 0, 0), (0.015, 0.019), 0.004, 4)
    return model


def main():
    texture = atlas()
    for prop in load_props([SIM / "bundles/gallery/props"]).values():
        is_can = "_can_" in prop.name
        model = can() if is_can else mug()
        scale = (1, 1, 1) if is_can else (prop.size[0] / 0.025,) * 2 + (prop.size[1] / 0.0279,)
        mesh = model.mesh(scale)
        mesh.visual = trimesh.visual.TextureVisuals(
            uv=np.array(model.uv),
            material=trimesh.visual.material.PBRMaterial(
                name=prop.name,
                baseColorTexture=texture,
                metallicFactor=0.25 if is_can else 0.0,
                roughnessFactor=0.35,
            ),
        )
        objects = prop.root.parent / "objects"
        texture.save(objects / f"{prop.name}_basecolor.png", optimize=True)
        obj = trimesh.exchange.obj.export_obj(mesh, include_texture=True, write_texture=False)
        obj = (
            "\n".join(line for line in obj.rstrip().splitlines() if not line.startswith(("mtllib ", "usemtl "))) + "\n"
        )
        (objects / f"{prop.name}.obj").write_text(obj)
        viewer = prop.root.parent / "viewer" / prop.viewer["glb"].lstrip("/")
        viewer.parent.mkdir(parents=True, exist_ok=True)
        viewer.write_bytes(mesh.export(file_type="glb"))
        print(f"{prop.name}: {len(mesh.faces)} triangles, {mesh.extents.round(4)} metres")


if __name__ == "__main__":
    main()
