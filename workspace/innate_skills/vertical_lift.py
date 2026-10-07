# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Preflight a vertical lift and optional sideways move at clearance height."""

import math
import time
from pathlib import Path

from innate.exceptions import SkillFailed


def vertical_plan(joints, height, side_y=None, carry_joints=None):
    # Load the same measured robot chain and limits as the robot's IK node.
    import numpy as np
    import PyKDL as kdl
    from ament_index_python.packages import get_package_share_directory
    from scipy.optimize import least_squares, minimize
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

    if carry_joints is not None and height is None:
        goal = np.asarray(carry_joints, dtype=float)
        if goal.shape != (len(names),) or not np.isfinite(goal).all() or np.any(goal < lower) or np.any(goal > upper):
            raise SkillFailed("Invalid fixed carry posture")
        start = frame(joints)
        origin = (start.p.x(), start.p.y(), start.p.z())
        if max(abs(a - b) for a, b in zip(joints, goal, strict=True)) <= 0.05:
            return [], origin
        crossing = abs(joints[0] - goal[0]) > math.pi / 2
        waypoints = []
        if crossing:

            def upright(q):
                return [joints[0], *q, joints[4]]

            fits = [
                minimize(
                    lambda q: -frame(upright(q)).p.z(),
                    seed,
                    bounds=list(zip(lower[1:4], upper[1:4], strict=True)),
                    method="L-BFGS-B",
                )
                for seed in (joints[1:4], [0, -1.3, 0])
            ]
            best = min(fits, key=lambda result: result.fun)
            if not best.success:
                raise SkillFailed("Cannot establish fully upright arm posture")
            raised = upright(best.x)
            turned = list(raised)
            turned[0] = float(goal[0])
            waypoints.extend((raised, turned))
        waypoints.append(list(goal))
        plan = [(list(target), frame(target).p.z()) for target in waypoints]
        return plan, origin
    start = frame(joints)
    x, y, z = start.p.x(), start.p.y(), start.p.z()
    if not math.isfinite(height) or (side_y is not None and not math.isfinite(side_y)):
        raise SkillFailed("Invalid arm clearance target")
    clearance_z = max(z, height)
    targets = []
    if z < height:
        steps = math.ceil((height - z) / 0.005)
        targets.extend((x, y, z + (height - z) * step / steps) for step in range(1, steps + 1))
    if side_y is not None and abs(side_y - y) > 0.003:
        steps = math.ceil(abs(side_y - y) / 0.005)
        targets.extend((x, y + (side_y - y) * step / steps, clearance_z) for step in range(1, steps + 1))
    previous = list(joints)
    plan = []
    previous_target = (x, y, z)
    for target_x, target_y, target_z in targets:
        seed = np.clip(previous, lower + 1e-7, upper - 1e-7)

        def residual(q, target_x=target_x, target_y=target_y, target_z=target_z, seed=seed):
            f = frame(q)
            # Tiny posture regularization chooses the nearby solution without
            # demanding a fixed wrist orientation from a five-joint arm.
            return np.concatenate(([f.p.x() - target_x, f.p.y() - target_y, f.p.z() - target_z], 0.0001 * (q - seed)))

        result = least_squares(residual, seed, bounds=(lower, upper), max_nfev=200, ftol=1e-10, xtol=1e-10, gtol=1e-10)
        values = result.x.tolist()
        reached = frame(values)
        if math.dist((reached.p.x(), reached.p.y(), reached.p.z()), (target_x, target_y, target_z)) > 0.001:
            raise SkillFailed(f"No clearance path within joint limits at height {target_z:.3f} m")
        if max(abs(a - b) for a, b in zip(values, previous, strict=True)) > 0.15:
            raise SkillFailed("Vertical lift would switch arm posture; arm held in place")
        # Sample between dense joint targets as well as Cartesian endpoints.
        # Execution blends these targets into a single continuous trajectory.
        last_z = frame(previous).p.z()
        for sample in range(11):
            f = frame([a + (b - a) * sample / 10 for a, b in zip(previous, values, strict=True)])
            expected = tuple(
                a + (b - a) * sample / 10 for a, b in zip(previous_target, (target_x, target_y, target_z), strict=True)
            )
            if (
                abs(f.p.x() - x) > 0.003
                or math.dist((f.p.x(), f.p.y(), f.p.z()), expected) > 0.003
                or f.p.z() < last_z - 0.002
            ):
                raise SkillFailed("Arm clearance path would leave its checked corridor; arm held in place")
            last_z = f.p.z()
        plan.append((values, target_z))
        previous = values
        previous_target = (target_x, target_y, target_z)
    if carry_joints is not None:
        # Execute a single lift waypoint; dense IK points were only a preflight.
        # Check the actual joint interpolation that the controller will run.
        lift_goal = previous
        last_z = z
        for i in range(101):
            q = [a + (b - a) * i / 100 for a, b in zip(joints, lift_goal, strict=True)]
            f = frame(q)
            if f.p.x() > x + 0.003 or f.p.z() < last_z - 0.002:
                raise SkillFailed("Direct lift would sweep forward or descend")
            last_z = f.p.z()
        plan = [(list(lift_goal), frame(lift_goal).p.z())] if plan else []
        goal = np.asarray(carry_joints, dtype=float)
        if goal.shape != (len(names),) or not np.isfinite(goal).all() or np.any(goal < lower) or np.any(goal > upper):
            raise SkillFailed("Invalid fixed carry posture")
        target = frame(goal)
        minimum_z = min(clearance_z, target.p.z()) - 0.003
        maximum_x = max(x, target.p.x()) + 0.003
        # Keep yaw fixed while tucking the raised arm, then rotate at the
        # smallest reachable radius along the transition to the carry posture.
        candidates = []
        for i in range(101):
            q = [a + (b - a) * i / 100 for a, b in zip(previous, goal, strict=True)]
            q[0] = previous[0]
            f = frame(q)
            if f.p.z() >= minimum_z:
                candidates.append((abs(f.p.y() + 0.05285), q))
        if not candidates:
            raise SkillFailed("No raised posture for carry turn")
        tucked = min(candidates, key=lambda item: item[0])[1]
        turned = list(tucked)
        turned[0] = float(goal[0])
        for segment_goal in (tucked, turned, goal):
            transition_start = previous
            count = max(1, math.ceil(max(abs(a - b) for a, b in zip(previous, segment_goal, strict=True)) / 0.01))
            for step in range(1, count + 1):
                q = [a + (b - a) * step / count for a, b in zip(transition_start, segment_goal, strict=True)]
                f = frame(q)
                if (
                    f.p.x() > max(maximum_x, 0.24)
                    or f.p.z() < minimum_z
                    or (f.p.x() > maximum_x and f.p.z() < minimum_z)
                ):
                    raise SkillFailed(f"Fixed carry transition leaves clearance: x={f.p.x():.3f} z={f.p.z():.3f}")
            if max(abs(a - b) for a, b in zip(transition_start, segment_goal, strict=True)) > 1e-5:
                plan.append((list(segment_goal), frame(segment_goal).p.z()))
            previous = list(segment_goal)
        # Preserve the exact rehearsed destination, including wrist pitch/roll.
        if plan:
            plan[-1] = (list(carry_joints), target.p.z())
    return plan, (x, y, z)


