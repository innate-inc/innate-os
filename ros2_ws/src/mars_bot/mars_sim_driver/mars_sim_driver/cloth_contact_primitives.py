"""Optional experimental cloth contact primitives; no IPC dependency."""

import numpy as np
from numba import njit
from scipy.spatial import ConvexHull


def edges_from_faces(faces):
    return np.unique(np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1), axis=0)


@njit(cache=True)
def project_kernel(
    cloth,
    delta,
    ids,
    weights,
    pushes,
    jac,
    rigid_push,
    normals,
    tangent_inverse,
    denominators,
    mobility,
    rest,
    fixed,
    thickness,
    friction,
    iterations,
):
    count = len(ids)
    lambdas = np.zeros(count)
    tangents = np.zeros((count, 3))
    separation = np.empty(3)
    impulse = np.empty(3)
    displacement = np.empty(3)
    new_tangent = np.empty(3)
    for _iteration in range(iterations):
        for c in range(count):
            gap = 0.0
            for axis in range(3):
                separation[axis] = fixed[c, axis]
                for dof in range(len(delta)):
                    separation[axis] += jac[c, axis, dof] * delta[dof]
                for v in range(4):
                    separation[axis] += weights[c, v] * cloth[ids[c, v], axis]
                gap += normals[c, axis] * separation[axis]
            if gap >= thickness and lambdas[c] == 0:
                continue
            new_lambda = max(0.0, lambdas[c] + (thickness - gap) / denominators[c])
            for axis in range(3):
                impulse[axis] = normals[c, axis] * (new_lambda - lambdas[c])
            lambdas[c] = new_lambda
            normal_displacement = 0.0
            for axis in range(3):
                displacement[axis] = separation[axis] - rest[c, axis]
                for other in range(3):
                    displacement[axis] += mobility[c, axis, other] * impulse[other]
                normal_displacement += normals[c, axis] * displacement[axis]
            norm = 0.0
            for axis in range(3):
                new_tangent[axis] = tangents[c, axis]
                for other in range(3):
                    new_tangent[axis] -= tangent_inverse[c, axis, other] * (
                        displacement[other] - normals[c, other] * normal_displacement
                    )
                norm += new_tangent[axis] ** 2
            norm = np.sqrt(norm)
            limit = friction * new_lambda
            for axis in range(3):
                if norm > limit:
                    new_tangent[axis] *= limit / norm
                impulse[axis] += new_tangent[axis] - tangents[c, axis]
                tangents[c, axis] = new_tangent[axis]
            for v in range(4):
                for axis in range(3):
                    cloth[ids[c, v], axis] += pushes[c, v] * impulse[axis]
            for dof in range(len(delta)):
                for axis in range(3):
                    delta[dof] += rigid_push[c, dof, axis] * impulse[axis]
    return lambdas


class ContactSetup:
    def __init__(
        self, model, data, hulls, faces, masses, thickness=0.00025, activation=0.001, friction=0.8, iterations=12
    ):
        if (
            not np.isfinite([thickness, activation, friction]).all()
            or not 0 < thickness < activation
            or iterations < 1
            or friction < 0
        ):
            raise ValueError("Invalid contact parameters")
        self.model, self.data = model, data
        self.count = len(masses)
        faces = np.asarray(faces)
        if (
            faces.ndim != 2
            or faces.shape[1] != 3
            or not np.issubdtype(faces.dtype, np.integer)
            or np.any(faces < 0)
            or np.any(faces >= self.count)
        ):
            raise ValueError("Cloth faces must be integer vertex-index triplets within the mesh")
        masses = np.asarray(masses)
        if not len(masses) or not np.isfinite(masses).all() or np.any(masses <= 0):
            raise ValueError("Masses must be finite and positive")
        self.inverse = 1 / masses
        self.thickness, self.activation = thickness, activation
        self.friction, self.iterations = friction, iterations
        edges = edges_from_faces(faces)
        self.sample_ids = np.vstack(
            [np.repeat(np.arange(self.count)[:, None], 3, axis=1), np.column_stack([edges, edges[:, 0]]), faces]
        )
        self.sample_weights = np.vstack(
            [
                np.tile([1.0, 0, 0], (self.count, 1)),
                np.tile([0.5, 0.5, 0], (len(edges), 1)),
                np.full((len(faces), 3), 1 / 3),
            ]
        )
        adjacent = np.eye(self.count, dtype=bool)
        adjacent[edges[:, 0], edges[:, 1]] = True
        adjacent[edges[:, 1], edges[:, 0]] = True
        self.excluded = (adjacent.astype(int) @ adjacent.astype(int)) > 0
        self.hulls = []
        start = self.count
        for geom, local in hulls:
            # Deduplicate coplanar triangle planes of boxes and convex meshes.
            planes = np.unique(np.round(ConvexHull(local).equations, 12), axis=0)
            affine = np.linalg.pinv(np.column_stack([local, np.ones(len(local))]))
            self.hulls.append((geom, local, planes, affine, slice(start, start + len(local))))
            start += len(local)
