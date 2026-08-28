#!/usr/bin/env python3
"""Build a real-time MuJoCo/Three.js sock from the authored cloth bundle.

The source bundle keeps a high-resolution control surface.  That surface is
useful for offline renders but too expensive for the simulator's wall-clock
physics loop. Directly decimating its close inner/outer layers makes them
intersect at tiny resolutions, so this tool builds a low-resolution convex
cage, opens it at the cuff for cloth-like bending, and emits the two artifacts
the runtime consumes:

* ``cloth_data.npz`` for MuJoCo flex topology and rest-dihedral bending.
* a textured, double-sided GLB plus a compact skin map for Three.js.

Both outputs use metres, Z-up, an identity object transform, and a local frame
whose XY centre and lowest Z point are at the origin.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import coacd
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


def _convex_control_cage(
    source_vertices: np.ndarray, source_faces: np.ndarray, target_vertices: int
) -> tuple[np.ndarray, np.ndarray, float]:
    """Build one deterministic convex cage and verify every face plane.

    A convex cage is intentional here: QEM simplification of the authored
    sock's close inner/outer layers produced penetrating rest triangles, so a
    self-collision solve injected energy before the first physics step.  The
    high-resolution outline is still preserved by the separate render skin.
    """
    coacd.set_log_level("off")
    parts = coacd.run_coacd(
        coacd.Mesh(source_vertices, source_faces),
        threshold=1.0,
        max_convex_hull=1,
        preprocess_mode="off",
        resolution=200,
        mcts_nodes=4,
        mcts_iterations=20,
        mcts_max_depth=1,
        merge=True,
        decimate=True,
        max_ch_vertex=target_vertices,
        seed=0,
    )
    if len(parts) != 1:
        raise RuntimeError(f"expected one convex control cage, got {len(parts)}")
    vertices = np.asarray(parts[0][0], dtype=np.float64)
    faces = np.asarray(parts[0][1], dtype=np.int32)
    if len(vertices) > target_vertices:
        raise RuntimeError(f"control cage has {len(vertices)} vertices, target was {target_vertices}")

    triangles = vertices[faces]
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    centre = vertices.mean(axis=0)
    orientation = np.einsum("ij,ij->i", normals, triangles.mean(axis=1) - centre)
    if np.median(orientation) < 0.0:
        faces = faces[:, (0, 2, 1)]
        triangles = vertices[faces]
        normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        orientation = np.einsum("ij,ij->i", normals, triangles.mean(axis=1) - centre)
    if np.any(orientation <= 0.0):
        raise RuntimeError("convex control cage has inconsistent face winding")
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1.0e-12)
    plane_distance = np.einsum("fvi,fi->fv", vertices[None, :, :] - triangles[:, None, 0, :], normals)
    max_violation = float(max(0.0, np.max(plane_distance)))
    if max_violation > 1.0e-8:
        raise RuntimeError(f"control cage is not convex; plane violation {max_violation:g}m")
    return vertices, faces, max_violation


def _open_cuff(vertices: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """Remove the +Z cap so the control surface bends like an open sock.

    The render mesh remains unchanged. Removing a connected cap (rather than
    one triangle) also removes its internal edge constraints; a fully closed
    triangulated convex shell would otherwise be mechanically rigid even
    with zero bending stiffness.
    """
    triangles = vertices[faces]
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1.0e-12)
    z_max = float(vertices[:, 2].max())
    z_span = z_max - float(vertices[:, 2].min())
    remove = (normals[:, 2] > 0.9) & (triangles.mean(axis=1)[:, 2] > z_max - 0.1 * z_span)
    removed = int(np.count_nonzero(remove))
    if removed < 2:
        raise RuntimeError(f"control cage cuff selection removed only {removed} faces")
    faces = faces[~remove]
    used = np.unique(faces)
    remap = np.full(len(vertices), -1, dtype=np.int32)
    remap[used] = np.arange(len(used), dtype=np.int32)
    return vertices[used], remap[faces], removed


def _nearest_uvs(source_vertices: np.ndarray, source_uvs: np.ndarray, vertices: np.ndarray) -> np.ndarray:
    """Map UVs without pulling scipy into the simulator toolchain."""
    nearest = np.empty(len(vertices), dtype=np.int32)
    for start in range(0, len(vertices), 256):
        chunk = vertices[start : start + 256]
        distances = np.sum((chunk[:, None, :] - source_vertices[None, :, :]) ** 2, axis=2)
        nearest[start : start + len(chunk)] = np.argmin(distances, axis=1)
    return np.asarray(source_uvs[nearest], dtype=np.float64)


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


def _export_skin(
    path: Path,
    render_vertices: np.ndarray,
    control_vertices: np.ndarray,
    control_faces: np.ndarray,
) -> float:
    """Write render-vertex bindings to moving control triangles.

    ISK1 layout is deliberately browser-native: a 16-byte little-endian
    header, uint16 control indices, alignment padding, float32 barycentric
    weights, then float32 offsets in each rest triangle's local frame.
    """
    control_mesh = trimesh.Trimesh(vertices=control_vertices, faces=control_faces, process=False, maintain_order=True)
    closest, _distance, triangle_ids = trimesh.proximity.closest_point_naive(control_mesh, render_vertices)
    triangles = control_vertices[control_faces[triangle_ids]]
    weights = trimesh.triangles.points_to_barycentric(triangles, closest)
    indices = np.asarray(control_faces[triangle_ids], dtype="<u2")

    edge1 = triangles[:, 1] - triangles[:, 0]
    tangent1 = edge1 / np.maximum(np.linalg.norm(edge1, axis=1, keepdims=True), 1.0e-12)
    normal = np.cross(edge1, triangles[:, 2] - triangles[:, 0])
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1.0e-12)
    tangent2 = np.cross(normal, tangent1)
    residual = render_vertices - closest
    offsets = np.column_stack(
        (
            np.einsum("ij,ij->i", residual, tangent1),
            np.einsum("ij,ij->i", residual, tangent2),
            np.einsum("ij,ij->i", residual, normal),
        )
    ).astype("<f4")

    reconstructed = (
        np.einsum("ni,nij->nj", weights, triangles)
        + offsets[:, 0, None] * tangent1
        + offsets[:, 1, None] * tangent2
        + offsets[:, 2, None] * normal
    )
    error = float(np.max(np.linalg.norm(reconstructed - render_vertices, axis=1)))

    header = b"ISK1" + np.asarray((len(render_vertices), len(control_vertices), 0), dtype="<u4").tobytes()
    index_bytes = indices.tobytes()
    padding = b"\0" * ((-len(index_bytes)) % 4)
    payload = header + index_bytes + padding + np.asarray(weights, dtype="<f4").tobytes() + offsets.tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, required=True)
    parser.add_argument("--source-texture", type=Path, required=True)
    parser.add_argument("--physics-dir", type=Path, required=True)
    parser.add_argument("--viewer-glb", type=Path, required=True)
    parser.add_argument("--viewer-skin", type=Path, required=True)
    parser.add_argument("--target-control-vertices", type=int, default=42)
    parser.add_argument("--texture-size", type=int, default=1024)
    args = parser.parse_args()

    with np.load(args.source_data.resolve()) as stored:
        source_vertices = np.asarray(stored["vertices"], dtype=np.float64)
        source_faces = np.asarray(stored["faces"], dtype=np.int32)
        source_uvs = np.asarray(stored["uvs"], dtype=np.float64)

    vertices, faces, max_convex_plane_violation = _convex_control_cage(
        source_vertices, source_faces, args.target_control_vertices
    )
    vertices, faces, removed_cuff_faces = _open_cuff(vertices, faces)
    anchor = np.array((vertices[:, 0].mean(), vertices[:, 1].mean(), vertices[:, 2].min()))
    vertices -= anchor
    source_vertices = source_vertices - anchor
    uvs = _nearest_uvs(source_vertices, source_uvs, vertices)
    hinges, rest_angles, rest_lengths = _build_hinges(vertices, faces)
    boundary_edges, nonmanifold_edges = _topology_counts(faces)
    if not boundary_edges or nonmanifold_edges:
        raise RuntimeError(f"control cage has {boundary_edges} boundary and {nonmanifold_edges} non-manifold edges")

    physics_dir = args.physics_dir.resolve()
    physics_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(args.source_texture.resolve()) as source:
        physics_texture = source.convert("RGB")
        if max(physics_texture.size) > args.texture_size:
            physics_texture.thumbnail((args.texture_size, args.texture_size), Image.Resampling.LANCZOS)
        physics_texture.save(physics_dir / "texture_base_color.png", optimize=True)
    np.savez_compressed(
        physics_dir / "cloth_data.npz",
        vertices=vertices,
        faces=faces,
        uvs=uvs,
        hinges=hinges,
        rest_angles=rest_angles,
        rest_lengths=rest_lengths,
        render_vertex_count=np.asarray(len(source_vertices), dtype=np.int32),
    )
    # Render the original 5k-triangle control surface, skinned to the tiny
    # physics mesh. This keeps the authored colour/outline without sending or
    # simulating the offline mesh's 2,496 control vertices.
    _export_glb(
        args.viewer_glb.resolve(),
        source_vertices,
        source_faces,
        source_uvs,
        args.source_texture.resolve(),
        args.texture_size,
    )
    skin_error = _export_skin(args.viewer_skin.resolve(), source_vertices, vertices, faces)
    diagnostics = {
        "source_vertices": len(source_vertices),
        "source_triangles": len(source_faces),
        "control_vertices": len(vertices),
        "control_triangles": len(faces),
        "control_strategy": "open_cuff_convex_cage",
        "removed_cuff_faces": removed_cuff_faces,
        "max_convex_plane_violation_m": max_convex_plane_violation,
        "bending_hinges": len(hinges),
        "boundary_edges": boundary_edges,
        "nonmanifold_edges": nonmanifold_edges,
        "bounds_m": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()],
        "render_vertices": len(source_vertices),
        "render_triangles": len(source_faces),
        "skin_rest_max_error_m": skin_error,
        "viewer_glb_bytes": args.viewer_glb.resolve().stat().st_size,
        "viewer_skin_bytes": args.viewer_skin.resolve().stat().st_size,
        "physics_texture_bytes": (physics_dir / "texture_base_color.png").stat().st_size,
    }
    (physics_dir / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2) + "\n")
    print(json.dumps(diagnostics, indent=2))


if __name__ == "__main__":
    main()
