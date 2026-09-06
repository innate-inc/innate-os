# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time

from pydantic import BaseModel

from innate import Map, Mobility, Odometry, Pose, Skill, SkillOutput, SkillReturn

# Allowed base speeds (m/s). Slow on purpose: no obstacle avoidance here.
MIN_SPEED = 0.05
MAX_SPEED = 0.3
DEFAULT_SPEED = 0.15
# Circumscribed footprint radius: the front corners sit at hypot(0.25, 0.165).
ROBOT_CLEARANCE_M = 0.30


def _cell(map_state: Map, x: float, y: float) -> tuple[int, int]:
    dx = x - map_state.origin_x
    dy = y - map_state.origin_y
    cos_map = math.cos(-map_state.origin_theta)
    sin_map = math.sin(-map_state.origin_theta)
    col = math.floor((dx * cos_map - dy * sin_map) / map_state.resolution)
    row = math.floor((dx * sin_map + dy * cos_map) / map_state.resolution)
    return row, col


def _footprint_offsets(resolution: float) -> list[tuple[int, int]]:
    reach = math.ceil(ROBOT_CLEARANCE_M / resolution)
    limit = (ROBOT_CLEARANCE_M / resolution) ** 2
    return [
        (dr, dc) for dr in range(-reach, reach + 1) for dc in range(-reach, reach + 1) if dr * dr + dc * dc <= limit
    ]


def _crosses_keepout(map_state: Map, pose: Pose, distance: float) -> bool:
    """True when the motion carries the footprint onto a keepout cell it does not
    already cover — a robot parked beside a zone must still be able to drive away."""
    mask = map_state.keepout_grid
    if mask is None:
        return False
    offsets = _footprint_offsets(map_state.resolution)
    start_row, start_col = _cell(map_state, pose.x, pose.y)
    already_on = {(start_row + dr, start_col + dc) for dr, dc in offsets}
    step = max(0.02, map_state.resolution * 0.5)
    travel_steps = max(1, math.ceil(abs(distance) / step))
    for index in range(1, travel_steps + 1):
        traveled = math.copysign(min(abs(distance), index * step), distance)
        row, col = _cell(map_state, pose.x + traveled * math.cos(pose.theta), pose.y + traveled * math.sin(pose.theta))
        for dr, dc in offsets:
            cell = (row + dr, col + dc)
            if cell in already_on or not (0 <= cell[0] < map_state.height and 0 <= cell[1] < map_state.width):
                continue
            if mask[cell] >= 50:
                return True
    return False


class MoveResult(BaseModel):
    """Structured payload on .data for chaining callers."""

    traveled_m: float


class MoveStraight(Skill):
    """Move the robot straight forward (positive distance, meters) or backward
    (negative distance) using odometry only -- no map or path planning, and no
    obstacle avoidance. Use for short moves in clear space; prefer
    navigate_to_position when a map position is needed."""

    mobility: Mobility
    odom: Odometry
    map: Map | None
    pose: Pose | None

    def execute(self, distance: float, speed: float = DEFAULT_SPEED) -> SkillReturn:
        if distance == 0.0:
            return SkillOutput("Moved 0.0m", MoveResult(traveled_m=0.0))
        if self.map is not None and self.pose is not None and _crosses_keepout(self.map, self.pose, distance):
            self.fail("Refusing direct motion because it crosses a keepout zone")
        start = self.odom.position
        target = abs(distance)
        velocity = math.copysign(min(max(abs(speed), MIN_SPEED), MAX_SPEED), distance)
        deadline = time.time() + target / abs(velocity) * 3.0 + 2.0

        traveled = 0.0
        while traveled < target:
            if time.time() > deadline:
                self.fail(f"Stuck: moved only {traveled:.2f}m of {target:.2f}m")
            self.mobility.send_cmd_vel(linear_x=velocity, duration=0.5)
            self.sleep(0.1)
            traveled = math.dist(self.odom.position, start)

        self.mobility.stop()
        direction = "forward" if distance > 0 else "backward"
        return SkillOutput(f"Moved {traveled:.2f}m {direction}", MoveResult(traveled_m=round(traveled, 3)))
