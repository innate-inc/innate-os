"""Semantic layer over the sim apartment: named rooms, the capture tour, and
the benchmark prop scene. Single source of truth for capture.py, the task set,
and the ground-truth scorer.

World frame == ROS map frame (the sim's grid localizer makes map == odom).
Room rectangles are loose floor bounds read off the top-down render and the
visual-mesh bounding boxes; viewpoints are exact poses verified by rendering.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Room:
    name: str
    x0: float
    y0: float
    x1: float
    y1: float
    description: str

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)


ROOMS: dict[str, Room] = {
    r.name: r
    for r in (
        Room("living_room", -6.9, -4.5, -3.5, -0.9, "sofa, coffee table and TV"),
        Room("dining_area", -3.1, -4.0, 0.0, -1.1, "dining table with six chairs"),
        Room("kitchen", -3.1, -1.1, 0.3, 0.5, "kitchen counters, sink, dishwasher and fridge"),
        Room("corridor", -5.15, -0.9, -3.5, 4.3, "central corridor"),
        Room("north_hallway", -6.5, 4.3, -3.5, 5.35, "hallway at the far north end"),
        Room("bathroom", -6.9, -0.6, -5.15, 4.3, "bathroom with tub, shower, sinks and toilet"),
        Room("master_bedroom", -3.5, 0.5, 0.4, 4.05, "master bedroom with a large double bed"),
        Room("office", -3.5, 4.2, 0.9, 5.35, "second bedroom used as an office, single bed and desk"),
    )
}


def room_at(x: float, y: float) -> str | None:
    """First matching room, or None outside all of them. Rect overlap is
    resolved by dict order, which lists the specific rooms before corridor."""
    for room in ROOMS.values():
        if room.contains(x, y):
            return room.name
    return None


@dataclass(frozen=True)
class PropPlacement:
    prop: str
    x: float
    y: float
    yaw_deg: float
    room: str
    note: str


# Dropped from above and settled by physics -- a placement over furniture ends
# up ON that furniture (the camera rides ~0.26m off the floor, so targets live
# on the floor where the robot can actually see them). Positions are verified
# free cells of the collision occupancy grid, next to the furniture the note
# names. Settled poses are re-read and written to scene.json.
SCENE: tuple[PropPlacement, ...] = (
    PropPlacement("banana", -1.10, -0.50, 30.0, "kitchen", "on the kitchen floor by the fridge"),
    PropPlacement("mug", -5.05, -1.85, 0.0, "living_room", "on the floor between the sofa and the coffee table"),
    PropPlacement("airpods", -0.75, 1.10, 15.0, "master_bedroom", "on the floor beside the bed"),
    PropPlacement("book", -2.50, 4.72, 70.0, "office", "on the office floor near the desk"),
    PropPlacement("backpack", -6.20, -3.40, 100.0, "living_room", "on the floor in the living room corner"),
    PropPlacement("sock", -4.15, 0.95, 0.0, "corridor", "on the corridor floor"),
    PropPlacement("ball", -0.70, -3.05, 0.0, "dining_area", "on the floor in the dining nook"),
    PropPlacement("can", -0.85, -0.55, 0.0, "kitchen", "on the kitchen floor by the fridge"),
    PropPlacement("cube", -2.85, 4.42, 0.0, "office", "on the office floor by the desk"),
)


@dataclass(frozen=True)
class Stop:
    """A tour waypoint. `looks` are extra headings (deg) captured standing
    still there -- how a curious robot sweeps a room it entered. `jump` marks
    the leg INTO this stop as teleport-only (no frames): used to reach floor
    pockets the collision grid shows as walled off (e.g. the living room's
    west side behind the armchair), where transit frames would clip furniture."""

    x: float
    y: float
    looks: tuple[float, ...] = field(default=())
    name: str = ""
    jump: bool = False


# One continuous walk that threads every room. Legs between stops are sampled
# at capture rate facing the direction of travel; looks add standing frames.
# Legs must thread doorways -- glide mode teleports, so a leg through a wall
# captures garbage. Every stop and leg is verified against free cells of
# occupancy_grid(); the bathroom needs a 3-point thread past its open door
# leaf (gap at x~-5.0, y 1.94..2.59, leaf swung NE into the corridor).
TOUR: tuple[Stop, ...] = (
    Stop(-4.34, -0.30, name="spawn"),
    Stop(-4.25, -1.30, looks=(180.0, -135.0), name="living_walkway"),
    Stop(-6.15, -2.50, looks=(55.0, -75.0, 90.0), name="living_west", jump=True),
    Stop(-6.00, -3.55, looks=(45.0, 100.0), name="living_sw"),
    Stop(-3.40, -1.15, name="dining_in", jump=True),
    Stop(-3.40, -2.20, looks=(30.0,), name="dining_west"),
    Stop(-3.40, -3.10, name="dining_sw"),
    Stop(-2.30, -3.10, looks=(90.0, 45.0), name="dining_south"),
    Stop(-0.85, -3.10, name="dining_se"),
    Stop(-0.80, -2.30, looks=(180.0, -90.0), name="dining_east"),
    Stop(-0.80, -0.80, name="kitchen_in"),
    Stop(-1.75, -0.55, looks=(180.0, 120.0, 0.0), name="kitchen"),
    Stop(-1.90, -0.85, name="kitchen_south"),
    Stop(-3.40, -1.15, name="dining_lane_w"),
    Stop(-3.80, -0.85, name="post_gap"),
    Stop(-4.30, -0.55, name="corridor_in"),
    Stop(-4.35, -0.20, looks=(150.0,), name="corridor_south"),
    Stop(-4.45, 1.30, looks=(90.0, -60.0), name="corridor_mid"),
    Stop(-4.30, 2.95, name="bath_thread_a"),
    Stop(-4.95, 2.50, name="bath_thread_b"),
    Stop(-4.97, 2.25, name="bath_thread_c"),
    Stop(-5.90, 2.10, looks=(95.0, 200.0), name="bathroom"),
    Stop(-4.97, 2.25, name="bath_out_c"),
    Stop(-4.95, 2.50, name="bath_out_b"),
    Stop(-4.30, 2.95, name="bath_out_a"),
    Stop(-4.40, 3.20, name="corridor_north"),
    Stop(-4.40, 4.55, name="hallway_junction"),
    Stop(-5.60, 4.60, looks=(0.0, -45.0), name="north_hallway"),
    Stop(-2.90, 4.60, looks=(0.0, -45.0), name="office_entry"),
    Stop(-2.30, 4.70, looks=(0.0, 90.0, -135.0), name="office_inner"),
    Stop(-2.95, 4.60, name="office_out"),
    Stop(-4.40, 4.55, name="hallway_junction2"),
    Stop(-4.40, 2.70, name="corridor_north2"),
    Stop(-3.30, 2.70, looks=(0.0, -90.0), name="bedroom_entry"),
    Stop(-3.30, 1.50, looks=(0.0, -35.0), name="bedroom_west"),
    Stop(-3.30, 2.70, name="bedroom_nw"),
    Stop(-1.20, 2.75, looks=(-90.0, 180.0), name="bedroom_north"),
    Stop(-0.70, 1.80, looks=(-90.0, 180.0), name="bedroom_east"),
    Stop(-1.20, 2.75, name="bedroom_out"),
    Stop(-3.30, 2.70, name="bedroom_door_out"),
    Stop(-4.40, 2.70, name="corridor_north3"),
    Stop(-4.34, -0.30, name="home"),
)


def leg_heading(a: Stop, b: Stop) -> float:
    return math.atan2(b.y - a.y, b.x - a.x)
