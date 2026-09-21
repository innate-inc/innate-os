"""XPBD cloth coupled to MuJoCo. Approximate contacts, not collision-safe.

MuJoCo remains authoritative for rigid dynamics and the streamed flex vertices.
Native elasticity owns only the cloth. No grasp attachments or pose restoration.
Loaded only when the XPBD backend is selected (the simulator default).
"""

import importlib
import os
import sys
import threading
from itertools import product
from pathlib import Path

import mujoco
import numpy as np

from .cloth_fast_contact import FastContacts

TO_PBD = np.array([[1.0, 0, 0], [0, 0, 1.0], [0, -1.0, 0]])
CLOTH_TIMESTEP = 1 / 300
CONTACT_ITERATIONS = 40
_NATIVE_LOCK = threading.RLock()  # The upstream clock and gravity are global.


def load_native():
    if sys.version_info < (3, 11):
        raise RuntimeError("Experimental XPBD requires Python 3.11 or newer")
    from .world import repo_root

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    directory = os.environ.get("INNATE_SIM_XPBD_MODULE_DIR", "").strip()
    if not directory:
        directory = str(repo_root() / "sim" / ".xpbd" / "lib")
    if directory:
        directory = str(Path(directory).expanduser().resolve())
        sys.path.insert(0, directory)
    try:
        return importlib.import_module("pypbd")
    except ImportError as exc:
        raise RuntimeError(
            "XPBD requires the optional patched pypbd build. See sim/sandbox/xpbd_integration.md; "
            "set INNATE_SIM_XPBD_MODULE_DIR to its lib directory. No fallback to the old sock was made."
        ) from exc
    finally:
        if directory:
            sys.path.remove(directory)


def convex_vertices(model, geom):
    """MuJoCo-local convex approximations. Unsupported collision types fail loud."""
    kind, size = model.geom_type[geom], model.geom_size[geom]
    corners = np.asarray(list(product((-1.0, 1.0), repeat=3)))
    if kind == mujoco.mjtGeom.mjGEOM_BOX:
        return corners * size
    if kind == mujoco.mjtGeom.mjGEOM_PLANE:
        # All shipped worlds fit inside this 20 km slab. It is a half-space
        # approximation only near its top, not a general infinite-plane solver.
        return corners * [10000.0, 10000.0, 100.0] - [0, 0, 100.0]
    if kind == mujoco.mjtGeom.mjGEOM_MESH:
        mesh = model.geom_dataid[geom]
        start, count = model.mesh_vertadr[mesh], model.mesh_vertnum[mesh]
        from scipy.spatial import ConvexHull

        points = np.asarray(model.mesh_vert[start : start + count], dtype=float)
        return points[ConvexHull(points).vertices]
    angles = np.arange(32) * (2 * np.pi / 32)
    if kind == mujoco.mjtGeom.mjGEOM_CYLINDER:
        radius, height = size[:2]
        return np.array(
            [
                [radius / np.cos(np.pi / 32) * np.cos(a), radius / np.cos(np.pi / 32) * np.sin(a), z]
                for z in (-height, height)
                for a in angles
            ]
        )
    if kind in (mujoco.mjtGeom.mjGEOM_SPHERE, mujoco.mjtGeom.mjGEOM_CAPSULE, mujoco.mjtGeom.mjGEOM_ELLIPSOID):
        points = np.array(
            [
                [np.cos(a) * np.sin(t), np.sin(a) * np.sin(t), np.cos(t)]
                for t in np.linspace(0, np.pi, 17)
                for a in angles
            ]
        )
        points *= 1 / np.cos(np.pi / 32) ** 2
        if kind == mujoco.mjtGeom.mjGEOM_ELLIPSOID:
            return points * size
        points *= size[0]
        if kind == mujoco.mjtGeom.mjGEOM_CAPSULE:
            points[:, 2] += np.where(points[:, 2] >= 0, size[1], -size[1])
        return points
    raise ValueError(f"XPBD does not support collider {model.geom(geom).name!r} (type {kind})")


