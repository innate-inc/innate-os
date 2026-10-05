# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Preflight a gripper lift, including the joint interpolation between targets."""

import math
import time
from pathlib import Path

from innate.exceptions import SkillFailed


def vertical_plan(joints, height):
    # Load the same measured robot chain and limits as the robot's IK node.
    import numpy as np
    import PyKDL as kdl
    from ament_index_python.packages import get_package_share_directory
    from scipy.optimize import least_squares
    from urdf_parser_py.urdf import URDF

    from mars_arm.urdf import treeFromUrdfModel

    model = URDF.from_xml_file(str(Path(get_package_share_directory("mars_description")) / "urdf/mars.urdf"))
    ok, tree = treeFromUrdfModel(model)
    if not ok:
        raise SkillFailed("Cannot load arm model for vertical lift")
    chain = tree.getChain("base_link", "ee_link")
    names = [
        chain.getSegment(i).getJoint().getName()
        for i in range(chain.getNrOfSegments())
        if chain.getSegment(i).getJoint().getType() != kdl.Joint.Fixed
    ]
    if len(joints) != len(names) or not all(math.isfinite(v) for v in joints):
        raise SkillFailed("Invalid arm state for vertical lift")
    fk = kdl.ChainFkSolverPos_recursive(chain)
    # Search inside the limits instead of rejecting an unconstrained IK
    # solution even when a different, feasible posture exists.
    lower = np.array([model.joint_map[n].limit.lower for n in names])
    upper = np.array([model.joint_map[n].limit.upper for n in names])

    def array(values):
        q = kdl.JntArray(len(values))
        for i, v in enumerate(values):
            q[i] = v
        return q

    def frame(values):
        out = kdl.Frame()
        if fk.JntToCart(array(values), out) < 0:
            raise SkillFailed("Cannot validate vertical lift kinematics")
        return out

    start = frame(joints)
    x, y, z = start.p.x(), start.p.y(), start.p.z()
    if z >= height:
        return [], (x, y, z)
    steps = math.ceil((height - z) / 0.005)
    previous = list(joints)
    plan = []
    for step in range(1, steps + 1):
        target_z = z + (height - z) * step / steps
        seed = np.clip(previous, lower + 1e-7, upper - 1e-7)

        def residual(q, target_z=target_z, seed=seed):
            f = frame(q)
            # Tiny posture regularization chooses the nearby solution without
            # demanding a fixed wrist orientation from a five-joint arm.
            return np.concatenate(([f.p.x() - x, f.p.y() - y, f.p.z() - target_z], 0.0001 * (q - seed)))

        result = least_squares(residual, seed, bounds=(lower, upper), max_nfev=200, ftol=1e-10, xtol=1e-10, gtol=1e-10)
        values = result.x.tolist()
        reached = frame(values)
        if math.dist((reached.p.x(), reached.p.y(), reached.p.z()), (x, y, target_z)) > 0.001:
            raise SkillFailed(f"No vertical lift within joint limits at height {target_z:.3f} m")
        if max(abs(a - b) for a, b in zip(values, previous, strict=True)) > 0.15:
            raise SkillFailed("Vertical lift would switch arm posture; arm held in place")
        # Sample between dense joint targets as well as Cartesian endpoints.
        # Execution blends these targets into a single continuous trajectory.
        last_z = frame(previous).p.z()
        for sample in range(11):
            f = frame([a + (b - a) * sample / 10 for a, b in zip(previous, values, strict=True)])
            if math.hypot(f.p.x() - x, f.p.y() - y) > 0.003 or f.p.z() < last_z - 0.002:
                raise SkillFailed("Vertical lift would swing forward or sideways; arm held in place")
            last_z = f.p.z()
        plan.append((values, target_z))
        previous = values
    return plan, (x, y, z)


def lift_vertical(host, height=0.32):
    """No base motion or sweeping fallback; keep grip and execute the preflighted path."""
    host.mobility.stop()
    host.check_cancelled()
    js = host.joint_states
    if js is None or len(js.position) < 5:
        raise SkillFailed("No arm state for vertical lift")
    plan, (x, y, start_z) = vertical_plan(list(js.position[:5]), height)
    host.logger.info(f"[VerticalLift] {len(plan)} checked steps at x={x:.3f}, y={y:.3f}, z={start_z:.3f}->{height:.3f}")
    if not plan:
        return
    grip = host.manipulation._grip_or(None)
    previous = list(js.position[:5])
    previous_z = start_z
    waypoints, durations = [], []
    for joints, z in plan:
        # At most 12 cm/s vertically and 1.2 rad/s between joint targets.
        durations.append(
            max(0.03, abs(z - previous_z) / 0.12, max(abs(a - b) for a, b in zip(joints, previous, strict=True)) / 1.2)
        )
        waypoints.append(joints + [grip])
        previous, previous_z = joints, z
    host.check_cancelled()
    started = time.monotonic()
    if not host.manipulation._send_trajectory(waypoints, durations):
        raise SkillFailed("Vertical lift trajectory failed")
    host.check_cancelled()
    host.logger.info(
        f"[VerticalLift] continuous motion: {time.monotonic() - started:.2f}s, planned {sum(durations):.2f}s"
    )
