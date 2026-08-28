"""MuJoCo surface-flex props with rest-shape bending.

``SoftProp`` deliberately mirrors the small interface used by ``PropRegistry``
without making a flex pretend to be a rigid free-joint body.  ``SoftBinding``
then resolves one named flex's vertex joints after compilation and owns all
model/data mutations needed to place, park, and step it.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import mujoco
import numpy as np


def _numbers(values: np.ndarray | tuple[float, ...]) -> str:
    """Compact, deterministic MJCF number formatting."""
    return " ".join(f"{float(value):.9g}" for value in np.asarray(values).ravel())


class DihedralBending:
    """Apply discrete-shell rest-dihedral forces to one named flex only."""

    def __init__(
        self,
        model: mujoco.MjModel,
        cloth_data: dict[str, np.ndarray],
        flex_id: int,
        dof_indices: np.ndarray,
        stiffness: float,
        damping_ratio: float,
        quality_min: float,
    ) -> None:
        vertex_adr = int(model.flex_vertadr[flex_id])
        vertex_num = int(model.flex_vertnum[flex_id])
        self._vertex_slice = slice(vertex_adr, vertex_adr + vertex_num)
        self.hinges = np.asarray(cloth_data["hinges"], dtype=np.int32)
        self.rest_angles = np.asarray(cloth_data["rest_angles"], dtype=np.float64)
        rest = np.asarray(cloth_data["vertices"], dtype=np.float64)
        if rest.shape != (vertex_num, 3):
            raise ValueError(f"flex has {vertex_num} vertices but cloth data has shape {rest.shape}")
        if self.hinges.ndim != 2 or self.hinges.shape[1] != 4:
            raise ValueError(f"hinges must be Nx4, got {self.hinges.shape}")
        if len(self.rest_angles) != len(self.hinges):
            raise ValueError("each bending hinge needs one rest angle")
        if self.hinges.size and (self.hinges.min() < 0 or self.hinges.max() >= vertex_num):
            raise ValueError("bending hinge references a vertex outside its named flex")

        h = self.hinges
        x0, x1, x2, x3 = rest[h[:, 0]], rest[h[:, 1]], rest[h[:, 2]], rest[h[:, 3]]
        area0 = 0.5 * np.linalg.norm(np.cross(x2 - x0, x3 - x0), axis=1)
        area1 = 0.5 * np.linalg.norm(np.cross(x3 - x1, x2 - x1), axis=1)
        edge_length = np.linalg.norm(x3 - x2, axis=1)

        def triangle_quality(a: np.ndarray, b: np.ndarray, c: np.ndarray, area: np.ndarray) -> np.ndarray:
            edge_sq_sum = np.sum((a - b) ** 2, axis=1) + np.sum((b - c) ** 2, axis=1) + np.sum((c - a) ** 2, axis=1)
            return 4.0 * np.sqrt(3.0) * area / np.maximum(edge_sq_sum, 1.0e-18)

        quality0 = triangle_quality(x0, x2, x3, area0)
        quality1 = triangle_quality(x1, x3, x2, area1)
        self._usable = (quality0 >= quality_min) & (quality1 >= quality_min)
        self._stiffness = stiffness * 3.0 * edge_length**2 / np.maximum(area0 + area1, 1.0e-18)
        self._damping_ratio = damping_ratio
        self._dof_indices = np.asarray(dof_indices, dtype=np.int32).copy()
        if self._dof_indices.shape != (vertex_num, 3):
            raise ValueError(f"flex DOF map must be {vertex_num}x3, got {self._dof_indices.shape}")

        body_ids = np.asarray(model.flex_vertbodyid[self._vertex_slice], dtype=np.int32)
        self._vertex_mass = np.asarray(model.body_mass[body_ids], dtype=np.float64)
        if np.any(self._vertex_mass <= 0.0):
            raise ValueError("custom cloth bending requires positive mass at every flex vertex")
        self.last_max_vertex_force = 0.0
        self.last_rms_vertex_force = 0.0

    def apply(self, data: mujoco.MjData) -> tuple[float, float]:
        """Accumulate bending force and return RMS/max hinge-angle error."""
        if not len(self.hinges):
            return 0.0, 0.0

        x = np.asarray(data.flexvert_xpos)[self._vertex_slice]
        velocity = np.asarray(data.qvel)[self._dof_indices]
        h = self.hinges
        x0, x1, x2, x3 = x[h[:, 0]], x[h[:, 1]], x[h[:, 2]], x[h[:, 3]]
        v0, v1, v2, v3 = velocity[h[:, 0]], velocity[h[:, 1]], velocity[h[:, 2]], velocity[h[:, 3]]
        n1 = np.cross(x2 - x0, x3 - x0)
        n2 = np.cross(x3 - x1, x2 - x1)
        edge = x3 - x2
        n1_length = np.linalg.norm(n1, axis=1)
        n2_length = np.linalg.norm(n2, axis=1)
        edge_length = np.linalg.norm(edge, axis=1)
        valid = self._usable & (n1_length > 1.0e-9) & (n2_length > 1.0e-9) & (edge_length > 1.0e-9)

        n1_hat = n1 / np.maximum(n1_length[:, None], 1.0e-9)
        n2_hat = n2 / np.maximum(n2_length[:, None], 1.0e-9)
        edge_hat = edge / np.maximum(edge_length[:, None], 1.0e-9)
        sine = np.einsum("ij,ij->i", np.cross(n1_hat, n2_hat), edge_hat)
        cosine = np.einsum("ij,ij->i", n1_hat, n2_hat)
        theta = np.arctan2(sine, cosine)
        angle_error = np.arctan2(np.sin(theta - self.rest_angles), np.cos(theta - self.rest_angles))

        # Exact signed-dihedral gradients. The topology is
        # (opposite0, opposite1, edge0, edge1).
        d0 = -edge_length[:, None] * n1 / np.maximum(n1_length[:, None] ** 2, 1.0e-18)
        d1 = -edge_length[:, None] * n2 / np.maximum(n2_length[:, None] ** 2, 1.0e-18)
        edge_length_sq = np.maximum(edge_length**2, 1.0e-18)
        alpha = np.einsum("ij,ij->i", x0 - x3, edge) / edge_length_sq
        beta = np.einsum("ij,ij->i", x1 - x3, edge) / edge_length_sq
        d2 = alpha[:, None] * d0 + beta[:, None] * d1
        d3 = -d0 - d1 - d2
        angular_rate = (
            np.einsum("ij,ij->i", d0, v0)
            + np.einsum("ij,ij->i", d1, v1)
            + np.einsum("ij,ij->i", d2, v2)
            + np.einsum("ij,ij->i", d3, v3)
        )
        inverse_effective_mass = (
            np.sum(d0**2, axis=1) / self._vertex_mass[h[:, 0]]
            + np.sum(d1**2, axis=1) / self._vertex_mass[h[:, 1]]
            + np.sum(d2**2, axis=1) / self._vertex_mass[h[:, 2]]
            + np.sum(d3**2, axis=1) / self._vertex_mass[h[:, 3]]
        )
        effective_mass = 1.0 / np.maximum(inverse_effective_mass, 1.0e-18)
        damping = 2.0 * self._damping_ratio * np.sqrt(self._stiffness * effective_mass)
        scalar = -(self._stiffness * angle_error + damping * angular_rate)
        scalar[~valid] = 0.0

        force = np.zeros((len(self._dof_indices), 3), dtype=np.float64)
        for column, derivative in enumerate((d0, d1, d2, d3)):
            np.add.at(force, h[:, column], derivative * scalar[:, None])
        force_norm = np.linalg.norm(force, axis=1)
        self.last_max_vertex_force = float(np.max(force_norm))
        self.last_rms_vertex_force = float(np.sqrt(np.mean(force_norm**2)))
        data.qfrc_applied[self._dof_indices] += force

        if not np.any(valid):
            return 0.0, 0.0
        valid_error = angle_error[valid]
        return float(np.sqrt(np.mean(valid_error**2))), float(np.max(np.abs(valid_error)))


class SoftBinding:
    """Compiled addresses and lifecycle operations for one ``SoftProp``."""

    def __init__(self, prop: SoftProp, model: mujoco.MjModel, park_xy: tuple[float, float]) -> None:
        self.prop = prop
        self.model = model
        flex_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_FLEX, prop.name)
        if flex_id < 0:
            raise ValueError(f"compiled model has no flex named {prop.name!r}")
        self.flex_id = int(flex_id)

        vertex_adr = int(model.flex_vertadr[flex_id])
        vertex_num = int(model.flex_vertnum[flex_id])
        body_ids = np.asarray(model.flex_vertbodyid[vertex_adr : vertex_adr + vertex_num], dtype=np.int32)
        if np.any(np.asarray(model.body_jntnum)[body_ids] != 3):
            raise RuntimeError("a direct full-DOF soft prop needs three slide joints per control vertex")
        first_joints = np.asarray(model.body_jntadr, dtype=np.int32)[body_ids]
        joint_ids = first_joints[:, None] + np.arange(3, dtype=np.int32)[None, :]
        self._qpos_indices = np.asarray(model.jnt_qposadr, dtype=np.int32)[joint_ids]
        self._dof_indices = np.asarray(model.jnt_dofadr, dtype=np.int32)[joint_ids]
        axes = np.asarray(model.jnt_axis, dtype=np.float64)[joint_ids]
        expected_axes = np.broadcast_to(np.eye(3), axes.shape)
        if not np.allclose(axes, expected_axes, atol=1.0e-9):
            raise RuntimeError("soft-prop slide joints are not world-aligned XYZ triplets")

        cloth_data = prop.cloth_data()
        self._local_vertices = np.asarray(cloth_data["vertices"], dtype=np.float64).copy()
        if self._local_vertices.shape != (vertex_num, 3):
            raise ValueError(f"flex has {vertex_num} vertices but cloth data has {len(self._local_vertices)}")
        park_x, park_y = park_xy
        self._compiled_vertices = self._local_vertices + np.asarray((park_x, park_y, prop.rest_z))
        self._qpos0 = np.asarray(model.qpos0[self._qpos_indices], dtype=np.float64).copy()

        eq_type = np.asarray(model.eq_type)
        eq_obj1id = np.asarray(model.eq_obj1id)
        self._equality_ids = np.flatnonzero((eq_type == int(mujoco.mjtEq.mjEQ_FLEX)) & (eq_obj1id == self.flex_id))
        if not len(self._equality_ids):
            raise RuntimeError(f"soft prop {prop.name!r} has no edge-length flex equality")
        self._equality_active = np.asarray(model.eq_active0[self._equality_ids]).copy()
        self._contact_type = int(model.flex_contype[self.flex_id])
        self._contact_affinity = int(model.flex_conaffinity[self.flex_id])
        self.bending = DihedralBending(
            model,
            cloth_data,
            self.flex_id,
            self._dof_indices,
            prop.bend_stiffness,
            prop.bend_damping_ratio,
            prop.hinge_quality_min,
        )

    def _zero_motion(self, data: mujoco.MjData) -> None:
        data.qvel[self._dof_indices] = 0.0
        data.qfrc_applied[self._dof_indices] = 0.0
        data.qacc[self._dof_indices] = 0.0
        data.qacc_warmstart[self._dof_indices] = 0.0

    def set_pose(self, data: mujoco.MjData, x: float, y: float, z: float, yaw: float) -> None:
        """Place the rest sock at an anchor pose; lowest rest vertex is at z."""
        cosine, sine = math.cos(yaw), math.sin(yaw)
        target = self._local_vertices.copy()
        local_x, local_y = target[:, 0].copy(), target[:, 1].copy()
        target[:, 0] = cosine * local_x - sine * local_y + x
        target[:, 1] = sine * local_x + cosine * local_y + y
        target[:, 2] += z
        data.qpos[self._qpos_indices] = self._qpos0 + target - self._compiled_vertices
        self._zero_motion(data)

    def park(self, data: mujoco.MjData) -> None:
        """Restore the compiled off-map rest pose and clear stale dynamics."""
        data.qpos[self._qpos_indices] = self._qpos0
        self._zero_motion(data)

    def set_active(self, data: mujoco.MjData, active: bool) -> None:
        """Enable/disable this flex's contacts and edge equalities."""
        self.model.flex_contype[self.flex_id] = self._contact_type if active else 0
        self.model.flex_conaffinity[self.flex_id] = self._contact_affinity if active else 0
        data.eq_active[self._equality_ids] = self._equality_active if active else 0

    def prepare_step(self, data: mujoco.MjData) -> None:
        """Pin a parked flex at its compiled pose without adding constraints."""
        self.park(data)
        self.set_active(data, False)

    def apply_forces(self, data: mujoco.MjData) -> tuple[float, float]:
        # qfrc_applied persists across steps. Clear only this flex's DOFs so
        # bending is integrated once without erasing unrelated external force
        # users elsewhere in the model.
        data.qfrc_applied[self._dof_indices] = 0.0
        return self.bending.apply(data)

    def vertices(self, data: mujoco.MjData) -> np.ndarray:
        """Current control vertices in world space, including after mj_step2."""
        displacement = np.asarray(data.qpos)[self._qpos_indices] - self._qpos0
        return self._compiled_vertices + displacement

    def pose(self, data: mujoco.MjData) -> list[float]:
        """Centroid pose for APIs that require one representative rigid pose."""
        center = self.vertices(data).mean(axis=0)
        return [float(center[0]), float(center[1]), float(center[2]), 1.0, 0.0, 0.0, 0.0]

    def center_xy(self, data: mujoco.MjData) -> tuple[float, float]:
        center = self.vertices(data).mean(axis=0)
        return float(center[0]), float(center[1])