def panel_bending_pairs(rest, faces):
    """Distance-bending approximation across flat panel hinges, never seams.

    These local spans resist curvature without a volume or world-space target.
    They also add some in-plane stiffness; this is an artistic cloth preset.
    """
    normals = np.cross(rest[faces[:, 1]] - rest[faces[:, 0]], rest[faces[:, 2]] - rest[faces[:, 0]])
    lengths = np.linalg.norm(normals, axis=1)
    normals /= np.maximum(lengths[:, None], 1e-15)
    edges = {}
    for f, (a, b, c) in enumerate(faces):
        for i, j, k in ((a, b, c), (b, c, a), (c, a, b)):
            edges.setdefault(tuple(sorted((int(i), int(j)))), []).append((int(k), f))
    pairs = set()
    parent = list(range(len(faces)))

    def root(f):
        while parent[f] != f:
            parent[f] = parent[parent[f]]
            f = parent[f]
        return f

    for shared in edges.values():
        if len(shared) == 2:
            (a, f), (b, g) = shared
            if a != b and lengths[f] > 1e-12 and lengths[g] > 1e-12 and normals[f] @ normals[g] > 0.95:
                pairs.add(tuple(sorted((a, b))))
                parent[root(f)] = root(g)
    panels = {}
    for f, face in enumerate(faces):
        panels.setdefault(root(f), set()).update(map(int, face))
    # A few-centimeter bending support stops all curvature concentrating on
    # one coarse hinge. Never bridge the two layers through the sewn boundary.
    for panel in panels.values():
        vertices = sorted(panel)
        for a in vertices:
            candidates = []
            for b in vertices:
                if a == b:
                    continue
                distance = np.linalg.norm(rest[a] - rest[b])
                if 0.02 <= distance <= 0.04:
                    candidates.append((abs(distance - 0.03), b))
            # Bounded support rather than a dense spring network.
            for _, b in sorted(candidates)[:3]:
                pairs.add(tuple(sorted((a, b))))
    return sorted(pairs)


class XPBDClothSet:
    """Independent material/state per prop, sharing the rigid world's cadence.

    As with the single-sock approximation, there is no cloth/cloth collision
    between separate props. Parked props never advance their native solver.
    """

    def __init__(self, model, data, registry):
        self.cloths = {name: XPBDCloth(model, data, registry, name) for name in registry._soft}

    def reset(self):
        for cloth in self.cloths.values():
            cloth.reset()

    def physics_timestep(self, default):
        return CLOTH_TIMESTEP if any(cloth.name in cloth.registry.out for cloth in self.cloths.values()) else default

    def before_step(self, dt):
        for cloth in self.cloths.values():
            cloth.before_step(dt)

    def after_step(self):
        for cloth in self.cloths.values():
            cloth.after_step()


