"""A grasp/reach feasibility tool: answer "can I get this from here, and if
not, where would I need to stand?" without driving there to find out.

WHY THIS EXISTS. Every probe transcript in this project's history has the
same expensive pattern: drive an estimated distance, call pick, get "nothing
within reach; get closer", re-estimate, drive again -- or the opposite,
misjudge a shelf as reachable and burn a whole approach on an object that was
never gettable. `counter_out_of_reach` exists BECAUSE this judgment is hard
from a single fixed-pitch camera. This module answers the question in closed
form instead of by trial and error, using the SAME constants the harness
itself judges pick/place against (imported, not re-typed) -- so a tool that
says "reachable" is never wrong about what the judge will accept. The one
exception is MIN_STANDOFF_M below, which the judge does not own: it is a
planning preference and is written down here.

TWO QUERIES, both O(1), no MuJoCo call needed:

  can_reach(robot_pose, target_xyz) -> Verdict
      Is the target pickable RIGHT NOW, from where the robot already is?

  standoff_for(target_xy, target_z) -> Standoff | None
      If not, where is the nearest point the robot COULD stand such that it
      becomes pickable -- or None if the height alone rules it out no matter
      where the base goes (counter_out_of_reach's shape exactly).

Deliberately NOT a full inverse-kinematics solver. The harness's own judge
does not check joint angles either -- it checks a horizontal distance and a
height, because that is what the real arm's tucked-fold envelope reduces to
for anything sitting on a shelf, counter or floor in front of the robot (see
brain_agent.py's PICK_REACH_M / ARM_Z_MAX_M, and core.py's joint2_min_target
for why a full-envelope model earns its keep only once tasks ask the arm to
work *behind* or *beside* the robot, which none in this suite do). A closed-
form envelope check is the elegant tool for the shape of task this suite
has; a 6-DOF solver would be answering a question nobody is asking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from brain_agent import ARM_Z_MAX_M, PICK_REACH_M

# How close the BASE CENTRE can get to a vertical obstacle (shelf front,
# counter face) before the footprint itself is the limit, independent of arm
# reach. Matches planner_agent.NEAR_STANDOFF -- the number every hand-plan in
# this repo already stands its oracle at.
MIN_STANDOFF_M = 0.34


@dataclass
class Verdict:
    reachable: bool
    horizontal_m: float
    height_m: float | None
    reason: str


@dataclass
class Standoff:
    x: float
    y: float
    distance_from_target_m: float


def can_reach(robot_xy: tuple[float, float], target_xyz: tuple[float, float, float]) -> Verdict:
    """Verdict for picking target_xyz from the robot's CURRENT position."""
    tx, ty, tz = target_xyz
    d = math.hypot(tx - robot_xy[0], ty - robot_xy[1])
    if tz > ARM_Z_MAX_M:
        return Verdict(
            False,
            round(d, 3),
            round(tz, 3),
            f"{tz:.2f} m up -- arm reaches below {ARM_Z_MAX_M:.2f} m regardless of distance",
        )
    if d > PICK_REACH_M:
        return Verdict(
            False, round(d, 3), round(tz, 3), f"{d:.2f} m away -- outside the {PICK_REACH_M:.2f} m pick radius"
        )
    return Verdict(True, round(d, 3), round(tz, 3), "in reach")


def standoff_for(target_xy: tuple[float, float], target_z: float) -> Standoff | None:
    """Nearest point (bearing unconstrained -- any direction) from which
    target_xy/z becomes pickable. None if the HEIGHT rules it out no matter
    where the base stands -- the honest "out of reach" case."""
    if target_z > ARM_Z_MAX_M:
        return None
    # Any point within PICK_REACH_M works; MIN_STANDOFF_M keeps the base off
    # the obstacle the target is sitting on/against. The tightest legal ring
    # is [MIN_STANDOFF_M, PICK_REACH_M] around the target -- report its near
    # edge, which is what a caller re-planning an approach actually wants.
    r = MIN_STANDOFF_M
    return Standoff(x=target_xy[0], y=target_xy[1] + r, distance_from_target_m=r)
