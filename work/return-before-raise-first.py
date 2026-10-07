"""Level pullback to the left-side rest column, followed by a fold in that plane."""
from pathlib import Path

from innate.exceptions import SkillFailed


def return_plan(joints, rest):
    import numpy as np
    import PyKDL as kdl
    from ament_index_python.packages import get_package_share_directory
    from scipy.optimize import least_squares
    from urdf_parser_py.urdf import URDF

    from mars_arm.urdf import treeFromUrdfModel

    model = URDF.from_xml_file(str(Path(get_package_share_directory('mars_description')) / 'urdf/mars.urdf'))
    _, tree = treeFromUrdfModel(model)
    chain = tree.getChain('base_link', 'ee_link')
    names = [chain.getSegment(i).getJoint().getName() for i in range(chain.getNrOfSegments())
             if chain.getSegment(i).getJoint().getType() != kdl.Joint.Fixed]
    lower = np.array([model.joint_map[n].limit.lower for n in names])
    upper = np.array([model.joint_map[n].limit.upper for n in names])
    fk = kdl.ChainFkSolverPos_recursive(chain)

    def position(q):
        values = kdl.JntArray(5)
        for i, v in enumerate(q):
            values[i] = float(v)
        f = kdl.Frame()
        fk.JntToCart(values, f)
        return np.array([f.p[i] for i in range(3)])

    previous = np.clip(joints[:5], lower, upper)
    wrist = float(previous[4])
    rest = [*rest[:4], wrist]
    start = position(previous)
    goal = position(rest[:5])
    goal[2] = start[2]
    plan = []
    for i in range(1, 4):
        target = start + (goal - start) * i / 3
        seed = previous.copy()
        result = least_squares(
            lambda q, target=target, seed=seed: np.r_[position(np.r_[q, wrist]) - target, .00001 * (q - seed[:4])],
            np.clip(seed[:4], lower[:4] + 1e-8, upper[:4] - 1e-8), bounds=(lower[:4], upper[:4]),
            max_nfev=200, ftol=1e-10, xtol=1e-10, gtol=1e-10,
        )
        q = np.r_[result.x, wrist]
        if np.linalg.norm(position(q) - target) > .003:
            raise SkillFailed('Cannot return to the left rest column at this height')
        plan.append(q.tolist())
        previous = q
    # Rest yaw defines a vertical plane: keeping it fixed avoids the old forward sweep.
    plan[-1][0] = rest[0]
    for a, b in zip([list(joints[:5]), *plan], [*plan, list(rest[:5])], strict=True):
        for t in np.linspace(0, 1, 21):
            point = position(np.array(a) + (np.array(b) - a) * t)
            if b != list(rest[:5]) and point[2] < start[2] - .006:
                raise SkillFailed('Return trajectory would lower before the rest column')
            if b == list(rest[:5]) and point[0] > goal[0] + .006:
                raise SkillFailed('Rest fold would swing forward')
    return plan, start.tolist(), goal.tolist()


def retract_to_rest(host, *, block=True):
    states = host.joint_states
    if states is None or len(states.position) < 5:
        raise SkillFailed('No joint state for level return')
    rest = list(host.manipulation.REST)
    current = list(states.position[:5])
    plan, start, goal = return_plan(current, rest)
    host.logger.info(f'[DropReturn] level back-left {start} -> {goal}; then lower in rest plane')
    grip = host.manipulation._grip_or(None)
    durations = []
    previous = current
    for q in plan:
        durations.append(max(.05, max(abs(a-b) for a,b in zip(q, previous, strict=True)) / 2.0))
        previous = q
    # One command completes both legs; no stale FK sample can veto lowering.
    plan.append(rest[:5])
    durations.append(max(.8, max(abs(a-b) for a,b in zip(rest[:5], previous, strict=True)) / 2.0))
    goals = [q + [grip] for q in plan[:-1]] + [rest]
    if not host.manipulation._send_trajectory(goals, durations, wait=block):
        raise SkillFailed('Arm return to rest failed')
    host.logger.info(f'[DropReturn] exact REST commanded; wrist reset and claw closed during lowering; blocking={block}')
