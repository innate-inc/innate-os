# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The action vocabulary a demonstration-conditioned run may command.

Pure validation: no ROS, no model, no skill state. The caps are defined once
here so the policy's JSON schema, the host's checks and the prompt cannot drift
apart — a limit the model is told differs from the one enforced reads to it as
an arbitrary rejection.
"""

import math

import numpy as np

MAX_JOINT_STEP = 0.15  # rad per joint_step
MAX_BASE_STEP = 0.03  # m per base_step
MAX_EE_STEP = 0.04  # m of translation per cartesian target
MAX_EE_ROTATION = 0.2  # rad per rotation component
MAX_BATCH_TRAVEL = 2 * MAX_EE_STEP

# How far a base step may stray from the line it was asked for, as a floor plus
# a share of the distance: odometry noise dominates a tiny step, real drift a
# long one.
BASE_YAW_PER_M, BASE_YAW_FLOOR = 1.5, 0.02
BASE_LATERAL_PER_M, BASE_LATERAL_FLOOR = 0.5, 0.005

ACTIONS = {
    "joint_step": 2,
    "ee_absolute": 6,
    "ee_delta": 6,
    "base_step": 1,
    "open_gripper": 0,
    "close_gripper": 0,
    "observe": 0,
    "retry": 0,
    "done": 0,
    "stop": 0,
}
# No object vocabulary anywhere the model can see it: naming the items would
# tell the untold variant the task, which is the one thing this pair measures.

# Which motion vocabulary a run offers is selectable; the rest is always present,
# since a task cannot be carried out without grasping, looking or finishing.
# Below this, a horizontal or vertical component is incidental rather than intended.
LEVEL_TOLERANCE = 0.005  # m

MOTION_ACTIONS = ("joint_step", "ee_absolute", "ee_delta", "base_step")
FIXED_ACTIONS = ("open_gripper", "close_gripper", "observe", "retry", "done", "stop")
ACTION_HELP = {
    "joint_step": """  joint_step    pose [joint index 1-5, delta radians], at most {max_joint_step} rad. Joint1 sweeps sideways,
                joints2-4 shape reach and height, joint5 rolls the wrist.""",
    "ee_absolute": """  ee_absolute   pose [x,y,z,roll,pitch,yaw] ABSOLUTE, within {max_ee_step_cm} cm and {max_ee_rotation} rad of the measured
                pose. Full-pose IK rejects what the five-joint arm cannot reach; that is feedback.
                The last three components are the wrist angle: copy the measured ones to hold the
                aim you have, change them to re-aim.""",
    "ee_delta": """  ee_delta      pose [dx,dy,dz,droll,dpitch,dyaw] RELATIVE to the measured pose, at most {max_ee_step_cm} cm of
                translation and {max_ee_rotation} rad per rotation. The same IK and the same rejections as move.
                droll/dpitch/dyaw re-aim the wrist and are the only way to change its angle; zeros
                there carry the angle you already have into the next pose.""",
    "base_step": """  base_step     pose [metres], at most {max_base_step}, positive forward. Moves the whole arm; it does not
                retract it. Keep whatever you hold clear of contact while repositioning. This is
                how you close distance — see the paragraph on reach below.""",
    "open_gripper": "  open_gripper  pose []. Releases whatever you are holding. It falls where it is.",
    "close_gripper": """  close_gripper pose []. Grasps whatever is between the fingers. Closing does not prove
                acquisition: inspect aperture and new images before lifting.""",
    "observe": "  observe       pose []. A new look with no motion.",
    "retry": "  retry         pose []. Planning only: state the observed failure and how your approach changes.",
    "done": "  done          pose []. Only when the task is visibly complete.",
    "stop": """  stop          pose []. A last resort. Only when the scene itself makes the task impossible —
                the object is gone, a person is in the path, the hardware is dead. Never for a
                setback you could work around.""",
}


def joint_target(decision, current):
    step = decision.get("joint_step", decision.get("pose"))
    if (
        not isinstance(step, list)
        or len(step) != 2
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in step)
        or step[0] not in (1, 2, 3, 4, 5)
        or not 0 < abs(step[1]) <= MAX_JOINT_STEP
    ):
        raise ValueError(f"joint_step needs joint 1-5 and a nonzero delta within {MAX_JOINT_STEP:g} rad")
    if current.get("joint_names") != [f"joint{i}" for i in range(1, 7)]:
        raise ValueError("Expected ordered measured arm joints")
    target = list(current["qpos"])
    if len(target) != 6 or not all(math.isfinite(v) for v in target):
        raise ValueError("Invalid measured arm joints")
    target[int(step[0]) - 1] += step[1]
    return target


def event_frames(events, rows, spread=8, cap=15):
    """The gripper transitions with a frame either side of each. In a "both" run
    the survey already supplies even coverage, so this second look is only worth
    a call if it is sharper than the survey rather than a near-copy of it."""
    picked = sorted({min(max(i + d, 0), rows - 1) for i in events for d in (-spread, 0, spread)})
    if len(picked) <= cap:
        return picked
    keep = np.linspace(0, len(picked) - 1, cap, dtype=int)
    return [picked[i] for i in keep]


def parse_actions(**toggles):
    """The motion vocabulary for one run, from one flag per motion action."""
    names = tuple(name for name in MOTION_ACTIONS if toggles.get(name))
    if not names:
        raise ValueError(f"Switch on at least one of {', '.join(MOTION_ACTIONS)}")
    return names + FIXED_ACTIONS


def stuck_hint(failures):
    """Said to the model once a run of proposals has failed in a row: the point is
    that repeating the idea is what is failing, not that the run is nearly over."""
    if failures < 3:
        return ""
    return (
        f" — {failures} proposals in a row have failed; repeating this one will fail again. Change the"
        " joint, the direction or the distance, close the gap with base_step, or take a fresh look."
    )


def check_decision(value, allowed):
    """Reject a malformed or oversized decision; never silently clamp a target."""
    if not isinstance(value, dict) or set(value) != {"action", "pose", "reason"}:
        raise ValueError("Decision requires exactly action, pose, reason")
    action, pose, reason = value["action"], value["pose"], value["reason"]
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("Unknown action")
    if action not in allowed:
        raise ValueError(f"{action} is not available on this run; use {', '.join(allowed)}")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 240:
        raise ValueError("Decision needs a brief reason")
    if (
        not isinstance(pose, list)
        or len(pose) != ACTIONS[action]
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in pose)
    ):
        raise ValueError("Invalid pose for action")
    if action == "joint_step" and (pose[0] not in (1, 2, 3, 4, 5) or not 0 < abs(pose[1]) <= MAX_JOINT_STEP):
        raise ValueError(f"joint_step needs joint 1-5 and a nonzero delta within {MAX_JOINT_STEP:g} rad")
    if action == "base_step" and not 0 < abs(pose[0]) <= MAX_BASE_STEP:
        raise ValueError(f"base_step requires a nonzero distance within {MAX_BASE_STEP:g} m")
    if action == "ee_delta":
        if math.dist(pose[:3], (0, 0, 0)) > MAX_EE_STEP + 1e-9:
            raise ValueError(f"ee_delta translation exceeds {MAX_EE_STEP:g} m")
        if any(abs(v) > MAX_EE_ROTATION for v in pose[3:]):
            raise ValueError(f"ee_delta rotation exceeds {MAX_EE_ROTATION:g} radians per component")
    return dict(value)


def ee_target(decision, current):
    """The absolute pose a move or ee_delta asks for. Roll/pitch/yaw compose by
    addition, which is exact for a delta about one axis and an approximation when
    two are asked for at once."""
    if decision["action"] == "ee_absolute":
        return list(decision["pose"])
    base, delta = current["pose"], decision["pose"]
    return [base[i] + delta[i] for i in range(3)] + [
        math.atan2(math.sin(base[i] + delta[i]), math.cos(base[i] + delta[i])) for i in range(3, 6)
    ]


def check_reachable_move(pose, current):
    """Workspace envelope and per-observation travel cap; not collision checking."""
    x, y, z = pose[:3]
    if not (-0.1 <= x <= 0.45 and abs(y) <= 0.35 and 0.025 <= z <= 0.5 and math.hypot(x, y) <= 0.45):
        raise ValueError("Target outside the manipulation workspace")
    if math.dist(pose[:3], current["pose"][:3]) > MAX_EE_STEP + 1e-9:
        raise ValueError(f"Movement exceeds {MAX_EE_STEP:g} m per observation")
    if any(
        abs(math.atan2(math.sin(a - b), math.cos(a - b))) > MAX_EE_ROTATION
        for a, b in zip(pose[3:], current["pose"][3:], strict=True)
    ):
        raise ValueError(f"Orientation change exceeds {MAX_EE_ROTATION:g} radians")
    # Sliding sideways while descending drags the fingers across the object and
    # knocks it over before they close. Centre at height, then drop straight in.
    drop = current["pose"][2] - z
    sideways = math.dist(pose[:2], current["pose"][:2])
    if drop > LEVEL_TOLERANCE and sideways > LEVEL_TOLERANCE:
        raise ValueError(
            f"A descent must be vertical: this drops {drop:.3f} m while moving {sideways:.3f} m "
            "sideways. Centre over the target at clearance height in one move, then descend straight down"
        )


def next_holding(action, holding):
    """The grip latch. It cannot name what is held without leaking the task, so
    it enforces only the ordering: no grasp with a full gripper, no release with
    an empty one, and no finishing while still carrying something."""
    if action == "close_gripper":
        if holding:
            raise ValueError("Already holding something; release it before grasping again")
        return True
    if action == "open_gripper":
        if not holding:
            raise ValueError("Nothing is held; open_gripper would do nothing")
        return False
    if action == "done" and holding:
        raise ValueError("The task cannot be complete while the gripper still holds something")
    return holding


def with_limits(text):
    """Fill a prompt's cap tokens. Prose names the token, never the number, so
    the figure the model reads cannot drift from the one the runner enforces."""
    from innate import Manipulation

    return (
        text.replace("{max_joint_step}", f"{MAX_JOINT_STEP:g}")
        .replace("{max_base_step}", f"{MAX_BASE_STEP:g}")
        .replace("{max_ee_step_cm}", f"{MAX_EE_STEP * 100:g}")
        .replace("{max_ee_rotation}", f"{MAX_EE_ROTATION:g}")
        .replace("{joint2_floor}", f"{Manipulation.JOINT2_FLOOR:g}")
    )