@dataclass
class SoftProp:
    """Sidecar definition for one direct, full-DOF MuJoCo surface flex."""

    is_deformable: ClassVar[bool] = True

    name: str
    data: str
    deformable_id: int
    label: str = "?"
    title: str = ""
    group: str | None = None
    texture: str | None = None
    rgba: tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0)
    size: tuple[float, ...] = (0.05, 0.03, 0.10)
    collision: str = "flex"
    mass: float = 0.02
    radius: float = 4.0e-5
    condim: int = 3
    friction: tuple[float, float, float] = (0.3, 0.01, 0.001)
    contact_solref: tuple[float, float] = (0.003, 1.0)
    contact_solimp: tuple[float, ...] = (0.95, 0.99, 0.001)
    edge_solref: tuple[float, float] = (0.002, 1.0)
    edge_solimp: tuple[float, ...] = (0.99, 0.999, 0.001)
    edge_damping: float = 0.001
    bend_stiffness: float = 5.0e-7
    bend_damping_ratio: float = 0.04
    hinge_quality_min: float = 0.10
    rest_z: float = 0.0
    drop_z: float | None = None
    reach: tuple[float, float] = (0.6, 0.0)
    center_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    viewer: dict = field(default_factory=dict)
    root: Path | None = field(default=None, repr=False)
    _cloth_cache: dict[str, np.ndarray] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.title:
            self.title = self.name.replace("_", " ").capitalize()
        if self.drop_z is None:
            self.drop_z = self.rest_z

    @property
    def cloth_path(self) -> Path | None:
        if self.root is None:
            return None
        path = (self.root / self.data).resolve()
        return path if path.is_file() else None

    @property
    def mesh_path(self) -> None:
        return None

    @property
    def texture_path(self) -> Path | None:
        if self.texture is None or self.root is None:
            return None
        path = (self.root / self.texture).resolve()
        return path if path.is_file() else None

    @property
    def collision_pieces(self) -> list[Path]:
        return []

    def cloth_data(self) -> dict[str, np.ndarray]:
        if self._cloth_cache is None:
            path = self.cloth_path
            if path is None:
                raise FileNotFoundError(f"soft prop {self.name!r} has no installed cloth data at {self.data!r}")
            with np.load(path) as stored:
                self._cloth_cache = {key: np.asarray(stored[key]).copy() for key in stored.files}
            vertices = self._cloth_cache.get("vertices")
            faces = self._cloth_cache.get("faces")
            if vertices is None or vertices.ndim != 2 or vertices.shape[1] != 3:
                raise ValueError(f"soft prop {self.name!r} needs Nx3 vertices")
            if faces is None or faces.ndim != 2 or faces.shape[1] != 3:
                raise ValueError(f"soft prop {self.name!r} needs Mx3 triangle faces")
        return self._cloth_cache

    def asset_files(self) -> list[Path]:
        return [path for path in (self.cloth_path, self.texture_path) if path is not None]

    def assets_xml(self, visual_group: int) -> str:
        del visual_group
        texture = self.texture_path
        if texture is None:
            return ""
        path = html.escape(str(texture), quote=True)
        return (
            f'\n    <texture name="tex_{self.name}" type="2d" file="{path}"/>'
            f'\n    <material name="mat_{self.name}" texture="tex_{self.name}" '
            'specular="0.05" shininess="0.1"/>'
        )

    def body_xml(self, park_x: float, park_y: float, visual_group: int, collision_group: int) -> str:
        del collision_group
        cloth = self.cloth_data()
        vertices = np.asarray(cloth["vertices"], dtype=np.float64)
        faces = np.asarray(cloth["faces"], dtype=np.int32)
        if faces.size and (faces.min() < 0 or faces.max() >= len(vertices)):
            raise ValueError(f"soft prop {self.name!r} has a face index outside its vertex array")
        appearance = f'material="mat_{self.name}"' if self.texture_path is not None else f'rgba="{_numbers(self.rgba)}"'
        texcoord = ""
        if self.texture_path is not None:
            uvs = np.asarray(cloth.get("uvs"), dtype=np.float64)
            if uvs.shape != (len(vertices), 2):
                raise ValueError(f"soft prop {self.name!r} texture needs one UV pair per control vertex")
            texcoord = f'\n              texcoord="{_numbers(uvs)}"'
        return f"""
    <flexcomp name="{self.name}" type="direct" dim="2" dof="full"
              pos="{park_x:.4f} {park_y:.4f} {self.rest_z:g}"
              radius="{self.radius:g}" mass="{self.mass:g}" group="{visual_group}"
              {appearance} flatskin="false"
              point="{_numbers(vertices)}"
              element="{" ".join(str(int(index)) for index in faces.ravel())}"{texcoord}>
      <contact contype="1" conaffinity="1" condim="{self.condim}"
               friction="{_numbers(self.friction)}" margin="0"
               solref="{_numbers(self.contact_solref)}" solimp="{_numbers(self.contact_solimp)}"
               internal="false" selfcollide="auto"/>
      <edge equality="true" solref="{_numbers(self.edge_solref)}"
            solimp="{_numbers(self.edge_solimp)}" damping="{self.edge_damping:g}"/>
    </flexcomp>"""

    def manifest(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "title": self.title,
            "group": self.group,
            "collision": self.collision,
            "size": list(self.size),
            "rgba": list(self.rgba),
            "viewer": self.viewer,
        }

    def bind(self, model: mujoco.MjModel, park_xy: tuple[float, float]) -> SoftBinding:
        return SoftBinding(self, model, park_xy)
