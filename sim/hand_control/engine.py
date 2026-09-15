"""A dedicated VirtualMars arm workspace; no ROS or physical-robot transport."""

import math
import time
from collections.abc import Sequence
from itertools import product
from typing import Any

import mujoco
import numpy as np
from innate_ik import get_ik
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
    def __init__(self, workspace: dict[str, Any] | None = None, *, synchronous_ik: bool = False) -> None:
        self.sim = VirtualMars(environment=Environment.load("void", ASSETS_DIR))
        self.model, self.data = self.sim.model, self.sim.data
        self.names = [f"joint{i}" for i in range(1, 6)]
        ids = [self.model.joint(f"robot_{name}").id for name in self.names]
        self.qadr = self.model.jnt_qposadr[ids]
        self.dadr = self.model.jnt_dofadr[ids]
        self.limits = self.model.jnt_range[ids].copy()
        self.ee = self.model.body("robot_ee_link").id
        self.pinch_point = bool(workspace and workspace.get("pinch_point"))
        self.direct_rotation = bool(workspace and workspace.get("direct_rotation"))
        self.innate_ik = get_ik() if self.direct_rotation else None
        self.ik_error = None
        self.synchronous_ik = synchronous_ik
        self.ik_pending = None
        self.ik_epoch = 0
        self.pads = [self.model.geom(f"robot_link6{i}_pad4").id for i in (1, 2)]
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
        if self.direct_rotation:
            # Direct angular mapping uses the roll/base joints' physical range.
            # Pose-specific IK and floor clearance still limit achieved motion.
            self.wrist_min = np.full(3, -math.pi / 2)
            self.wrist_max = np.full(3, math.pi / 2)
        self.demonstration = None
        self.center = np.array(workspace["center"] if workspace else CENTER, dtype=float)
        self.span = np.array(workspace["span"] if workspace else SPAN, dtype=float)
        self.base_body = self.model.body("robot_base_link").id
        a = self.model.body_pos[self.model.body("robot_link3").id][[0, 2]]
        b = self.model.body_pos[self.model.body("robot_link4").id][[0, 2]]
        self.link_lengths = (np.linalg.norm(a), np.linalg.norm(b))
        self.link_angles = (math.atan2(a[1], a[0]), math.atan2(b[1], b[0]))
        self.fixed_tool_length = (
            self.model.body_pos[self.model.body("robot_link5").id][0] + self.model.body_pos[self.ee][0]
        )
        self.shoulder_height = self.shoulder[2] + self.model.body_pos[self.model.body("robot_link2").id][2]
        self.ground_geom = self.model.geom("ground").id
        self.finger_geoms = {
            i
            for i in range(self.model.ngeom)
            if self.model.body(self.model.geom_bodyid[i]).name in ("robot_link61", "robot_link62")
            and (self.model.geom_contype[i] or self.model.geom_conaffinity[i])
        }
        self.box_corners = np.array(list(product((-1, 1), repeat=3)))
        self.finger_local = None
        self.ik_data = mujoco.MjData(self.model)
        self.tool_data = mujoco.MjData(self.model)
        self.predicted_tool_length = None
        self.grasp_offset = np.zeros(3)
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
        self.grip_mimic_qadr = self.model.jnt_qposadr[self.model.joint("robot_joint6M").id]
        self.grip_target = workspace["grip"] if workspace else 1.0
        self.sim.set_joint_target("joint6", float(self.grip_limits[0] + self.grip_target * np.ptp(self.grip_limits)))
        self.active = False
        self.last_input = -math.inf
        self.reason = "ready"
        for _ in range(100):
            self.drive(self.command, 0.01, initialize=True)
            self.sim.step(0.01)
        self.hold("ready")

    def grasp_point(self, data=None):
        """Control the midpoint of the two fingertip pads in pinch mode."""
        data = self.data if data is None else data
        return data.geom_xpos[self.pads].mean(axis=0) if self.pinch_point else data.xpos[self.ee].copy()

    @property
    def tool_length(self):
        if not self.pinch_point:
            return self.fixed_tool_length
        if self.predicted_tool_length is not None:
            return self.predicted_tool_length
        # Read the real pad geometry, including the current measured aperture.
        # The URDF's fixed ee link is ~12 mm beyond the open jaws' midpoint.
        forward = self.data.xmat[self.ee].reshape(3, 3)[:, 0]
        return self.fixed_tool_length + float(forward @ (self.grasp_point() - self.data.xpos[self.ee]))

    def anticipate_grasp(self):
        """Lead jaw geometry slightly to cover the arm servos' response time.

        Only the IK model is predicted; measured physics and reported contact
        points are unchanged. Joint and Cartesian speed limits still apply.
        """
        if not self.pinch_point:
            return
        d = self.tool_data
        d.qpos[:] = self.data.qpos
        target = self.grip_limits[0] + self.grip_target * np.ptp(self.grip_limits)
        measured = self.data.qpos[self.grip_qadr]
        lead = self.grip_speed * 0.12
        predicted = measured + np.clip(target - measured, -lead, lead)
        d.qpos[self.grip_qadr] = predicted
        d.qpos[self.grip_mimic_qadr] = -predicted
        mujoco.mj_forward(self.model, d)
        forward = d.xmat[self.ee].reshape(3, 3)[:, 0]
        self.grasp_offset = d.xmat[self.ee].reshape(3, 3).T @ (self.grasp_point(d) - d.xpos[self.ee])
        self.predicted_tool_length = self.fixed_tool_length + float(forward @ (self.grasp_point(d) - d.xpos[self.ee]))
        if self.direct_rotation:
            # Conservative collision envelopes, expressed around the actual
            # grasp midpoint. Boxes are exact; hub cylinders use their boxes.
            corners = []
            for gid in self.finger_geoms:
                kind = self.model.geom_type[gid]
                size = self.model.geom_size[gid].copy()
                if kind == mujoco.mjtGeom.mjGEOM_CYLINDER:
                    size = np.array([size[0], size[0], size[1]])
                elif kind != mujoco.mjtGeom.mjGEOM_BOX:
                    size = np.full(3, self.model.geom_rbound[gid])
                rotation = d.geom_xmat[gid].reshape(3, 3)
                corners.append((self.box_corners * size) @ rotation.T + d.geom_xpos[gid])
            self.finger_local = (np.concatenate(corners) - self.grasp_point(d)) @ d.xmat[self.ee].reshape(3, 3)

    def safe_roll(self, requested, pitch, height):
        if not self.direct_rotation:
            return requested * rotation_clearance(height)
        if self.finger_local is None:
            return requested
        x, y, z = self.finger_local.T

        def clearance(roll):
            return height + np.min(-math.sin(pitch) * x + math.cos(pitch) * (math.sin(roll) * y + math.cos(roll) * z))

        # Preserve unit gain whenever the full sweep clears the floor. Stop
        # at the first collision boundary instead of scaling every small roll.
        low = 0.0
        for high in np.linspace(0, requested, max(2, math.ceil(abs(requested) / math.radians(5)) + 1)):
            if clearance(high) < 0.001:
                for _ in range(12):
                    middle = (low + high) / 2
                    if clearance(middle) >= 0.001:
                        low = middle
                    else:
                        high = middle
                return low
            low = high
        return requested

    def solve_innate(self, point):
        """A fixed grasp point and orientation go to Innate's actual KDL IK.

        The worker compensates the pad offset using achieved FK. The live
        simulation never waits for it; a paused lease invalidates old results.
        """
        answer = None
        if self.ik_pending:
            future, epoch, started, clipped = self.ik_pending
            if future.done():
                self.ik_pending = None
                result = future.result()
                if epoch == self.ik_epoch and time.monotonic() - started < 0.3:
                    answer = result
                    self.rotation_limited = clipped
            elif time.monotonic() - started > 0.3:
                raise RuntimeError("Innate IK is taking too long; arm held")
        if self.ik_pending is None:
            requested = self.wrist.copy()
            requested[0] = self.safe_roll(requested[0], requested[1], point[2])
            clipped = bool(abs(requested[0] - self.wrist[0]) > 0.001)
            base_rotation = self.data.xmat[self.base_body].reshape(3, 3)
            point_local = base_rotation.T @ (point - self.data.xpos[self.base_body])
            args = (point_local.tolist(), requested.tolist(), self.command.tolist(), self.grasp_offset.tolist())
            if self.synchronous_ik:
                answer = self.innate_ik.solve(*args)
                self.rotation_limited = clipped
            else:
                self.ik_pending = (self.innate_ik.submit(*args), self.ik_epoch, time.monotonic(), clipped)
        if answer is None:
            return self.command.copy()
        self.ik_error = None
        if answer.get("solution") is None:
            self.rotation_limited = True
            return self.command.copy()
        candidate = np.array(answer["solution"])
        error = answer["grasp_error"]
        self.rotation_limited = bool(self.rotation_limited or error > 0.002 or answer["rotation_error"] > 0.04)
        if error > 0.004 or np.max(np.abs(candidate - self.constrain(candidate.copy(), physical=True))) > 1e-4:
            self.rotation_limited = True
            return self.command.copy()
        return candidate

    @property
    def grip_speed(self):
        # Near the floor, keeping the contact point fixed needs more shoulder
        # travel per millimetre. Let the arm keep up with the opening jaws.
        if self.pinch_point:
            return 1.0 + 1.5 * rotation_clearance(self.grasp_point()[2])
        return 2.5

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
            point = self.grasp_point(d)
            error = target - point
            if np.linalg.norm(error) < 0.0008:
                break
            mujoco.mj_jac(self.model, d, jac, None, point, self.ee)
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
        self.desired = self.center + self.span * np.clip([r, h, v], -1, 1)
        if not self.innate_ik:
            self.desired = self.swivel(self.desired, self.wrist[2])
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
        self.ik_epoch += 1
        self.demonstration = None
        self.active = False
        self.predicted_tool_length = None
        self.reason = reason
        self.command = self.data.qpos[self.qadr].copy()
        self.pitch_target = float(sum(self.command[1:4]))
        if self.absolute_pitch:
            self.wrist[1] = np.clip(self.pitch_target, self.wrist_min[1], self.wrist_max[1])
        if self.direct_rotation:
            # The next rigid alignment starts from the achieved heading,
            # including sideways translation, never a still-pending swivel.
            self.wrist[2] = np.clip(self.command[0], self.wrist_min[2], self.wrist_max[2])
        self.desired = self.grasp_point()
        self.path_target = self.desired.copy()
        if self.rotation_enabled:
            # Reanchoring after a pause uses the achieved wrist pose, rather
            # than resuming an ahead-of-arm rotation that never finished.
            clearance = 1.0 if self.direct_rotation else rotation_clearance(self.desired[2])
            self.wrist[0] = (
                np.clip(self.command[4] / clearance, self.wrist_min[0], self.wrist_max[0]) if clearance > 0.01 else 0
            )
        # Remove the ahead-of-arm setpoint immediately; physical inertia is
        # still handled by the PD servos and damping in VirtualMars.
        self.drive(self.command, 0, initialize=True)
        if self.pinch_point:
            # A stopped grasp must not keep closing toward an old setpoint.
            measured = float(np.clip(self.data.qpos[self.grip_qadr], *self.grip_limits))
            self.grip_target = float((measured - self.grip_limits[0]) / np.ptp(self.grip_limits))
            self.sim.set_joint_target("joint6", measured)

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
            self.sim.set_joint_target(
                "joint6", float(current + np.clip(target - current, -self.grip_speed * dt, self.grip_speed * dt))
            )
            self.sim.step(dt)
            return
        if self.active:
            self.anticipate_grasp()
            if self.innate_ik:
                distance = self.desired - self.path_target
                self.path_target += distance * min(1, 0.18 * dt / max(np.linalg.norm(distance), 1e-9))
                try:
                    self.command = self.solve_innate(self.path_target)
                except (RuntimeError, OSError) as error:
                    self.ik_error = str(error)
                    self.hold("ik_unavailable")
                    self.sim.step(dt)
                    return
                self.drive(self.command, dt)
                lo, hi = self.grip_limits
                target = lo + self.grip_target * (hi - lo)
                current = self.sim.joint_targets()["joint6"]
                self.sim.set_joint_target(
                    "joint6", float(current + np.clip(target - current, -self.grip_speed * dt, self.grip_speed * dt))
                )
                self.sim.step(dt)
                return
            projected = (
                self.project_personal_target(
                    self.desired, self.safe_roll(self.wrist[0], self.pitch_target, self.desired[2]), self.pitch_target
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
            roll = self.safe_roll(self.wrist[0], self.pitch_target, target[2])
            self.path_target = target
            # Keep a separate position-only posture. Reapplying a pitch offset
            # to the already tilted solution would accumulate rotation each tick.
            self.position_command = self.solve(target, self.position_command)
            roll_limited = (
                abs(roll - self.wrist[0]) > 0.001
                if self.direct_rotation
                else (clearance < 0.95 and abs(self.wrist[0]) > 0.06)
            )
            self.rotation_limited = bool(reach_limited or roll_limited)
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
            self.sim.set_joint_target(
                "joint6", float(current + np.clip(target - current, -self.grip_speed * dt, self.grip_speed * dt))
            )
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
        ee = self.grasp_point()
        grounded = bool(self.ground_contacts())
        unrotated = self.desired if self.innate_ik else self.swivel(self.desired, -self.wrist[2])
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
            "ik_solver": "innate_kdl" if self.innate_ik else "legacy_study",
            "ik_error": self.ik_error,
            "ground_z": GROUND_Z,
            "grounded": grounded,
            "height_mm": 0.0 if grounded else float(max(0, ee[2] - GROUND_Z) * 1000),
            "error_mm": float(np.linalg.norm(self.desired - ee) * 1000),
            "t": self.data.time,
        }
