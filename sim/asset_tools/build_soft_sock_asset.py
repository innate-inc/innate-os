#!/usr/bin/env python3
"""Build a real-time MuJoCo/Three.js sock from the authored cloth bundle.

Two sewn panels have a planar rest metric, a small initial opening, and an open cuff.

* ``cloth_data.npz`` for MuJoCo flex topology and rest-dihedral bending.
* a textured, double-sided GLB plus a skin map for Three.js. Panel skins are
  identity maps, so rendered folds match the collision surface exactly.

Both outputs use metres, Z-up, an identity object transform, and a local frame
whose XY centre and lowest Z point are at the origin.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image
from trimesh.visual.material import PBRMaterial
from trimesh.visual.texture import TextureVisuals


def _build_hinges(vertices: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return oriented manifold hinges and their rest state."""
    incidence: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for a, b, c in faces:
        for start, end, opposite in ((a, b, c), (b, c, a), (c, a, b)):
            incidence.setdefault(tuple(sorted((int(start), int(end)))), []).append(
                (int(start), int(end), int(opposite))
            )

    hinges = []
    for adjacent in incidence.values():
        if len(adjacent) != 2:
            continue
        (edge0, edge1, opposite0), (_other0, _other1, opposite1) = adjacent
        hinges.append((opposite0, opposite1, edge0, edge1))

    h = np.asarray(hinges, dtype=np.int32)
    x0, x1, x2, x3 = vertices[h[:, 0]], vertices[h[:, 1]], vertices[h[:, 2]], vertices[h[:, 3]]
    n1 = np.cross(x2 - x0, x3 - x0)
    n2 = np.cross(x3 - x1, x2 - x1)
    n1 /= np.maximum(np.linalg.norm(n1, axis=1, keepdims=True), 1.0e-12)
    n2 /= np.maximum(np.linalg.norm(n2, axis=1, keepdims=True), 1.0e-12)
    edge = x3 - x2
    lengths = np.linalg.norm(edge, axis=1)
    edge_hat = edge / np.maximum(lengths[:, None], 1.0e-12)
    sine = np.einsum("ij,ij->i", np.cross(n1, n2), edge_hat)
    cosine = np.einsum("ij,ij->i", n1, n2)
    return h, np.arctan2(sine, cosine), lengths


def _topology_counts(faces: np.ndarray) -> tuple[int, int]:
    incidence: dict[tuple[int, int], int] = {}
    for face in faces:
        for a, b in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            key = tuple(sorted((int(a), int(b))))
            incidence[key] = incidence.get(key, 0) + 1
    return (
        sum(count == 1 for count in incidence.values()),
        sum(count > 2 for count in incidence.values()),
    )


def _cross_section_points(vertices: np.ndarray, faces: np.ndarray, z: float) -> np.ndarray:
    """Intersect the authored triangles with one horizontal plane."""
    triangles = vertices[faces]
    points: list[np.ndarray] = []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        va, vb = triangles[:, a], triangles[:, b]
        denominator = vb[:, 2] - va[:, 2]
        crossing = (np.abs(denominator) > 1.0e-12) & ((va[:, 2] - z) * (vb[:, 2] - z) <= 0.0)
        t = (z - va[crossing, 2]) / denominator[crossing]
        points.append(va[crossing, :2] + t[:, None] * (vb[crossing, :2] - va[crossing, :2]))
    return np.unique(np.round(np.concatenate(points), 10), axis=0)


def _nearest_uvs(source_vertices: np.ndarray, source_uvs: np.ndarray, vertices: np.ndarray) -> np.ndarray:
    """Map UVs without pulling scipy into the simulator toolchain."""
    nearest = np.empty(len(vertices), dtype=np.int32)
    for start in range(0, len(vertices), 256):
        chunk = vertices[start : start + 256]
        distances = np.sum((chunk[:, None, :] - source_vertices[None, :, :]) ** 2, axis=2)
        nearest[start : start + len(chunk)] = np.argmin(distances, axis=1)
    return np.asarray(source_uvs[nearest], dtype=np.float64)


