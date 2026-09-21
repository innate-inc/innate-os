"""Compiled implementation of the approximate convex/particle contact model."""

import numpy as np
from numba import njit

from .cloth_contact_primitives import ContactSetup, edges_from_faces, project_kernel


@njit(cache=True)
def solve(
    previous,
    predicted,
    sample_ids,
    sample_weights,
    inverse,
    excluded,
    planes,
    ranges,
    old_transforms,
    rotations,
    positions,
    lower,
    upper,
    jac_coeff,
    inverse_mass,
    thickness,
    activation,
    friction,
    iterations,
    edges,
    rest_lengths,
    strain_limit,
):
    n = len(inverse)
    ns, ng, nd = len(sample_ids), len(ranges), len(inverse_mass)
    capacity = ns * ng + n * (n - 1) // 2
    ids = np.zeros((capacity, 4), np.int64)
    weights = np.zeros((capacity, 4))
    jac = np.zeros((capacity, 3, nd))
    normals = np.empty((capacity, 3))
    rest = np.empty((capacity, 3))
    fixed = np.empty((capacity, 3))
    count = 0
    for s in range(ns):
        point, old_point = np.zeros(3), np.zeros(3)
        for v in range(3):
            point += sample_weights[s, v] * predicted[sample_ids[s, v]]
            old_point += sample_weights[s, v] * previous[sample_ids[s, v]]
        for g in range(ng):
            if (
                point[0] < lower[g, 0]
                or point[1] < lower[g, 1]
                or point[2] < lower[g, 2]
                or point[0] > upper[g, 0]
                or point[1] > upper[g, 1]
                or point[2] > upper[g, 2]
            ):
                continue
            local, old_local = np.zeros(3), np.zeros(3)
            for a in range(3):
                for b in range(3):
                    local[a] += (point[b] - positions[g, b]) * rotations[g, b, a]
                    old_local[a] += (old_point[b] - old_transforms[g, 3, b]) * old_transforms[g, a, b]
            best, index = -1e100, 0
            for p in range(ranges[g, 0], ranges[g, 1]):
                distance = planes[p, 3]
                for a in range(3):
                    distance += old_local[a] * planes[p, a]
                if distance > best:
                    best, index = distance, p
            distance = planes[index, 3]
            for a in range(3):
                distance += local[a] * planes[index, a]
            if distance >= activation:
                continue
            contact = local - distance * planes[index, :3]
            normal = np.zeros(3)
            q, old_q = positions[g].copy(), old_transforms[g, 3].copy()
            for a in range(3):
                for b in range(3):
                    normal[a] += rotations[g, a, b] * planes[index, b]
                    q[a] += rotations[g, a, b] * contact[b]
                    old_q[a] += contact[b] * old_transforms[g, b, a]
            for v in range(3):
                ids[count, v], weights[count, v] = sample_ids[s, v], sample_weights[s, v]
            for a in range(3):
                normals[count, a] = normal[a]
                rest[count, a] = old_point[a] - old_q[a]
                fixed[count, a] = -q[a]
                for d in range(nd):
                    jac[count, a, d] = -jac_coeff[g, 3, a, d]
                    for b in range(3):
                        jac[count, a, d] -= contact[b] * jac_coeff[g, b, a, d]
            count += 1
    for i in range(n):
        for j in range(i + 1, n):
            if excluded[i, j]:
                continue
            dx = predicted[i, 0] - predicted[j, 0]
            dy = predicted[i, 1] - predicted[j, 1]
            dz = predicted[i, 2] - predicted[j, 2]
            if dx * dx + dy * dy + dz * dz >= activation * activation:
                continue
            diff = previous[i] - previous[j]
            length = np.sqrt(np.dot(diff, diff))
            if length <= 1e-10:
                continue
            ids[count, 0], ids[count, 1] = i, j
            weights[count, 0], weights[count, 1] = 1.0, -1.0
            normals[count], rest[count], fixed[count] = diff / length, diff, np.zeros(3)
            count += 1
    ids, weights, jac = ids[:count], weights[:count], jac[:count]
    normals, rest, fixed = normals[:count], rest[:count], fixed[:count]
    pushes = np.empty((count, 4))
    rigid_push = np.empty((count, nd, 3))
    mobility = np.empty((count, 3, 3))
    tangent = np.empty((count, 3, 3))
    denominator = np.empty(count)
    for c in range(count):
        mass = 0.0
        for v in range(4):
            pushes[c, v] = inverse[ids[c, v]] * weights[c, v]
            mass += pushes[c, v] * weights[c, v]
        if nd == 0 or not np.any(jac[c]):
            # Static-world/self contact has isotropic mobility. Avoid hundreds
            # of tiny matrix products and tangent inversions per floor step.
            denominator[c] = mass
            rigid_push[c] = 0.0
            for a in range(3):
                for b in range(3):
                    identity = 1.0 if a == b else 0.0
                    mobility[c, a, b] = identity * mass
                    tangent[c, a, b] = (identity - normals[c, a] * normals[c, b]) / mass
            continue
        for d in range(nd):
            for a in range(3):
                rigid_push[c, d, a] = 0.0
                for j in range(nd):
                    rigid_push[c, d, a] += inverse_mass[d, j] * jac[c, a, j]
        for a in range(3):
            for b in range(3):
                mobility[c, a, b] = mass if a == b else 0.0
                for d in range(nd):
                    mobility[c, a, b] += jac[c, a, d] * rigid_push[c, d, b]
        normal = normals[c]
        denominator[c] = 0.0
        for a in range(3):
            for b in range(3):
                denominator[c] += normal[a] * mobility[c, a, b] * normal[b]
        # Exact inverse on the two-dimensional tangent plane, not a per-contact SVD.
        axis = 0
        for i in range(1, 3):
            if abs(normal[i]) < abs(normal[axis]):
                axis = i
        u, v = np.zeros(3), np.empty(3)
        u[(axis + 1) % 3], u[(axis + 2) % 3] = normal[(axis + 2) % 3], -normal[(axis + 1) % 3]
        length = np.sqrt(u[0] ** 2 + u[1] ** 2 + u[2] ** 2)
        for i in range(3):
            u[i] /= length
        for i in range(3):
            v[i] = normal[(i + 1) % 3] * u[(i + 2) % 3] - normal[(i + 2) % 3] * u[(i + 1) % 3]
        a, b, d = 0.0, 0.0, 0.0
        for i in range(3):
            for j in range(3):
                a += u[i] * mobility[c, i, j] * u[j]
                b += u[i] * mobility[c, i, j] * v[j]
                d += v[i] * mobility[c, i, j] * v[j]
        for i in range(3):
            for j in range(3):
                tangent[c, i, j] = (d * u[i] * u[j] - b * (u[i] * v[j] + v[i] * u[j]) + a * v[i] * v[j]) / (
                    a * d - b * b
                )
    cloth, delta = predicted[:n].copy(), np.zeros(nd)
    lambdas = project_kernel(
        cloth,
        delta,
        ids,
        weights,
        pushes,
        jac,
        rigid_push,
        normals,
        tangent,
        denominator,
        mobility,
        rest,
        fixed,
        thickness,
        friction,
        iterations,
    )
    # Local, mass-weighted strain projection; no world-space pins or rest-pose
    # shape matching. Compression remains free so the fabric can fold.
    if strain_limit > 0:
        for _sweep in range(6):
            for e in range(len(edges)):
                i, j = edges[e]
                diff = cloth[j] - cloth[i]
                length = np.sqrt(np.dot(diff, diff))
                maximum = rest_lengths[e] * (1 + strain_limit)
                if length > maximum:
                    correction = diff * ((length - maximum) / (length * (inverse[i] + inverse[j])))
                    cloth[i] += inverse[i] * correction
                    cloth[j] -= inverse[j] * correction
    gap = np.inf
    for c in range(count):
        separation = fixed[c] + jac[c] @ delta
        for v in range(4):
            separation += weights[c, v] * cloth[ids[c, v]]
        gap = min(gap, np.dot(normals[c], separation))
    return cloth, delta, count, np.count_nonzero(lambdas > 0), gap


