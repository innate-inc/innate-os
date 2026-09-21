import numpy as np
import pytest
import trimesh
from asset_tools.build_soft_sock_asset import (
    _export_surface_skin,
    _sewn_panels,
    _topology_counts,
)


def test_cli_exports_matching_sewn_physics_and_viewer_assets(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    source = trimesh.creation.box(extents=(0.08, 0.05, 0.20))
    src = tmp_path / "source.npz"
    np.savez(src, vertices=source.vertices, faces=source.faces, uvs=np.zeros((8, 2)))
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parent / "asset_tools/build_soft_sock_asset.py"),
            "--source-data",
            str(src),
            "--physics-dir",
            str(tmp_path / "physics"),
            "--viewer-glb",
            str(tmp_path / "sock.glb"),
            "--viewer-skin",
            str(tmp_path / "skin.bin"),
        ],
        check=True,
        capture_output=True,
    )
    with np.load(tmp_path / "physics/cloth_data.npz") as data:
        assert data["vertices"].shape == (133, 3)
        assert data["faces"].shape == (256, 3)
        assert np.all(data["vertices"][:, 1] == 0)
        assert np.ptp(data["initial_vertices"][:, 1]) > 0
    skin = (tmp_path / "skin.bin").read_bytes()
    assert skin[:4] == b"ISK2"
    np.testing.assert_array_equal(np.frombuffer(skin, "<u4", count=3, offset=4), [133, 133, 0])
    assert (tmp_path / "physics/texture_base_color.png").is_file()
    assert len(trimesh.load(tmp_path / "sock.glb", force="mesh", process=False).vertices) == 133


def test_sewn_panels_have_one_open_cuff_and_closed_seams():
    source = trimesh.creation.box(extents=(0.08, 0.05, 0.20))
    vertices, faces = _sewn_panels(source.vertices, source.faces)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    assert len(vertices) == 133
    assert mesh.euler_number == 1  # connected surface with exactly one boundary
    assert mesh.is_winding_consistent
    assert np.min(mesh.area_faces) > 1e-8
    boundary_count, nonmanifold = _topology_counts(faces)
    assert boundary_count == 8 and nonmanifold == 0
    edges, counts = np.unique(np.sort(mesh.edges, axis=1), axis=0, return_counts=True)
    boundary = edges[counts == 1]
    np.testing.assert_allclose(vertices[boundary, 2], vertices[:, 2].max())
    assert np.ptp(vertices[:, 1]) == pytest.approx(0.004)
    reached = {0}
    while True:
        expanded = reached | {int(b) for a, b in mesh.edges if int(a) in reached}
        if expanded == reached:
            break
        reached = expanded
    assert len(reached) == len(vertices)


def test_panel_resolution_rejects_degenerate_patterns():
    source = trimesh.creation.box()
    for rows, columns in ((1, 5), (5, 2)):
        with pytest.raises(ValueError, match="at least three"):
            _sewn_panels(source.vertices, source.faces, rows, columns)


def test_surface_skin_matches_collision_even_under_non_affine_folds(tmp_path):
    vertices = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 1.0, 0.0]])
    path = tmp_path / "skin.bin"
    _export_surface_skin(path, vertices)
    payload = path.read_bytes()
    assert payload[:4] == b"ISK2"
    np.testing.assert_array_equal(np.frombuffer(payload, "<u4", count=3, offset=4), [4, 4, 0])
    rest = np.frombuffer(payload, "<f4", count=12, offset=16).reshape(4, 3)
    weights = np.frombuffer(payload, "<f4", offset=16 + 2 * 12 * 4).reshape(4, 4)
    folded = np.array([[0.1, 0.2, 0.3], [1.0, 0.0, 0.0], [0.3, -0.8, 0.1], [-0.2, 0.1, 0.7]])
    np.testing.assert_allclose(rest + weights @ (folded - rest), folded)
    # A fold on the back panel must not pull a front-panel point through a jaw.
    np.testing.assert_array_equal(weights, np.eye(4))


@pytest.mark.parametrize("rows,columns", [(17, 5), (21, 5), (21, 6), (21, 7)])
def test_sewn_density_variants_keep_one_open_manifold_cuff(rows, columns):
    source = trimesh.creation.box(extents=(0.08, 0.05, 0.20))
    vertices, faces = _sewn_panels(source.vertices, source.faces, rows, columns)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    assert mesh.euler_number == 1 and mesh.is_winding_consistent
    assert _topology_counts(faces) == (2 * (columns - 1), 0)
    assert len(np.unique(np.sort(faces, axis=1), axis=0)) == len(faces)
