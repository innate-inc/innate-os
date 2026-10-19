#!/usr/bin/env python3
"""Build Household's matching textured robot-camera OBJ and browser GLB props.

Run with sim/.venv/bin/python sim/tools/build_household_props.py. The mug is
Gallery's geometry with its own glaze: the kitchen mug is BLUE, so that it
never reads as the red display mugs, and so that it stands out on a pale
kitchen floor. No external assets are needed.
"""

import numpy as np
import trimesh
from build_gallery_props import mug
from build_pantry_props import SIM, load_props
from PIL import Image, ImageDraw


def atlas():
    """Gallery's swatch layout, with the glaze slots the mug reads from
    (outer wall at 4, inner floor at 6) set to the kitchen mug's blue."""
    image = Image.new("RGB", (1024, 512), (238, 233, 222))
    draw = ImageDraw.Draw(image)
    colors = [
        (27, 86, 153),
        (188, 198, 202),
        (109, 119, 128),
        (48, 57, 64),
        (43, 107, 199),  # outer glaze: the prop's rgba (0.17, 0.42, 0.78)
        (220, 75, 53),
        (24, 58, 112),  # inside of the mug, darker
        (246, 228, 198),
    ]
    for i, color in enumerate(colors):
        draw.rectangle((i * 128, 400, (i + 1) * 128 - 1, 511), fill=color)
    return image


def main():
    texture = atlas()
    for prop in load_props([SIM / "bundles/household/props"]).values():
        model = mug()
        scale = (prop.size[0] / 0.025,) * 2 + (prop.size[1] / 0.0279,)
        mesh = model.mesh(scale)
        mesh.visual = trimesh.visual.TextureVisuals(
            uv=np.array(model.uv),
            material=trimesh.visual.material.PBRMaterial(
                name=prop.name, baseColorTexture=texture, metallicFactor=0.0, roughnessFactor=0.35
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