def lift_vertical(host, height=0.32, side_y=None, carry_joints=None):
    """No base motion or sweeping fallback; keep grip and execute the preflighted path."""
    host.mobility.stop()
    host.check_cancelled()
    js = host.joint_states
    if js is None or len(js.position) < 5:
        raise SkillFailed("No arm state for vertical lift")
    if carry_joints is not None and all(
        math.isfinite(a) and abs(a - b) <= 0.05 for a, b in zip(js.position[:5], carry_joints, strict=True)
    ):
        host.logger.info("[VerticalLift] already in fixed carry pose; skipping transition")
        return
    plan, (x, y, start_z) = vertical_plan(list(js.position[:5]), height, side_y=side_y, carry_joints=carry_joints)
    host.logger.info(
        f"[VerticalLift] {len(plan)} checked steps: vertical x={x:.3f}, y={y:.3f}, "
        f"z={start_z:.3f}->{max([start_z] + [z for _, z in plan]):.3f}, then sideways y={side_y}, fixed carry={carry_joints is not None}"
    )
    if not plan:
        return
    grip = host.manipulation._grip_or(None)
    previous = list(js.position[:5])
    previous_z = start_z
    waypoints, durations = [], []
    for joints, z in plan:
        # Time actual vertical/joint movement; sampling density must not add dwell time.
        durations.append(
            max(
                0.001,
                abs(z - previous_z) / 0.12,
                max(abs(a - b) for a, b in zip(joints, previous, strict=True)) / 1.2,
            )
        )
        waypoints.append(joints + [grip])
        previous, previous_z = joints, z
    if carry_joints is not None:
        # One second across a handful of key poses, never a per-sample dwell.
        total = sum(durations)
        durations = [d / total for d in durations]
        previous = list(js.position[:5])
        for i, waypoint in enumerate(waypoints):
            # Respect the URDF 2 rad/s joint limit even for the one-second target.
            durations[i] = max(durations[i], max(abs(a - b) for a, b in zip(previous, waypoint[:5], strict=True)) / 2.0)
            previous = waypoint[:5]
    host.check_cancelled()
    started = time.monotonic()
    if not host.manipulation._send_trajectory(waypoints, durations):
        raise SkillFailed("Vertical lift trajectory failed")
    host.check_cancelled()
    host.logger.info(
        f"[VerticalLift] continuous motion: {time.monotonic() - started:.2f}s, planned {sum(durations):.2f}s"
    )
