import numpy as np
import trimesh
from trimesh.ray import ray_triangle
import genesis as gs


def quaternion_to_matrix(q):
    """
    Convert a quaternion to a 3x3 rotation matrix.
    q is assumed to be a PyTorch CPU tensor or a similar 4-element object: (w, x, y, z).
    """
    w, x, y, z = q.cpu().numpy()
    ww, xx_, yy, zz = w * w, x * x, y * y, z * z
    R = np.array(
        [
            [ww + xx_ - yy - zz, 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), ww - xx_ + yy - zz, 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), ww - xx_ - yy + zz],
        ]
    )
    return R


def rotate_vector(vec, quat):
    """
    Rotate a 3D vector `vec` by the rotation described by quaternion `quat`.
    """
    R = quaternion_to_matrix(quat)
    return R.dot(vec)


def transform_from_pos_quat(pos, quat):
    """
    Construct a 4x4 homogeneous transform from a position and quaternion.

    :param pos: np.ndarray of shape (3,) for translation (x, y, z).
    :param quat: np.ndarray of shape (4,) for quaternion (w, x, y, z).
                 (Assumed to be in wxyz order!)
    :return: np.ndarray of shape (4,4), the homogeneous transform matrix.
    """
    # Unpack
    w, x, y, z = quat
    # Normalize just to be safe
    norm = np.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / norm, x / norm, y / norm, z / norm

    # Rotation part: standard quaternion-to-rotation formula
    R = np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )

    # Build the full transform
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = R
    T[:3, 3] = pos
    return T


def create_occupancy_grid_from_scene(
    scene,
    x_range=(-2.0, 2.0),
    y_range=(-2.0, 2.0),
    resolution=0.1,
    slice_z=0.05,
    ray_offset=0.05,
):
    """
    Build a 2D occupancy grid by casting short vertical rays
    at `slice_z`, using *only* trimesh.ray.ray_triangle (no pyembree).
    """
    all_meshes = []

    for ent in scene.entities:
        morph = ent.morph
        if morph is None:
            continue

        try:
            # Build a genesis.Mesh or list of them
            tmp_surface = gs.surfaces.Plastic(smooth=False)
            gmesh = gs.Mesh.from_morph_surface(morph, tmp_surface)
            # Might be a single Mesh or a list of submeshes
            if not isinstance(gmesh, list):
                gmesh = [gmesh]

            # World transform from entity
            pos = ent.get_pos().cpu().numpy()  # (3,)
            quat = ent.get_quat().cpu().numpy()  # (4,) wxyz
            T_world = transform_from_pos_quat(pos, quat)

            # For each submesh, apply transform and collect its .trimesh
            for submesh in gmesh:
                submesh.apply_transform(T_world)
                all_meshes.append(submesh.trimesh.copy())

        except Exception as e:
            print(f"Skipping entity {ent} for occupancy; error: {e}")
            continue

    if len(all_meshes) == 0:
        print(
            "[create_occupancy_grid] No meshable geometry found; returning empty grid."
        )
        return np.zeros((1, 1), dtype=np.uint8)

    # Merge into one big Trimesh
    big_mesh = trimesh.util.concatenate(all_meshes)

    # Set up ray intersection with *no* pyembree
    intersector = ray_triangle.RayMeshIntersector(big_mesh)

    # XY grid
    x_min, x_max = x_range
    y_min, y_max = y_range
    xs = np.arange(x_min, x_max, resolution)
    ys = np.arange(y_min, y_max, resolution)
    grid = np.zeros((len(ys), len(xs)), dtype=np.uint8)

    # Cast short rays downward from slice_z + ray_offset
    direction = np.array([0.0, 0.0, -1.0])
    for row_idx, y_val in enumerate(ys):
        for col_idx, x_val in enumerate(xs):
            origin = np.array([x_val, y_val, slice_z + ray_offset]).reshape(1, 3)
            dirs = np.tile(direction, (1, 1))
            # returns -1 if no intersection
            tri_idx = intersector.intersects_first(origins=origin, directions=dirs)
            if tri_idx[0] != -1:
                grid[row_idx, col_idx] = 1

    return grid
