# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure pose math — no ROS, no I/O.

These functions implement the local-navigation motion compensation: when the robot
moves between capturing an image and executing the navigation command the agent
returned, the target (expressed in the robot frame at capture time) must be
re-expressed in the robot's current frame.

A pose is a plain ``(x, y, theta)`` tuple in metres / radians. Kept import-clean so
it can be unit-tested without a ROS runtime.
"""

from __future__ import annotations

import math

Pose = tuple[float, float, float]
Delta = tuple[float, float, float]


def compute_pose_delta(old_pose: Pose, new_pose: Pose) -> Delta:
    """Motion from ``old_pose`` to ``new_pose`` in the robot frame at ``old_pose``.

    Returns ``(delta_forward, delta_lateral, delta_theta)``:
    - ``delta_forward``  metres moved along the original heading
    - ``delta_lateral``  metres moved sideways (positive = left)
    - ``delta_theta``    rotation in radians, normalised to [-pi, pi]
      (positive = counter-clockwise)
    """
    old_x, old_y, old_theta = old_pose
    new_x, new_y, new_theta = new_pose

    dx = new_x - old_x
    dy = new_y - old_y

    cos_t = math.cos(-old_theta)
    sin_t = math.sin(-old_theta)
    delta_forward = dx * cos_t - dy * sin_t
    delta_lateral = dx * sin_t + dy * cos_t

    delta_theta = math.atan2(math.sin(new_theta - old_theta), math.cos(new_theta - old_theta))
    return (delta_forward, delta_lateral, delta_theta)


def absolute_to_local_nav_command(inputs: dict, robot_pose: Pose) -> dict:
    """Re-express an absolute nav goal as a robot-relative ``local_frame`` goal.

    Used in mapfree mode, where no map frame exists: the only absolute frame the
    agent knows is the one its pose readout uses, so a goal stated there is
    re-based onto ``robot_pose`` (given in that same frame) and marked
    ``local_frame=True`` instead of being sent to the inactive map-frame planner.

    Local-frame commands are returned unchanged. The input dict is never mutated.
    """
    if inputs.get("local_frame", False):
        return inputs

    # Same key convention as adjust_local_nav_command: read whichever theta
    # spelling is present and write back the same one.
    use_degrees = "theta" not in inputs and "theta_degrees" in inputs
    goal_theta = math.radians(inputs["theta_degrees"]) if use_degrees else inputs.get("theta", 0.0)
    goal_pose = (inputs.get("x", 0.0), inputs.get("y", 0.0), goal_theta)

    local_x, local_y, local_theta = compute_pose_delta(robot_pose, goal_pose)

    adjusted = inputs.copy()
    adjusted["x"] = local_x
    adjusted["y"] = local_y
    if use_degrees:
        adjusted["theta_degrees"] = math.degrees(local_theta)
    else:
        adjusted["theta"] = local_theta
    adjusted["local_frame"] = True
    return adjusted


def local_to_absolute_nav_command(inputs: dict, robot_pose: Pose) -> dict:
    """The exact inverse of :func:`absolute_to_local_nav_command`.

    Re-expresses a robot-relative goal in the frame ``robot_pose`` is given in,
    and clears ``local_frame`` so navigate_to_position plans it against the map
    instead of the rolling mapfree costmap. Absolute commands are returned
    unchanged. The input dict is never mutated.

    Used only under BENCH_MAP_FRAME_GOALS -- see sim/bench/FINDINGS.md
    (patch_map_frame_goals).
    """
    if not inputs.get("local_frame", False):
        return inputs

    robot_x, robot_y, robot_theta = robot_pose
    use_degrees = "theta" not in inputs and "theta_degrees" in inputs
    local_theta = math.radians(inputs["theta_degrees"]) if use_degrees else inputs.get("theta", 0.0)
    local_x = inputs.get("x", 0.0)
    local_y = inputs.get("y", 0.0)

    # Rotate by +theta and translate: the mirror of compute_pose_delta, which
    # rotates by -theta after translating.
    cos_t = math.cos(robot_theta)
    sin_t = math.sin(robot_theta)
    absolute_x = robot_x + local_x * cos_t - local_y * sin_t
    absolute_y = robot_y + local_x * sin_t + local_y * cos_t
    absolute_theta = math.atan2(math.sin(robot_theta + local_theta), math.cos(robot_theta + local_theta))

    adjusted = inputs.copy()
    adjusted["x"] = absolute_x
    adjusted["y"] = absolute_y
    if use_degrees:
        adjusted["theta_degrees"] = math.degrees(absolute_theta)
    else:
        adjusted["theta"] = absolute_theta
    adjusted["local_frame"] = False
    return adjusted


def adjust_local_nav_command(inputs: dict, delta: Delta) -> dict:
    """Re-express a local-frame navigation target after the robot has moved.

    For ``navigate_to_position`` with ``local_frame=True`` the original ``(x, y,
    theta)`` is in the frame where the robot stood at image capture; this shifts it
    into the robot's current frame using ``delta`` (from :func:`compute_pose_delta`).

    Global-frame commands are returned unchanged. The input dict is never mutated;
    a copy with updated ``x``/``y``/``theta`` is returned.
    """
    if not inputs.get("local_frame", False):
        return inputs

    delta_forward, delta_lateral, delta_theta = delta

    orig_x = inputs.get("x", 0.0)
    orig_y = inputs.get("y", 0.0)
    # The agent may speak either `theta` (radians) or the skill schema's
    # `theta_degrees`; read whichever is present and write back the SAME key,
    # so the adjusted dict never grows a second, conflicting spelling.
    use_degrees = "theta" not in inputs and "theta_degrees" in inputs
    orig_theta = math.radians(inputs["theta_degrees"]) if use_degrees else inputs.get("theta", 0.0)

    # Translate by the robot's displacement, then rotate into the new frame.
    translated_x = orig_x - delta_forward
    translated_y = orig_y - delta_lateral

    cos_dt = math.cos(-delta_theta)
    sin_dt = math.sin(-delta_theta)
    new_x = translated_x * cos_dt - translated_y * sin_dt
    new_y = translated_x * sin_dt + translated_y * cos_dt

    new_theta = math.atan2(math.sin(orig_theta - delta_theta), math.cos(orig_theta - delta_theta))

    adjusted = inputs.copy()
    adjusted["x"] = new_x
    adjusted["y"] = new_y
    if use_degrees:
        adjusted["theta_degrees"] = math.degrees(new_theta)
    else:
        adjusted["theta"] = new_theta
    return adjusted
