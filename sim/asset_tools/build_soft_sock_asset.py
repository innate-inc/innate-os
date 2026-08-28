#!/usr/bin/env python3
"""Build a real-time MuJoCo/Three.js sock from the authored cloth bundle.

The source bundle keeps a high-resolution control surface.  That surface is
useful for offline renders but too expensive for the simulator's wall-clock
physics loop. Directly decimating its close inner/outer layers makes them
intersect at tiny resolutions, so this tool builds a regularly sampled
cross-section cage with an open cuff and emits the two artifacts the runtime
consumes:

* ``cloth_data.npz`` for MuJoCo flex topology and rest-dihedral bending.
* a textured, double-sided GLB plus a compact skin map for Three.js.

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


def _convex_hull_2d(points: np.ndarray) -> np.ndarray:
    """Andrew monotone-chain hull, returned counter-clockwise."""
    ordered = sorted(map(tuple, points.tolist()))
    if len(ordered) < 3:
        raise RuntimeError("sock cross-section has fewer than three points")

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    return np.asarray(lower[:-1] + upper[:-1], dtype=np.float64)


def _sample_hull_radially(hull: np.ndarray, segments: int) -> np.ndarray:
    """Sample a convex section at consistent angles around an interior point."""
    center = hull.mean(axis=0)
    sampled = []
    for angle in np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False):
        direction = np.asarray((np.cos(angle), np.sin(angle)))
        hits = []
        for start, end in zip(hull, np.roll(hull, -1, axis=0), strict=True):
            edge = end - start
            denominator = direction[0] * edge[1] - direction[1] * edge[0]
            if abs(denominator) < 1.0e-12:
                continue
            relative = start - center
            distance = (relative[0] * edge[1] - relative[1] * edge[0]) / denominator
            fraction = (relative[0] * direction[1] - relative[1] * direction[0]) / denominator
            if distance >= 0.0 and -1.0e-9 <= fraction <= 1.0 + 1.0e-9:
                hits.append(distance)
        if not hits:
            raise RuntimeError("failed to intersect a radial ray with the sock cross-section")
        sampled.append(center + min(hits) * direction)
    return np.asarray(sampled)


def _regular_control_cage(
    source_vertices: np.ndarray, source_faces: np.ndarray, rings: int, segments: int
) -> tuple[np.ndarray, np.ndarray]:
    """Build an evenly sampled, open-cuff surface instead of a lumpy decimation.

    The former 42-point convex hull put nearly every vertex at the sole or
    cuff, leaving the middle of the sock as a few huge rigid triangles.  This
    swept cage gives every longitudinal region the same deformation budget.
    """
    z_min = float(source_vertices[:, 2].min())
    z_max = float(source_vertices[:, 2].max())
    inset = min(0.002, 0.02 * (z_max - z_min))
    levels = np.linspace(z_min + inset, z_max - inset, rings)
    ring_vertices = []
    for z in levels:
        section = _cross_section_points(source_vertices, source_faces, float(z))
        sampled = _sample_hull_radially(_convex_hull_2d(section), segments)
        ring_vertices.append(np.column_stack((sampled, np.full(segments, z))))

    vertices = np.concatenate(ring_vertices, axis=0)
    bottom_center = np.asarray((vertices[:segments, 0].mean(), vertices[:segments, 1].mean(), z_min))
    vertices = np.vstack((vertices, bottom_center))
    bottom = len(vertices) - 1
    faces: list[tuple[int, int, int]] = []
    for ring in range(rings - 1):
        lower = ring * segments
        upper = (ring + 1) * segments
        for segment in range(segments):
            following = (segment + 1) % segments
            a, b = lower + segment, lower + following
            c, d = upper + following, upper + segment
            if (ring + segment) % 2:
                faces.extend(((a, b, d), (b, c, d)))
            else:
                faces.extend(((a, b, c), (a, c, d)))
    for segment in range(segments):
        following = (segment + 1) % segments
        faces.append((bottom, following, segment))
    return vertices, np.asarray(faces, dtype=np.int32)


def _nearest_uvs(source_vertices: np.ndarray, source_uvs: np.ndarray, vertices: np.ndarray) -> np.ndarray:
    """Map UVs without pulling scipy into the simulator toolchain."""
    nearest = np.empty(len(vertices), dtype=np.int32)
    for start in range(0, len(vertices), 256):
        chunk = vertices[start : start + 256]
        distances = np.sum((chunk[:, None, :] - source_vertices[None, :, :]) ** 2, axis=2)
        nearest[start : start + len(chunk)] = np.argmin(distances, axis=1)
    return np.asarray(source_uvs[nearest], dtype=np.float64)


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


def _export_skin(
    path: Path,
    render_vertices: np.ndarray,
    control_vertices: np.ndarray,
    control_faces: np.ndarray,
) -> tuple[float, float]:
    """Write a smooth radial-basis deformation map (ISK2).

    Per-triangle nearest bindings are discontinuous: neighboring render
    vertices can follow unrelated cage faces and tear apart as those faces
    fold.  A polyharmonic RBF map is continuous over the whole sock and its
    affine tail reproduces translations, rotations, and rest geometry.
    """
    del control_faces  # topology drives physics; smooth skinning uses its vertices
    scale = float(np.max(np.ptp(control_vertices, axis=0)))
    controls = control_vertices / scale
    renders = render_vertices / scale
    distances = np.linalg.norm(controls[:, None, :] - controls[None, :, :], axis=2)
    kernel = distances**3
    affine = np.column_stack((np.ones(len(controls)), controls))
    system = np.block(
        [
            [kernel + np.eye(len(controls)) * 1.0e-10, affine],
            [affine.T, np.zeros((4, 4))],
        ]
    )
    rhs = np.vstack((np.eye(len(controls)), np.zeros((4, len(controls)))))
    coefficients = np.linalg.solve(system, rhs)
    evaluation = np.column_stack(
        (
            np.linalg.norm(renders[:, None, :] - controls[None, :, :], axis=2) ** 3,
            np.ones(len(renders)),
            renders,
        )
    )
    weights = evaluation @ coefficients
    reconstructed = weights @ control_vertices
    rest_error = float(np.max(np.linalg.norm(reconstructed - render_vertices, axis=1)))
    partition_error = float(np.max(np.abs(weights.sum(axis=1) - 1.0)))

    header = b"ISK2" + np.asarray((len(render_vertices), len(control_vertices), 0), dtype="<u4").tobytes()
    payload = (
        header
        + np.asarray(control_vertices, dtype="<f4").tobytes()
        + np.asarray(render_vertices, dtype="<f4").tobytes()
        + np.asarray(weights, dtype="<f4").tobytes()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return rest_error, partition_error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, required=True)
    parser.add_argument("--source-texture", type=Path, required=True)
    parser.add_argument("--physics-dir", type=Path, required=True)
    parser.add_argument("--viewer-glb", type=Path, required=True)
    parser.add_argument("--viewer-skin", type=Path, required=True)
    parser.add_argument("--control-rings", type=int, default=9)
    parser.add_argument("--control-segments", type=int, default=12)
    parser.add_argument("--texture-size", type=int, default=1024)
    parser.add_argument("--palette", choices=("authored", "two-tone-gray"), default="two-tone-gray")
    args = parser.parse_args()

    with np.load(args.source_data.resolve()) as stored:
        source_vertices = np.asarray(stored["vertices"], dtype=np.float64)
        source_faces = np.asarray(stored["faces"], dtype=np.int32)
        source_uvs = np.asarray(stored["uvs"], dtype=np.float64)
    if args.palette == "two-tone-gray":
        source_uvs = _position_uvs(source_vertices, source_vertices)

    vertices, faces = _regular_control_cage(source_vertices, source_faces, args.control_rings, args.control_segments)
    anchor = np.array((vertices[:, 0].mean(), vertices[:, 1].mean(), vertices[:, 2].min()))
    vertices -= anchor
    source_vertices = source_vertices - anchor
    if args.palette == "two-tone-gray":
        uvs = _position_uvs(vertices, source_vertices)
    else:
        uvs = _nearest_uvs(source_vertices, source_uvs, vertices)
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
        output_texture,
        args.texture_size,
    )
    skin_error, skin_partition_error = _export_skin(args.viewer_skin.resolve(), source_vertices, vertices, faces)
    diagnostics = {
        "source_vertices": len(source_vertices),
        "source_triangles": len(source_faces),
        "control_vertices": len(vertices),
        "control_triangles": len(faces),
        "control_strategy": "regular_cross_section_cage",
        "control_rings": args.control_rings,
        "control_segments": args.control_segments,
        "bending_hinges": len(hinges),
        "boundary_edges": boundary_edges,
        "nonmanifold_edges": nonmanifold_edges,
        "bounds_m": [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()],
        "render_vertices": len(source_vertices),
        "render_triangles": len(source_faces),
        "skin_rest_max_error_m": skin_error,
        "skin_partition_max_error": skin_partition_error,
        "viewer_glb_bytes": args.viewer_glb.resolve().stat().st_size,
        "viewer_skin_bytes": args.viewer_skin.resolve().stat().st_size,
        "physics_texture_bytes": (physics_dir / "texture_base_color.png").stat().st_size,
    }
    (physics_dir / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2) + "\n")
    print(json.dumps(diagnostics, indent=2))


if __name__ == "__main__":
    main()
