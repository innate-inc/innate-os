"""A dedicated VirtualMars arm workspace; no ROS or physical-robot transport."""

import math
import time

import mujoco
import numpy as np
from mars_sim_driver.core import ASSETS_DIR, KP_JOINT, VirtualMars, joint2_min_target
from mars_sim_driver.environments import Environment
from mars_sim_driver.world import ARM_BACKLASH_RAD, BACKLASH_TANH_NM, STRUCT_STIFFNESS

GROUND_Z = 0.0
TOP_Z = 0.27
CENTER = np.array([0.29, -0.053, (GROUND_Z + TOP_Z) / 2])
SPAN = np.array([0.045, 0.10, (TOP_Z - GROUND_Z) / 2])
WATCHDOG_S = 0.35
MAX_JOINT_SPEED = 1.0


class ArmWorld:
    def __init__(self):
        self.sim = VirtualMars(environment=Environment.load("void", ASSETS_DIR))
        self.model, self.data = self.sim.model, self.sim.data
        self.names = [f"joint{i}" for i in range(1, 6)]
        ids = [self.model.joint(f"robot_{name}").id for name in self.names]
        self.qadr = self.model.jnt_qposadr[ids]
        self.dadr = self.model.jnt_dofadr[ids]
        self.limits = self.model.jnt_range[ids].copy()
        self.ee = self.model.body("robot_ee_link").id
        self.ground_geom = self.model.geom("ground").id
        self.finger_geoms = {
            i
            for i in range(self.model.ngeom)
            if self.model.body(self.model.geom_bodyid[i]).name in ("robot_link61", "robot_link62")
            and (self.model.geom_contype[i] or self.model.geom_conaffinity[i])
        }
        self.ik_data = mujoco.MjData(self.model)
        self.command = np.array([0.0, 0.4, -0.3, 0.5, 0.0])
        self.desired = CENTER.copy()
        self.desired[2] = GROUND_Z
        self.command = self.solve(self.desired, self.command)
        # A fresh dedicated simulator boots in its working pose. Runtime motion
        # always goes through the existing physics servos, never a visual pose.
        self.data.qpos[self.qadr] = self.command
        self.data.qvel[:] = 0
        mujoco.mj_forward(self.model, self.data)
        gripper = self.model.joint("robot_joint6")
        self.grip_limits = self.model.jnt_range[gripper.id].copy()
        self.grip_qadr = self.model.jnt_qposadr[gripper.id]
        self.grip_target = 1.0
        self.sim.set_joint_target("joint6", float(self.grip_limits[1]))
        self.active = False
        self.last_input = -math.inf
        self.reason = "ready"
        for _ in range(100):
            self.drive(self.command, 0.01, initialize=True)
            self.sim.step(0.01)
        self.hold("ready")

    def constrain(self, values, *, physical=False):
        values = np.clip(values, self.limits[:, 0], self.limits[:, 1])
        values[1] = max(values[1], joint2_min_target(values[0], self.limits[1, 0]) + (0.09 if physical else 0))
        return values

    def solve(self, target, seed):
        d = self.ik_data
        d.qpos[:] = self.data.qpos
        angles = self.constrain(np.array(seed, dtype=float), physical=True)
        jac = np.zeros((3, self.model.nv))
        for _ in range(35):
            d.qpos[self.qadr] = angles
            mujoco.mj_forward(self.model, d)
            error = target - d.xpos[self.ee]
            if np.linalg.norm(error) < 0.0008:
                break
            mujoco.mj_jacBody(self.model, d, jac, None, self.ee)
            arm_jac = jac[:, self.dadr]
            delta = arm_jac.T @ np.linalg.solve(arm_jac @ arm_jac.T + 0.02**2 * np.eye(3), error)
            angles = self.constrain(angles + np.clip(delta, -0.08, 0.08), physical=True)
        return angles

    def gravity_offset(self):
        bias = self.data.qfrc_bias[self.dadr]
        # Invert the simulator's documented structural sag + static PD error.
        # This uses physics state, and does not alter the shared robot model.
        return bias / STRUCT_STIFFNESS + ARM_BACKLASH_RAD * np.tanh(bias / BACKLASH_TANH_NM) + bias / KP_JOINT

    def drive(self, angles, dt, initialize=False):
        target = self.constrain(angles + self.gravity_offset())
        current = np.array([self.sim.joint_targets()[name] for name in self.names])
        command = (
            target if initialize else current + np.clip(target - current, -MAX_JOINT_SPEED * dt, MAX_JOINT_SPEED * dt)
        )
        for name, value in zip(self.names, command, strict=True):
            self.sim.set_joint_target(name, float(value))

    def move(self, horizontal, vertical, reach=0, grip=1, now=None):
        if not all(
            isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)
            for v in (horizontal, vertical, reach, grip)
        ):
            raise ValueError("Expected finite control values")
        if max(abs(horizontal), abs(vertical), abs(reach)) > 1.0001 or not 0 <= grip <= 1:
            raise ValueError("Control values outside workspace")
        self.desired = CENTER + SPAN * np.clip([reach, horizontal, vertical], -1, 1)
        self.grip_target = grip
        self.last_input = time.monotonic() if now is None else now
        self.active = True
        self.reason = "following"

    def hold(self, reason="paused"):
        self.active = False
        self.reason = reason
        self.command = self.data.qpos[self.qadr].copy()
        self.desired = self.data.xpos[self.ee].copy()
        self.path_target = self.desired.copy()
        # Remove the ahead-of-arm setpoint immediately; physical inertia is
        # still handled by the PD servos and damping in VirtualMars.
        self.drive(self.command, 0, initialize=True)

    def tick(self, dt, now=None):
        now = time.monotonic() if now is None else now
        if self.active and now - self.last_input > WATCHDOG_S:
            self.hold("input_timeout")
        if self.active:
            distance = self.desired - self.path_target
            # Cartesian target advances at <= 0.18 m/s. The independent joint
            # clamp handles singularities and difficult edge configurations.
            target = self.path_target + distance * min(1, 0.18 * dt / max(np.linalg.norm(distance), 1e-9))
            self.path_target = target
            self.command = self.solve(target, self.command)
            self.drive(self.command, dt)
            lo, hi = self.grip_limits
            target = lo + self.grip_target * (hi - lo)
            current = self.sim.joint_targets()["joint6"]
            self.sim.set_joint_target("joint6", float(current + np.clip(target - current, -2.5 * dt, 2.5 * dt)))
        self.sim.step(dt)

    def ground_contacts(self):
        return [
            c
            for c in self.data.contact
            if (c.geom1 == self.ground_geom and c.geom2 in self.finger_geoms)
            or (c.geom2 == self.ground_geom and c.geom1 in self.finger_geoms)
        ]

    def snapshot(self):
        angles = {
            name: float(self.data.qpos[self.model.jnt_qposadr[self.model.joint(f"robot_{name}").id]])
            for name in (*self.names, "joint6", "joint_head")
        }
        ee = self.data.xpos[self.ee]
        grounded = bool(self.ground_contacts())
        return {
            "type": "state",
            "simulated": True,
            "active": self.active,
            "reason": self.reason,
            "joints": angles,
            "pose": self.sim.pose(),
            "ee": ee.tolist(),
            "target": self.desired.tolist(),
            "offset": np.clip(((self.desired - CENTER) / SPAN)[[1, 2, 0]], -1, 1).tolist(),
            "grip": float(
                np.clip((self.data.qpos[self.grip_qadr] - self.grip_limits[0]) / np.ptp(self.grip_limits), 0, 1)
            ),
            "grip_target": self.grip_target,
            "ground_z": GROUND_Z,
            "grounded": grounded,
            "height_mm": 0.0 if grounded else float(max(0, ee[2] - GROUND_Z) * 1000),
            "error_mm": float(np.linalg.norm(self.desired - ee) * 1000),
            "t": self.data.time,
        }
