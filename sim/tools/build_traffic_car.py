# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Export the MJCF car description as one tintable GLB with rolling wheel nodes."""

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))
from mars_sim_driver.crossroads import CAR_MODEL, SIGNAL_COLORS, SIGNAL_MATERIALS  # noqa: E402, F401


def primitive(part):
    if part["shape"] == "box":
        mesh = trimesh.creation.box(extents=part["size"])
    elif part["shape"] == "cylinder":
        mesh = trimesh.creation.cylinder(radius=part["radius"], height=part["length"], sections=12)
    else:
        # The authored profiles are convex and counterclockwise in X/Z.
        profile, width = part["profile"], part["width"]
        n = len(profile)
        vertices = [[x, y, z] for y in (-width / 2, width / 2) for x, z in profile]
        faces = [[0, i, i + 1] for i in range(1, n - 1)]
        faces += [[n, n + i + 1, n + i] for i in range(1, n - 1)]
        for i in range(n):
            j = (i + 1) % n
            faces.extend(([i, n + i, j], [j, n + i, n + j]))
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.apply_transform(trimesh.transformations.euler_matrix(*part.get("rotation", (0, 0, 0))))
    mesh.apply_translation(part["position"])
    return mesh


def build_car(path: Path) -> None:
    scene = trimesh.Scene()
    # Keep wheel nodes in simulator axes under one standard glTF Y-up root.
    scene.graph.update("car", matrix=trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    wheels = {}
    for part in CAR_MODEL["parts"]:
        if part["shape"] == "cylinder" and part["material"] == "rubber":
            x, y, _ = part["position"]
            node = f"wheel_{len(wheels)}"
            wheels[x, np.sign(y)] = (node, np.array(part["position"]))
            scene.graph.update(
                node,
                frame_from="car",
                translation=part["position"],
                metadata={"rolling_radius": part["rolling_radius"]},
            )
    batches = defaultdict(list)
    for part in CAR_MODEL["parts"]:
        mesh = primitive(part)
        parent = "car"
        if "rolling_radius" in part:
            x, y, _ = part["position"]
            parent, center = wheels[x, np.sign(y)]
            mesh.apply_translation(-center)
        batches[parent, part["material"]].append(mesh)
    batches["car", "collision"] = [primitive(part) for part in CAR_MODEL["colliders"]]
    materials = {}
    for name, color in {"body": "#ffffff", "collision": "#ffffff", **CAR_MODEL["materials"]}.items():
        rgb = tuple(bytes.fromhex(color.lstrip("#")))
        materials[name] = trimesh.visual.material.PBRMaterial(
            name=name,
            baseColorFactor=[255, 255, 255, 255],
            baseColorTexture=None if name == "body" else Image.new("RGB", (4, 4), rgb),
            metallicFactor=0.45 if name == "alloy" else 0.08 if name == "body" else 0.1 if name == "glass" else 0,
            roughnessFactor=0.25 if name == "glass" else 0.62 if name == "body" else 0.72,
            emissiveFactor=np.array(rgb) / 255 * 0.45 if name == "headlight" else None,
        )
    for (parent, name), meshes in batches.items():
        mesh = trimesh.util.concatenate(meshes)
        mesh.unmerge_vertices()
        mesh.visual = trimesh.visual.TextureVisuals(uv=np.zeros((len(mesh.vertices), 2)), material=materials[name])
        node = name if parent == "car" else f"{parent}_{name}"
        scene.add_geometry(mesh, node_name=node, geom_name=node, parent_node_name=parent)
    scene.export(path)