class XPBDCloth:
    """One native cloth model per binding; no shared particle-state ownership."""

    def __init__(self, model, data, registry, name=None):
        self.model, self.data, self.registry = model, data, registry
        if name is None and len(registry._soft) != 1:
            raise ValueError("Experimental XPBD currently requires exactly one deformable prop")
        self.name = name if name is not None else next(iter(registry._soft))
        self.binding = registry._soft[self.name]
        if not np.allclose(model.opt.gravity, [0, 0, -9.81]):
            raise ValueError("Experimental XPBD currently requires standard z-up gravity")
        self.pbd = load_native()
        finger_ids = [
            mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name) for name in ("robot_joint6", "robot_joint6M")
        ]
        self._finger_dofs = np.array([model.jnt_dofadr[j] for j in finger_ids if j >= 0], dtype=int)
        self._finger_ids = np.array([j for j in finger_ids if j >= 0], dtype=int)
        self._finger_body = np.zeros((model.nbody, len(self._finger_ids)), dtype=bool)
        for body in range(1, model.nbody):
            parent = model.body_parentid[body]
            self._finger_body[body] = self._finger_body[parent] | (model.jnt_bodyid[self._finger_ids] == body)
        mesh = self.binding.prop.cloth_data()
        rest, faces = np.asarray(mesh["vertices"], dtype=float), np.asarray(mesh["faces"], dtype=int)
        area = (
            np.linalg.norm(
                np.cross(rest[faces[:, 1]] - rest[faces[:, 0]], rest[faces[:, 2]] - rest[faces[:, 0]]), axis=1
            )
            / 2
        )
        masses = np.zeros(len(rest))
        for column in range(3):
            np.add.at(masses, faces[:, column], area / 3)
        if np.any(masses <= 0) or not np.isfinite(masses).all():
            raise ValueError("XPBD cloth has degenerate or disconnected vertices")
        masses *= self.binding.prop.mass / masses.sum()
        with _NATIVE_LOCK:
            self.pbd.Simulation.getCurrent()  # initializes upstream global gravity
            self.native = self.pbd.SimulationModel()
            self.native.init()
            self.stepper = self.pbd.TimeStepController()
            self.stepper.init()
            for field, value in (("NUM_SUB_STEPS", 1), ("MAX_ITERATIONS", 12), ("MAX_ITERATIONS_V", 12)):
                self.stepper.setValueUInt(getattr(self.pbd.TimeStepController, field), value)
            tri = self.native.addTriangleModel((rest @ TO_PBD.T).tolist(), faces.ravel().tolist(), testMesh=False)
            self.particles = self.native.getParticles()
            if not hasattr(self.particles, "setStateArrays"):
                raise RuntimeError("Rebuild pypbd with xpbd_batch_state.patch; the native batch setter is missing")
            for i, mass in enumerate(masses):
                self.particles.setMass(i, float(mass))
            self.native.addClothConstraints(tri, 4, 1000.0, 1000.0, 1000.0, 1000.0, 0.3, 0.3, False, False)
            self.native.addBendingConstraints(
                tri, self.binding.prop.xpbd_bending_model, self.binding.prop.xpbd_bend_stiffness
            )
            if self.binding.prop.xpbd_panel_bend_stiffness:
                for a, b in panel_bending_pairs(rest, faces):
                    self.native.addDistanceConstraint_XPBD(a, b, self.binding.prop.xpbd_panel_bend_stiffness)
        self.contact = FastContacts(
            model,
            data,
            [],
            faces,
            masses,
            rest_vertices=rest,
            activation=0.003,
            iterations=CONTACT_ITERATIONS,
            thickness=self.binding.prop.xpbd_contact_thickness,
        )
        self.contact.static_iterations = 12
        self._hull_cache = {}
        self._hull_signature = None
        self._revision = None
        self._was_active = False
        self._pending = None
        # Vectorized world-space broad phase; expensive hulls are prepared only
        # when encountered. Far-away rooms and parked props cost no contact solve.
        self._geoms = np.flatnonzero((model.geom_contype != 0) | (model.geom_conaffinity != 0))
        supported = {
            int(getattr(mujoco.mjtGeom, "mjGEOM_" + kind))
            for kind in ("PLANE", "BOX", "MESH", "SPHERE", "CAPSULE", "ELLIPSOID", "CYLINDER")
        }
        unsupported = [model.geom(int(g)).name for g in self._geoms if int(model.geom_type[g]) not in supported]
        if unsupported:
            raise ValueError(f"XPBD does not support these colliders: {unsupported}")
        self._centers = np.asarray(model.geom_aabb[self._geoms, :3]).copy()
        self._extents = np.asarray(model.geom_aabb[self._geoms, 3:]).copy()
        planes = model.geom_type[self._geoms] == mujoco.mjtGeom.mjGEOM_PLANE
        self._centers[planes] = [0, 0, -100]
        self._extents[planes] = [10000, 10000, 100]
        self.binding.external_physics = True
        self.binding.set_active(data, False)
        # Compile before the server starts its real-time loop, not on first drop.
        points = rest.copy()
        self.contact.project(points, points, np.empty((0, 3, 0)), np.empty((0, 0)))

    def reset(self):
        self._was_active = False
        self._revision = None
        self._pending = None

    def physics_timestep(self, default):
        # This opt-in mode uses the validated, synchronous 300 Hz cadence.
        # Defaults and rigid-only scenes are unchanged; no missed substep
        # is allowed between finger motion and cloth contact projection.
        return CLOTH_TIMESTEP if self.name in self.registry.out else default

    def _near_hulls(self, points):
        rotations = self.data.geom_xmat[self._geoms].reshape(-1, 3, 3)
        centers = np.einsum("nij,nj->ni", rotations, self._centers) + self.data.geom_xpos[self._geoms]
        extents = np.einsum("nij,nj->ni", np.abs(rotations), self._extents)
        near = np.all(points.max(0) + 0.05 >= centers - extents, axis=1)
        near &= np.all(points.min(0) - 0.05 <= centers + extents, axis=1)
        near &= ((self.model.geom_conaffinity[self._geoms] & self.binding._contact_type) != 0) | (
            (self.model.geom_contype[self._geoms] & self.binding._contact_affinity) != 0
        )
        signature = tuple(self._geoms[near])
        if signature == self._hull_signature:
            return
        self._hull_signature = signature
        hulls, start = [], len(points)
        for geom in self._geoms[near]:
            if not (
                (self.binding._contact_type & self.model.geom_conaffinity[geom])
                or (self.model.geom_contype[geom] & self.binding._contact_affinity)
            ):
                continue
            if geom not in self._hull_cache:
                local = convex_vertices(self.model, geom)
                from scipy.spatial import ConvexHull

                planes = np.unique(np.round(ConvexHull(local).equations, 12), axis=0)
                affine = np.linalg.pinv(np.column_stack([local, np.ones(len(local))]))
                self._hull_cache[geom] = (local, planes, affine)
            local, planes, affine = self._hull_cache[geom]
            hulls.append((geom, local, planes, affine, slice(start, start + len(local))))
            start += len(local)
        self.contact.hulls = hulls
        self.contact.starts = np.array([h[4].start - len(points) for h in hulls], dtype=int)
        self._local_points = np.vstack([h[1] for h in hulls]) if hulls else np.empty((0, 3))
        self._vertex_geoms = (
            np.concatenate([np.full(len(h[1]), h[0], dtype=int) for h in hulls]) if hulls else np.empty(0, int)
        )
        bodies = self.model.geom_bodyid[self._vertex_geoms]
        self._influence = self._finger_body[bodies]

    def _surface(self, points):
        rigid = np.einsum("nij,nj->ni", self.data.geom_xmat[self._vertex_geoms].reshape(-1, 3, 3), self._local_points)
        rigid += self.data.geom_xpos[self._vertex_geoms]
        return np.vstack([points, rigid])

    def before_step(self, dt):
        """After mj_step1, before rigid integration; capture contact linearization."""
        active = self.name in self.registry.out
        if not active:
            self.reset()
            return
        points = self.binding.vertices(self.data).copy()
        if not self._was_active or self._revision != self.binding.placement_revision:
            with _NATIVE_LOCK:
                self.particles.setStateArrays(points @ TO_PBD.T, np.zeros_like(points))
            self._revision = self.binding.placement_revision
            self._pending = None
        self._was_active = True
        # Rigid and cloth steps are synchronous: every finger motion gets a
        # contact projection, using its pre-step pose for entry-side friction.
        self._near_hulls(points)
        previous = self._surface(points)
        # Interactive approximation: cloth reacts on the two finger hinges.
        # Other rigid poses are collision surfaces, but receive no cloth load.
        used = np.flatnonzero(np.any(self._influence, axis=0))
        dofs = self._finger_dofs[used]
        jac = np.zeros((len(self._local_points), 3, len(dofs)))
        for column, finger in enumerate(used):
            joint = self._finger_ids[finger]
            jac[:, :, column] = (
                np.cross(self.data.xaxis[joint], previous[len(points) :] - self.data.xanchor[joint])
                * self._influence[:, finger, None]
            )
        # These leaf hinges are on disjoint branches: their mass submatrix
        # with the rest of the articulation held fixed is diagonal.
        inverse = np.diag(1 / self.data.qM[self.model.dof_Madr[dofs]])
        with _NATIVE_LOCK:
            self.pbd.TimeManager.getCurrent().setTimeStepSize(dt)
            self.stepper.step(self.native)
            predicted = np.asarray(self.particles.getVertices()).copy() @ TO_PBD
        self._pending = points, previous, predicted, jac, inverse, dofs, dt

    def after_step(self):
        if self._pending is None:
            return
        points, previous, predicted, jac, inverse, dofs, dt = self._pending
        self._pending = None
        # mj_step2 leaves geom_xpos at the pre-integration pose. Refresh before
        # projecting against the actual new rigid positions.
        mujoco.mj_kinematics(self.model, self.data)
        self.contact.iterations = CONTACT_ITERATIONS if len(dofs) else 12
        corrected, delta, self.last_contact_stats = self.contact.project(
            previous, self._surface(predicted), jac, inverse
        )
        if not np.isfinite(corrected).all() or not np.isfinite(delta).all():
            raise RuntimeError("XPBD produced non-finite state")
        displacement = np.zeros(self.model.nv)
        displacement[dofs] = delta
        mujoco.mj_integratePos(self.model, self.data.qpos, displacement, 1.0)
        self.data.qvel[dofs] += delta / dt
        binding = self.binding
        self.data.qpos[binding._qpos_indices] = binding._qpos0 + corrected - binding._compiled_vertices
        binding._zero_motion(self.data)  # MuJoCo flex is a passive stream carrier.
        with _NATIVE_LOCK:
            self.particles.setStateArrays(corrected @ TO_PBD.T, ((corrected - points) / dt) @ TO_PBD.T)
        mujoco.mj_kinematics(self.model, self.data)
        mujoco.mj_flex(self.model, self.data)
