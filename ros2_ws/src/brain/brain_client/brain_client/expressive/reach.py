# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Keep commanded poses physically sane: joint limits, the shoulder-clearance rule and, where mujoco
is installed (host tools, never the robot), no self-collision or floor contact.

The collision check runs on a robot-only MuJoCo model built from mars.urdf's collision shapes plus
the floor. A pose in collision is pulled back along the straight line from NEUTRAL to the furthest
safe point, so projection is stateless and deterministic. The robot relies on the basis instead:
``basis_tool`` bakes this check into basis.json's ``safe`` table.
"""

from __future__ import annotations

import importlib
import math
from pathlib import Path
from typing import Any

import numpy as np

from brain_client.expressive.basis import Act, Basis, Vector

URDF_PATH = Path(__file__).resolve().parents[4] / "mars_bot" / "mars_description" / "urdf" / "mars.urdf"
ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")
FINGERS = ("link61", "link62")
BROADPHASE_M = 0.002
# The collision boxes are simplified; parts that touch on the real arm graze here, so only penetration counts.
PENETRATION_M = 0.0
SEARCH_STEPS = 12
Contact = tuple[str, str, float]


class _Collider:
    """mujoco has no type stubs and is optional, so it is handled as ``Any`` behind this class."""

    def __init__(self, mujoco: Any, urdf: Path) -> None:
        self.mujoco = mujoco
        spec = mujoco.MjSpec.from_string(
            urdf.read_text().replace("package://mars_description/", f"{urdf.parent.parent}/")
        )
        floor = spec.worldbody.add_geom()
        floor.name = "floor"
        floor.type = mujoco.mjtGeom.mjGEOM_PLANE
        floor.size = [2.0, 2.0, 0.1]
        # The finger hub pins overlap by design and the blades cannot cross (joint6M mirrors joint6).
        spec.add_exclude(bodyname1=FINGERS[0], bodyname2=FINGERS[1])
        # base_link is fused into the world body here, and MuJoCo never filters children of the world.
        # link2's shoulder box grazes the arm mount it sits on; arm.srdf marks the pair adjacent too.
        for child in ("link1", "link2", "head"):
            spec.add_exclude(bodyname1="world", bodyname2=child)
        self.model = spec.compile()
        self.model.geom_margin[:] = BROADPHASE_M
        self.data = mujoco.MjData(self.model)
        self.arm = [int(self.model.jnt_qposadr[self.model.joint(name).id]) for name in ARM_JOINTS]
        self.mimic = int(self.model.jnt_qposadr[self.model.joint("joint6M").id])
        self.head = int(self.model.jnt_qposadr[self.model.joint("joint_head").id])

    def contacts(self, q: Vector) -> list[Contact]:
        mujoco, model, data = self.mujoco, self.model, self.data
        for adr, value in zip(self.arm, q[: Act.HEAD_DEG].tolist(), strict=True):
            data.qpos[adr] = value
        data.qpos[self.mimic] = -q[Act.J6]
        data.qpos[self.head] = math.radians(q[Act.HEAD_DEG])
        mujoco.mj_fwdPosition(model, data)
        candidates = {(int(c.geom[0]), int(c.geom[1])) for c in data.contact[: data.ncon]}
        found: list[Contact] = []
        for a, b in sorted(candidates):
            # mj_collision's box-box routine reports phantom deep contacts near edges; GJK decides.
            dist = float(mujoco.mj_geomDistance(model, data, a, b, BROADPHASE_M, None))
            if dist < PENETRATION_M:
                found.append((str(model.geom(a).name), str(model.geom(b).name), dist))
        return found


def _collider(urdf: Path | None) -> _Collider | None:
    if urdf is None or not urdf.is_file():
        return None
    try:
        mujoco = importlib.import_module("mujoco")
    except ImportError:
        return None
    return _Collider(mujoco, urdf)


class Reach:
    def __init__(self, basis: Basis, urdf: Path | None = URDF_PATH) -> None:
        self.basis = basis
        self._collider = _collider(urdf)

    @property
    def checks_collisions(self) -> bool:
        return self._collider is not None

    def contacts(self, q: Vector) -> list[Contact]:
        """Geom pairs interpenetrating at actuator vector ``q`` (empty without mujoco)."""
        return self._collider.contacts(q) if self._collider is not None else []

    def collides(self, q: Vector) -> bool:
        return bool(self.contacts(q))

    def project(self, q: Vector) -> Vector:
        """``q`` clamped, then (with mujoco) pulled toward NEUTRAL until collision-free."""
        q = self.basis.clamp(np.asarray(q, dtype=np.float64))
        if not self.collides(q):
            return q
        start = self.basis.clamp(self.basis.neutral.copy())
        start[Act.J6], start[Act.BASE_YAW], start[Act.BASE_X] = q[Act.J6], q[Act.BASE_YAW], q[Act.BASE_X]
        lo, hi = 0.0, 1.0
        for _ in range(SEARCH_STEPS):
            mid = 0.5 * (lo + hi)
            if self.collides(self.basis.clamp(start + mid * (q - start))):
                hi = mid
            else:
                lo = mid
        return self.basis.clamp(start + lo * (q - start))

    __call__ = project