def _sewn_panels(
    source_vertices: np.ndarray, source_faces: np.ndarray, rows: int = 17, columns: int = 5
) -> tuple[np.ndarray, np.ndarray]:
    """Two thin fabric panels sharing a toe/side seam, with an unsewn cuff.

    The source supplies the X/Z tailoring outline, NOT a puffy rest metric.
    Inextensible triangles on the old inflated cage lock its non-developable
    curvature. These nearly planar panels can instead fold without stretching.
    Shared seam vertices cannot separate; there are no hidden cross-cuff bars,
    interior solids, doubled render shells, or welds to the gripper.
    """
    if rows < 3 or columns < 3:
        raise ValueError("sewn panels need at least three rows and columns")
    low, high = source_vertices[:, 2].min(), source_vertices[:, 2].max()
    inset = min(0.002, 0.02 * (high - low))
    vertices: list[list[float]] = []
    ids: dict[tuple[int, int, int], int] = {}
    for row, z in enumerate(np.linspace(low + inset, high - inset, rows)):
        section = _cross_section_points(source_vertices, source_faces, float(z))
        left, right = section[:, 0].min(), section[:, 0].max()
        for side in range(2):
            for col in range(columns):
                seam = col in (0, columns - 1) or row == 0
                if side == 1 and seam:
                    ids[side, row, col] = ids[0, row, col]
                    continue
                ids[side, row, col] = len(vertices)
                u = col / (columns - 1)
                # A small opening keeps opposing faces initially separated.
                # This is empty space between panels, not cloth thickness.
                y = 0.0 if seam else (1 if side else -1) * 0.002 * np.sin(np.pi * u)
                vertices.append([float(left + (right - left) * u), float(y), float(z)])
    faces = []
    for side in range(2):
        for row in range(rows - 1):
            for col in range(columns - 1):
                a, b, c, d = [
                    ids[side, r, k] for r, k in ((row, col), (row, col + 1), (row + 1, col + 1), (row + 1, col))
                ]
                # At the sewn toe's right corner, the other diagonal creates
                # an all-seam triangle duplicated on both panels when columns
                # is even. Keep the diagonal incident to the interior vertex.
                diagonal_ac = (row + col) % 2 == 0 and not (row == 0 and col == columns - 2)
                triangles = [(a, b, c), (a, c, d)] if diagonal_ac else [(a, b, d), (b, c, d)]
                faces.extend(triangles if side == 0 else [t[::-1] for t in triangles])
    return np.asarray(vertices), np.asarray(faces, dtype=np.int32)


def _export_surface_skin(path: Path, vertices: np.ndarray) -> None:
    """Identity ISK2: visible fabric is exactly the colliding cloth surface.

    No signed MLS weights spanning opposite panels, or rigid normal offsets
    that can stick out through a fingertip when the cloth folds tightly.
    """
    count = len(vertices)
    header = b"ISK2" + np.asarray((count, count, 0), dtype="<u4").tobytes()
    rest = np.asarray(vertices, dtype="<f4").tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + rest + rest + np.eye(count, dtype="<f4").tobytes())