class FastContacts(ContactSetup):
    def __init__(self, model, data, hulls, faces, masses, *, rest_vertices=None, strain_limit=0.15, **kwargs):
        super().__init__(model, data, hulls, faces, masses, **kwargs)
        if not np.isfinite(strain_limit) or strain_limit < 0:
            raise ValueError("Invalid strain limit")
        self.starts = np.array([h[4].start - self.count for h in self.hulls], dtype=int)
        self.strain_limit = strain_limit if rest_vertices is not None else 0.0
        self.edges = edges_from_faces(faces) if rest_vertices is not None else np.empty((0, 2), dtype=int)
        self.rest_lengths = (
            np.linalg.norm(rest_vertices[self.edges[:, 0]] - rest_vertices[self.edges[:, 1]], axis=1)
            if rest_vertices is not None
            else np.empty(0)
        )

    def project(self, previous, predicted, rigid_jacobian, inverse_mass):
        if (
            len(previous) != self.count + sum(len(h[1]) for h in self.hulls)
            or previous.ndim != 2
            or previous.shape != predicted.shape
            or previous.shape[1] != 3
            or inverse_mass.ndim != 2
            or inverse_mass.shape[0] != inverse_mass.shape[1]
            or rigid_jacobian.shape != (len(previous) - self.count, 3, len(inverse_mass))
            or not all(np.isfinite(x).all() for x in (previous, predicted, rigid_jacobian, inverse_mass))
        ):
            raise ValueError("Invalid approximate contact input")
        low, high = predicted[: self.count].min(0), predicted[: self.count].max(0)
        selected = []
        if len(self.starts):
            lows = np.minimum.reduceat(predicted[self.count :], self.starts) - self.activation
            highs = np.maximum.reduceat(predicted[self.count :], self.starts) + self.activation
            for i in np.flatnonzero(np.all(high >= lows, axis=1) & np.all(low <= highs, axis=1)):
                g, _local, planes, affine, section = self.hulls[i]
                selected.append((g, planes, affine, section, lows[i], highs[i]))
        ng, nd = len(selected), len(inverse_mass)
        ranges = np.empty((ng, 2), dtype=np.int64)
        old = np.empty((ng, 4, 3))
        rotations, positions = np.empty((ng, 3, 3)), np.empty((ng, 3))
        lower, upper = np.empty((ng, 3)), np.empty((ng, 3))
        coeff = np.empty((ng, 4, 3, nd))
        plane_list, offset = [], 0
        for k, (g, planes, affine, section, lo, hi) in enumerate(selected):
            plane_list.append(planes)
            ranges[k] = [offset, offset + len(planes)]
            offset += len(planes)
            old[k] = affine @ previous[section]
            rotations[k] = self.data.geom_xmat[g].reshape(3, 3)
            positions[k] = self.data.geom_xpos[g]
            lower[k], upper[k] = lo, hi
            coeff[k] = (
                affine
                @ rigid_jacobian[section.start - self.count : section.stop - self.count].reshape(
                    section.stop - section.start, 3 * nd
                )
            ).reshape(4, 3, nd)
        # Broad-phase neighbors need not actually contact the cloth. Do not
        # carry the robot's dense generalized coordinates into a floor-only
        # solve merely because the gripper is nearby.
        used = np.flatnonzero(np.any(coeff != 0, axis=(0, 1, 2)))
        iterations = self.iterations if len(used) else getattr(self, "static_iterations", self.iterations)
        cloth, reduced_delta, count, active, gap = solve(
            previous,
            predicted,
            self.sample_ids,
            self.sample_weights,
            self.inverse,
            self.excluded,
            np.concatenate(plane_list) if plane_list else np.empty((0, 4)),
            ranges,
            old,
            rotations,
            positions,
            lower,
            upper,
            np.ascontiguousarray(coeff[:, :, :, used]),
            np.ascontiguousarray(inverse_mass[np.ix_(used, used)]),
            self.thickness,
            self.activation,
            self.friction,
            iterations,
            self.edges,
            self.rest_lengths,
            self.strain_limit,
        )
        delta = np.zeros(nd)
        if len(used) == nd:
            delta[:] = reduced_delta
        elif len(used):
            # A coordinate absent from the contact Jacobian can still move
            # through off-diagonal articulated inertia. Restore that response.
            delta[:] = inverse_mass[:, used] @ np.linalg.solve(inverse_mass[np.ix_(used, used)], reduced_delta)
        return (
            cloth,
            delta,
            dict(
                candidates=int(count),
                active=int(active),
                minimum_linear_gap=float(gap) if count else None,
            ),
        )
