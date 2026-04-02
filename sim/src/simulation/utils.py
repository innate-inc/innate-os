import numpy as np
import trimesh
from trimesh.ray import ray_triangle
import genesis as gs


def quaternion_to_matrix(q):
    """
    Convert a quaternion to a 3x3 rotation matrix.
    q can be either a PyTorch tensor or numpy array/numeric type: (w, x, y, z).
    """
    # Handle PyTorch tensor input
    if hasattr(q, "cpu"):
        w, x, y, z = q.cpu().numpy()
    # Handle numpy array or numeric input
    else:
        w, x, y, z = q

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