def _position_uvs(vertices: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Map model X/Z directly to texture U/V for clean semantic regions."""
    low = reference.min(axis=0)
    span = np.maximum(reference.max(axis=0) - low, 1.0e-12)
    normalized = (vertices - low) / span
    return np.column_stack((normalized[:, 0], normalized[:, 2]))


def _two_tone_gray_texture(size: int) -> Image.Image:
    """Create a clean light body with dark cuff and medium heel/toe."""
    body = (184, 187, 190)
    cuff = (58, 61, 65)
    heel_toe = (99, 103, 108)
    pixels = np.empty((size, size, 3), dtype=np.uint8)
    pixels[:] = body
    u = np.linspace(0.0, 1.0, size)[None, :]
    v = np.linspace(1.0, 0.0, size)[:, None]
    heel_or_toe = ((u <= 0.28) & (v <= 0.35)) | ((u >= 0.70) & (v <= 0.55))
    pixels[heel_or_toe] = heel_toe
    pixels[np.broadcast_to(v >= 0.78, (size, size))] = cuff
    return Image.fromarray(pixels)


def _export_glb(
    path: Path,
    vertices: np.ndarray,
    faces: np.ndarray,
    uvs: np.ndarray,
    texture_path: Path,
    texture_size: int,
) -> None:
    with Image.open(texture_path) as source:
        texture = source.convert("RGB")
        if max(texture.size) > texture_size:
            texture.thumbnail((texture_size, texture_size), Image.Resampling.LANCZOS)
        texture.load()
    material = PBRMaterial(
        name="sock_material",
        baseColorTexture=texture,
        baseColorFactor=(255, 255, 255, 255),
        metallicFactor=0.0,
        roughnessFactor=0.9,
        doubleSided=True,
    )
    mesh = trimesh.Trimesh(
        vertices=np.asarray(vertices, dtype=np.float32),
        faces=np.asarray(faces, dtype=np.int32),
        visual=TextureVisuals(uv=np.asarray(uvs, dtype=np.float32), material=material),
        process=False,
        maintain_order=True,
    )
    scene = trimesh.Scene(base_frame="world")
    scene.add_geometry(mesh, node_name="soft_sock", geom_name="soft_sock", transform=np.eye(4))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(scene.export(file_type="glb"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-data", type=Path)
    source.add_argument("--source-glb", type=Path)
    parser.add_argument("--source-texture", type=Path)
    parser.add_argument("--physics-dir", type=Path, required=True)
    parser.add_argument("--viewer-glb", type=Path, required=True)
    parser.add_argument("--viewer-skin", type=Path, required=True)
    parser.add_argument("--texture-size", type=int, default=1024)
    parser.add_argument("--palette", choices=("authored", "two-tone-gray"), default="two-tone-gray")
    parser.add_argument("--panel-rows", type=int, default=17)
    parser.add_argument("--panel-columns", type=int, default=5)
    args = parser.parse_args()

    if args.source_glb:
        mesh = trimesh.load(args.source_glb.resolve(), force="mesh", process=False)
        source_vertices, source_faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
        source_uvs = np.asarray(mesh.visual.uv)
    else:
        with np.load(args.source_data.resolve()) as stored:
            source_vertices = np.asarray(stored["vertices"], dtype=np.float64)
            source_faces = np.asarray(stored["faces"], dtype=np.int32)
            source_uvs = np.asarray(stored["uvs"], dtype=np.float64)
    if args.palette == "authored" and not args.source_texture:
        parser.error("--palette authored requires --source-texture")
    if args.palette == "two-tone-gray":
        source_uvs = _position_uvs(source_vertices, source_vertices)

    source_vertex_count, source_face_count = len(source_vertices), len(source_faces)
    vertices, faces = _sewn_panels(source_vertices, source_faces, args.panel_rows, args.panel_columns)
    anchor = np.array((vertices[:, 0].mean(), vertices[:, 1].mean(), vertices[:, 2].min()))
    vertices -= anchor
    source_vertices = source_vertices - anchor
    initial_vertices = vertices.copy()
    # Stress-free planar rest metric; placement opens the two panels.
    vertices[:, 1] = 0.0
    if args.palette == "two-tone-gray":
        uvs = _position_uvs(vertices, source_vertices)
    else:
        uvs = _nearest_uvs(source_vertices, source_uvs, vertices)
    source_vertices, source_faces, source_uvs = initial_vertices.copy(), faces.copy(), uvs.copy()
    hinges, rest_angles, rest_lengths = _build_hinges(vertices, faces)
    boundary_edges, nonmanifold_edges = _topology_counts(faces)
    if not boundary_edges or nonmanifold_edges:
        raise RuntimeError(f"control cage has {boundary_edges} boundary and {nonmanifold_edges} non-manifold edges")

    physics_dir = args.physics_dir.resolve()
    physics_dir.mkdir(parents=True, exist_ok=True)
    if args.palette == "two-tone-gray":
        physics_texture = _two_tone_gray_texture(args.texture_size)
    else:
        with Image.open(args.source_texture.resolve()) as source:
            physics_texture = source.convert("RGB")
            if max(physics_texture.size) > args.texture_size:
                physics_texture.thumbnail((args.texture_size, args.texture_size), Image.Resampling.LANCZOS)
            physics_texture.load()
    output_texture = physics_dir / "texture_base_color.png"
    physics_texture.save(output_texture, optimize=True)
    np.savez_compressed(
        physics_dir / "cloth_data.npz",
        vertices=vertices,
        initial_vertices=initial_vertices,
        faces=faces,
        uvs=uvs,
        hinges=hinges,
        rest_angles=rest_angles,
        rest_lengths=rest_lengths,
        render_vertex_count=np.asarray(len(source_vertices), dtype=np.int32),
    )
    _export_glb(
        args.viewer_glb.resolve(),
        source_vertices,
        source_faces,
        source_uvs,
        output_texture,
        args.texture_size,
    )
    _export_surface_skin(args.viewer_skin.resolve(), vertices)
    diagnostics = {
        "source_vertices": source_vertex_count,
        "source_triangles": source_face_count,
        "control_vertices": len(vertices),
        "control_triangles": len(faces),
        "control_strategy": "sewn-panels",
        "panel_rows": args.panel_rows,
        "panel_columns": args.panel_columns,
        "bending_hinges": len(hinges),
        "boundary_edges": boundary_edges,
        "nonmanifold_edges": nonmanifold_edges,
        "bounds_m": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()],
        "render_vertices": len(source_vertices),
        "render_triangles": len(source_faces),
        "viewer_glb_bytes": args.viewer_glb.resolve().stat().st_size,
        "viewer_skin_bytes": args.viewer_skin.resolve().stat().st_size,
        "physics_texture_bytes": (physics_dir / "texture_base_color.png").stat().st_size,
    }
    (physics_dir / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2) + "\n")
    print(json.dumps(diagnostics, indent=2))


if __name__ == "__main__":
    main()
