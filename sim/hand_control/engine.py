"""A dedicated VirtualMars arm workspace; no ROS or physical-robot transport."""

import math
import time
from collections.abc import Sequence
from typing import Any

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
WRIST_LIMITS = np.array([1.2, 0.65, 0.45])


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("Expected finite control values")
    return float(value)


def _angles(wrist: object) -> np.ndarray | None:
    """Roll, pitch, yaw off the wire, or None when rotation is not being driven."""
    if wrist is None:
        return None
    if not isinstance(wrist, (list, tuple)) or len(wrist) != 3:
        raise ValueError("Expected bounded roll, pitch and yaw values")
    return np.array([_finite(v) for v in wrist])


def rotation_clearance(height: float) -> float:
    clearance = np.clip((height - GROUND_Z) / 0.06, 0, 1)
    return clearance**2 * (3 - 2 * clearance)


class ArmWorld:
    def __init__(self, workspace: dict[str, Any] | None = None) -> None:
        self.sim = VirtualMars(environment=Environment.load("void", ASSETS_DIR))
        self.model, self.data = self.sim.model, self.sim.data
        self.names = [f"joint{i}" for i in range(1, 6)]
        ids = [self.model.joint(f"robot_{name}").id for name in self.names]
        self.qadr = self.model.jnt_qposadr[ids]
        self.dadr = self.model.jnt_dofadr[ids]
        self.limits = self.model.jnt_range[ids].copy()
        self.ee = self.model.body("robot_ee_link").id
        self.shoulder = self.model.body_pos[self.model.body("robot_link1").id].copy()
        self.wrist = np.zeros(3)
        self.rotation_limited = False
        self.rotation_enabled = False
        self.absolute_pitch = workspace is not None
        self.wrist_min = -WRIST_LIMITS
        self.wrist_max = WRIST_LIMITS.copy()
        if self.absolute_pitch:
            # The taught controller uses absolute pitch. A 37-degree cap
            # prevented downward floor grasps even when the arm could reach them.
            self.wrist_max[1] = math.pi / 2
        self.demonstration = None
        self.center = np.array(workspace["center"] if workspace else CENTER, dtype=float)
        self.span = np.array(workspace["span"] if workspace else SPAN, dtype=float)
        self.base_body = self.model.body("robot_base_link").id
        a = self.model.body_pos[self.model.body("robot_link3").id][[0, 2]]
        b = self.model.body_pos[self.model.body("robot_link4").id][[0, 2]]
        self.link_lengths = (np.linalg.norm(a), np.linalg.norm(b))
        self.link_angles = (math.atan2(a[1], a[0]), math.atan2(b[1], b[0]))
        self.tool_length = self.model.body_pos[self.model.body("robot_link5").id][0] + self.model.body_pos[self.ee][0]
        self.shoulder_height = self.shoulder[2] + self.model.body_pos[self.model.body("robot_link2").id][2]
        self.ground_geom = self.model.geom("ground").id
        self.finger_geoms = {
            i
            for i in range(self.model.ngeom)
            if self.model.body(self.model.geom_bodyid[i]).name in ("robot_link61", "robot_link62")
            and (self.model.geom_contype[i] or self.model.geom_conaffinity[i])
        }
        self.ik_data = mujoco.MjData(self.model)
        self.command = np.array([0.0, 0.4, -0.3, 0.5, 0.0])
        self.desired = self.center.copy()
        if workspace:
            self.command = self.constrain(np.array(workspace["angles"], dtype=float), physical=True)
        else:
            self.desired[2] = GROUND_Z
            self.command = self.solve(self.desired, self.command)
        self.position_command = self.command.copy()
        self.pitch_target = float(sum(self.command[1:4]))
        # A fresh dedicated simulator boots in its working pose. Runtime motion
        # always goes through the existing physics servos, never a visual pose.
        self.data.qpos[self.qadr] = self.command
        self.data.qvel[:] = 0
        mujoco.mj_forward(self.model, self.data)
        gripper = self.model.joint("robot_joint6")
        self.grip_limits = self.model.jnt_range[gripper.id].copy()
        self.grip_qadr = self.model.jnt_qposadr[gripper.id]
        self.grip_target = workspace["grip"] if workspace else 1.0
        self.sim.set_joint_target("joint6", float(self.grip_limits[0] + self.grip_target * np.ptp(self.grip_limits)))
        self.active = False
        self.last_input = -math.inf
        self.reason = "ready"
        for _ in range(100):
            self.drive(self.command, 0.01, initialize=True)
            self.sim.step(0.01)
        self.hold("ready")

    def constrain(self, values: np.ndarray, *, physical: bool = False) -> np.ndarray:
        values = np.clip(values, self.limits[:, 0], self.limits[:, 1])
        values[1] = max(values[1], joint2_min_target(values[0], self.limits[1, 0]) + (0.09 if physical else 0))
        return values

    def solve(self, target: np.ndarray, seed: np.ndarray) -> np.ndarray:
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

    def wrist_candidates(self, target: np.ndarray, pitches: Sequence[float] | np.ndarray, roll: float) -> np.ndarray:
        """Exact planar IK from this URDF's link offsets, with real limits."""
        base_rotation = self.data.xmat[self.base_body].reshape(3, 3)
        local = base_rotation.T @ (target - self.data.xpos[self.base_body])
        dx, dy = local[:2] - self.shoulder[:2]
        yaw = math.atan2(dy, dx)
        radius = math.hypot(dx, dy)
        la, lb = self.link_lengths
        alpha, beta = self.link_angles
        pitches = np.asarray(pitches)
        x = radius - self.tool_length * np.cos(pitches)
        z = local[2] - self.shoulder_height + self.tool_length * np.sin(pitches)
        cosine = (x * x + z * z - la * la - lb * lb) / (2 * la * lb)
        reachable = np.abs(cosine) <= 1
        answers = []
        for sign in (-1, 1):
            elbow = sign * np.arccos(np.clip(cosine, -1, 1))
            shoulder = np.arctan2(z, x) - np.arctan2(lb * np.sin(elbow), la + lb * np.cos(elbow))
            q2 = alpha - shoulder
            q3 = beta - alpha - elbow
            q4 = pitches - q2 - q3
            q = np.column_stack((np.full(len(pitches), yaw), q2, q3, q4, np.full(len(pitches), roll)))
            valid = reachable & np.all((q >= self.limits[:, 0]) & (q <= self.limits[:, 1]), axis=1)
            valid &= q2 >= joint2_min_target(yaw, self.limits[1, 0]) + 0.09
            answers.extend(q[valid])
        return np.array(answers)

    def solve_wrist(self, target: np.ndarray, roll: float, requested: float) -> np.ndarray:
        if self.absolute_pitch:
            precise = self.wrist_candidates(target, [requested], roll)
            if len(precise):
                return min(precise, key=lambda q: np.linalg.norm(q - self.command))
        step = math.pi / 180
        candidates = self.wrist_candidates(target, np.linspace(-math.pi, math.pi, 361), roll)
        if not len(candidates):
            self.rotation_limited = True
            solution = self.position_command.copy()
            solution[4] = roll
            return solution
        pitches = candidates[:, 1:4].sum(axis=1)
        order = np.argsort(pitches)
        # Joint limits can split the reachable pitches into disconnected
        # intervals. Stay in the nearest posture's interval; averaging across
        # a gap would command an unreachable pose and flip the elbow branch.
        groups = np.split(order, np.where(np.diff(pitches[order]) > step * 1.5)[0] + 1)
        selected = min(groups, key=lambda ids: np.min(np.linalg.norm(candidates[ids] - self.command, axis=1)))
        candidates, pitches = candidates[selected], pitches[selected]
        low, high = float(min(pitches)), float(max(pitches))
        # Preserve the requested absolute tilt. Re-centering the usable range
        # every tick made translation rotate the claw even with a still hand.
        pitch = np.clip(requested, low, high)
        precise = self.wrist_candidates(target, [pitch], roll)
        if len(precise):
            solution = min(precise, key=lambda q: np.linalg.norm(q - self.command))
        else:
            solution = candidates[np.argmin(np.abs(pitches - pitch))]
        self.rotation_limited = bool(
            self.rotation_limited
            or abs(requested - sum(solution[1:4])) > 0.01
            or pitch <= low + 0.01
            or pitch >= high - 0.01
        )
        return solution

    def swivel(self, point: Sequence[float] | np.ndarray, yaw: float) -> np.ndarray:
        point = np.array(point, dtype=float, copy=True)
        x, y = point[:2] - self.shoulder[:2]
        point[:2] = self.shoulder[:2] + [math.cos(yaw) * x - math.sin(yaw) * y, math.sin(yaw) * x + math.cos(yaw) * y]
        return point

    def gravity_offset(self) -> np.ndarray:
        bias = self.data.qfrc_bias[self.dadr]
        # Invert the simulator's documented structural sag + static PD error.
        # This uses physics state, and does not alter the shared robot model.
        return bias / STRUCT_STIFFNESS + ARM_BACKLASH_RAD * np.tanh(bias / BACKLASH_TANH_NM) + bias / KP_JOINT

    def project_personal_target(self, target: np.ndarray, roll: float, pitch: float) -> np.ndarray:
        """Prefer the taught tilt when estimated reach requests an impossible pose.

        Move radial reach at most 60 mm to the nearest available solution at
        this height and yaw, inside the workspace and real joint limits.
        Without a nearby solution, regular bounded IK remains in charge.
        """
        if len(self.wrist_candidates(target, [pitch], roll)):
            return target
        direction = target[:2] - self.shoulder[:2]
        direction = direction / max(np.linalg.norm(direction), 1e-9)
        for distance in np.arange(0.0025, 0.0601, 0.0025):
            for sign in (1, -1):
                candidate = target.copy()
                candidate[:2] += direction * distance * sign
                unrotated = self.swivel(candidate, -self.wrist[2])
                if np.any(np.abs(unrotated - self.center) > self.span + 1e-6):
                    continue
                if len(self.wrist_candidates(candidate, [pitch], roll)):
                    return candidate
        return target

    def drive(self, angles: np.ndarray, dt: float, initialize: bool = False) -> None:
        target = self.constrain(angles + self.gravity_offset())
        current = np.array([self.sim.joint_targets()[name] for name in self.names])
        command = (
            target if initialize else current + np.clip(target - current, -MAX_JOINT_SPEED * dt, MAX_JOINT_SPEED * dt)
        )
        for name, value in zip(self.names, command, strict=True):
            self.sim.set_joint_target(name, float(value))

    def move(
        self,
        horizontal: object,
        vertical: object,
        reach: object = 0,
        grip: object = 1,
        now: float | None = None,
        *,
        wrist: object = None,
    ) -> None:
        """Retarget from wire values; anything but bounded finite numbers is refused untouched."""
        h, v, r, g = (_finite(value) for value in (horizontal, vertical, reach, grip))
        if max(abs(h), abs(v), abs(r)) > 1.0001 or not 0 <= g <= 1:
            raise ValueError("Control values outside workspace")
        angles = _angles(wrist)
        if angles is not None and (np.any(angles < self.wrist_min - 1e-6) or np.any(angles > self.wrist_max + 1e-6)):
            raise ValueError("Expected bounded roll, pitch and yaw values")
        rotation_enabled = angles is not None
        angles = np.zeros(3) if angles is None else angles
        if rotation_enabled:
            if self.absolute_pitch:
                self.pitch_target = float(angles[1])
            else:
                if not self.rotation_enabled:
                    self.pitch_target = float(sum(self.data.qpos[self.qadr][1:4]))
                self.pitch_target += float(angles[1] - self.wrist[1])
        self.rotation_enabled = rotation_enabled
        self.demonstration = None
        self.wrist = np.clip(angles, self.wrist_min, self.wrist_max)
        # Yaw is a real base swivel, not an independent sixth arm joint.
        # It moves the claw along an arc about the shoulder.
        self.desired = self.swivel(self.center + self.span * np.clip([r, h, v], -1, 1), self.wrist[2])
        self.grip_target = g
        self.last_input = time.monotonic() if now is None else now
        self.active = True
        self.reason = "following"

    def show_pose(self, pose: dict[str, Any], now: float | None = None) -> None:
        """Move to a server-selected study pose through the real servos."""
        self.demonstration = np.array(pose["angles"], dtype=float)
        self.desired = np.array(pose["ee"], dtype=float)
        self.grip_target = pose["grip"]
        self.last_input = time.monotonic() if now is None else now
        self.active = True
        self.reason = "pose_study"

    def hold(self, reason: str = "paused") -> None:
        self.demonstration = None
        self.active = False
        self.reason = reason
        self.command = self.data.qpos[self.qadr].copy()
        self.pitch_target = float(sum(self.command[1:4]))
        if self.absolute_pitch:
            self.wrist[1] = np.clip(self.pitch_target, self.wrist_min[1], self.wrist_max[1])
        self.desired = self.data.xpos[self.ee].copy()
        self.path_target = self.desired.copy()
        if self.rotation_enabled:
            # Reanchoring after a pause uses the achieved wrist pose, rather
            # than resuming an ahead-of-arm rotation that never finished.
            clearance = rotation_clearance(self.desired[2])
            self.wrist[0] = (
                np.clip(self.command[4] / clearance, -WRIST_LIMITS[0], WRIST_LIMITS[0]) if clearance > 0.01 else 0
            )
        # Remove the ahead-of-arm setpoint immediately; physical inertia is
        # still handled by the PD servos and damping in VirtualMars.
        self.drive(self.command, 0, initialize=True)

    def tick(self, dt: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.active and now - self.last_input > WATCHDOG_S:
            self.hold("input_timeout")
        if self.active and self.demonstration is not None:
            self.command = self.demonstration.copy()
            self.drive(self.command, dt)
            lo, hi = self.grip_limits
            target = lo + self.grip_target * (hi - lo)
            current = self.sim.joint_targets()["joint6"]
            self.sim.set_joint_target("joint6", float(current + np.clip(target - current, -2.5 * dt, 2.5 * dt)))
            self.sim.step(dt)
            return
        if self.active:
            projected = (
                self.project_personal_target(
                    self.desired, self.wrist[0] * rotation_clearance(self.desired[2]), self.pitch_target
                )
                if self.absolute_pitch
                else self.desired
            )
            reach_limited = np.linalg.norm(projected - self.desired) > 1e-6
            distance = projected - self.path_target
            # Cartesian target advances at <= 0.18 m/s. The independent joint
            # clamp handles singularities and difficult edge configurations.
            target = self.path_target + distance * min(1, 0.18 * dt / max(np.linalg.norm(distance), 1e-9))
            clearance = rotation_clearance(target[2])
            roll = self.wrist[0] * clearance
            self.path_target = target
            # Keep a separate position-only posture. Reapplying a pitch offset
            # to the already tilted solution would accumulate rotation each tick.
            self.position_command = self.solve(target, self.position_command)
            self.rotation_limited = bool(reach_limited or (clearance < 0.95 and abs(self.wrist[0]) > 0.06))
            self.command = (
                self.solve_wrist(target, roll, self.pitch_target)
                if self.rotation_enabled
                else self.position_command.copy()
            )
            # Consume blocked tilt at a joint limit, so reversing the
            # hand immediately moves away from the limit (no hidden wind-up).
            if not self.absolute_pitch:
                self.pitch_target = float(sum(self.command[1:4]))
            self.drive(self.command, dt)
            lo, hi = self.grip_limits
            target = lo + self.grip_target * (hi - lo)
            current = self.sim.joint_targets()["joint6"]
            self.sim.set_joint_target("joint6", float(current + np.clip(target - current, -2.5 * dt, 2.5 * dt)))
        self.sim.step(dt)

    def ground_contacts(self) -> list[Any]:
        return [
            c
            for c in self.data.contact
            if (c.geom1 == self.ground_geom and c.geom2 in self.finger_geoms)
            or (c.geom2 == self.ground_geom and c.geom1 in self.finger_geoms)
        ]

    def snapshot(self) -> dict[str, Any]:
        angles = {
            name: float(self.data.qpos[self.model.jnt_qposadr[self.model.joint(f"robot_{name}").id]])
            for name in (*self.names, "joint6", "joint_head")
        }
        ee = self.data.xpos[self.ee]
        grounded = bool(self.ground_contacts())
        unrotated = self.swivel(self.desired, -self.wrist[2])
        return {
            "type": "state",
            "simulated": True,
            "active": self.active,
            "reason": self.reason,
            "joints": angles,
            "pose": self.sim.pose(),
            "ee": ee.tolist(),
            "target": self.desired.tolist(),
            "offset": np.clip(((unrotated - self.center) / self.span)[[1, 2, 0]], -1, 1).tolist(),
            "grip": float(
                np.clip((self.data.qpos[self.grip_qadr] - self.grip_limits[0]) / np.ptp(self.grip_limits), 0, 1)
            ),
            "grip_target": self.grip_target,
            "wrist": self.wrist.tolist(),
            "pitch_target": self.pitch_target,
            "wrist_measured": [angles["joint5"], sum(angles[f"joint{i}"] for i in (2, 3, 4)), angles["joint1"]],
            "rotation_limited": self.rotation_limited,
            "ground_z": GROUND_Z,
            "grounded": grounded,
            "height_mm": 0.0 if grounded else float(max(0, ee[2] - GROUND_Z) * 1000),
            "error_mm": float(np.linalg.norm(self.desired - ee) * 1000),
            "t": self.data.time,
        }
