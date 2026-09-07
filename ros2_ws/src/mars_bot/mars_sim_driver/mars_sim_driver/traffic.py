"""Deterministic intersection traffic for Crossroads.

MuJoCo owns the clock, signal aspects, and car poses.  The browser receives a
car roster plus snapshots of that authoritative state; it never
runs a second traffic simulation.  Cars are mocap bodies: their paths and
signal compliance stay deterministic while their collision boxes remain real
obstacles to MARS and their group-1 visual primitives remain visible to the
robot cameras and lidar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape
from typing import Any

from .crossroads import (
    CAR_LENGTH_M,
    CAR_MODEL,
    CAR_WIDTH_M,
    EW,
    GREEN,
    JUNCTION_HALF_SIZE,
    LANE_CENTER,
    NS,
    RED,
    ROAD_HALF_WIDTH,
    ROUTE_END,
    SIGNAL_MATERIALS,
    STOP_CENTER,
    YELLOW,
)

GREEN_S = 12.0
YELLOW_S = 3.0
ALL_RED_S = 4.0
CYCLE_S = 2 * (GREEN_S + YELLOW_S + ALL_RED_S)

CRUISE_SPEED_MPS = 3.0
ACCEL_MPS2 = 1.5
BRAKE_MPS2 = 3.0
STOP_EPS_M = 0.015
COMMIT_MARGIN_M = 0.20
ROBOT_RADIUS_M = 0.28
ROBOT_FOLLOW_GAP_M = 0.55

CAR_HALF_LENGTH_M = CAR_LENGTH_M / 2
# Cars at the authored stop centres keep their front bumper just outside this
# box. A vehicle already overlapping it owns the junction until its rear has
# cleared, preventing cross traffic from driving through a MARS-blocked car.
INTERSECTION_MIN_M = -JUNCTION_HALF_SIZE
INTERSECTION_MAX_M = JUNCTION_HALF_SIZE

# A private collision category: cars ignore the town's closed road-end walls
# and scenery, while configure_robot_spec() adds this bit to the robot before
# compilation so the car boxes still collide with MARS.
CAR_COLLISION_BIT = 2


@dataclass(frozen=True)
class Lane:
    id: str
    signal_group: str
    axis: str
    direction: int
    fixed: float
    start: float
    end: float
    stop: float
    yaw: float
    color: str
    initial_position: float


LANES = (
    # Route endpoints put the whole 3.6m car beyond the 40 m Crossroads bounds,
    # so a recycle never visibly pops in the middle of a road-edge facade.
    Lane(
        "eastbound", EW, "x", 1, -LANE_CENTER, -ROUTE_END, ROUTE_END, -STOP_CENTER, 0.0, "#e05b47", -STOP_CENTER - 3.2
    ),
    Lane(
        "westbound", EW, "x", -1, LANE_CENTER, ROUTE_END, -ROUTE_END, STOP_CENTER, math.pi, "#4d83d1", STOP_CENTER + 3.2
    ),
    # MARS spawns beside the northbound lane. Start this car beyond the
    # junction; after it recycles at the south edge, robot following prevents
    # it from spawning through or driving through the stationary robot.
    Lane(
        "northbound",
        NS,
        "y",
        1,
        LANE_CENTER,
        -ROUTE_END,
        ROUTE_END,
        -STOP_CENTER,
        math.pi / 2,
        "#e2b643",
        2 * ROAD_HALF_WIDTH,
    ),
    Lane(
        "southbound",
        NS,
        "y",
        -1,
        -LANE_CENTER,
        ROUTE_END,
        -ROUTE_END,
        STOP_CENTER,
        -math.pi / 2,
        "#55a96f",
        STOP_CENTER + 3.2,
    ),
)


@dataclass
class Car:
    lane: Lane
    position: float
    speed: float = 0.0
    spawn_seq: int = 0
    active: bool = True
    committed: bool = False


def signals_at(sim_time: float) -> dict[str, str]:
    """Scheduled aspects before the occupied-junction interlock."""
    t = sim_time % CYCLE_S
    if t < ALL_RED_S:
        return {NS: RED, EW: RED}
    if t < ALL_RED_S + GREEN_S:
        return {NS: GREEN, EW: RED}
    if t < ALL_RED_S + GREEN_S + YELLOW_S:
        return {NS: YELLOW, EW: RED}
    if t < 2 * ALL_RED_S + GREEN_S + YELLOW_S:
        return {NS: RED, EW: RED}
    if t < 2 * ALL_RED_S + 2 * GREEN_S + YELLOW_S:
        return {NS: RED, EW: GREEN}
    return {NS: RED, EW: YELLOW}


def _rgba(hex_color: str) -> str:
    value = hex_color.lstrip("#")
    rgb = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    return " ".join(f"{channel:.6g}" for channel in (*rgb, 1.0))


def _vec(values: list[float]) -> str:
    return " ".join(f"{value:.6g}" for value in values)


def _part_xml(part: dict[str, Any], body_color: str, name: str, index: int) -> str:
    material = part["material"]
    color = body_color if material == "body" else CAR_MODEL["materials"][material]
    common = f'name="{name}" pos="{_vec(part["position"])}" rgba="{_rgba(color)}" contype="0" conaffinity="0" group="1"'
    if "rotation" in part:
        common += f' euler="{_vec([math.degrees(value) for value in part["rotation"]])}"'
    if part["shape"] == "prism":
        return f'      <geom type="mesh" mesh="traffic_profile_{index}" {common}/>'
    if part["shape"] == "box":
        half = [value / 2 for value in part["size"]]
        return f'      <geom type="box" size="{_vec(half)}" {common}/>'
    return f'      <geom type="cylinder" size="{part["radius"]:.6g} {part["length"] / 2:.6g}" {common}/>'


def _collider_xml(collider: dict[str, Any]) -> str:
    half = [value / 2 for value in collider["size"]]
    return (
        f'      <geom type="box" size="{_vec(half)}" '
        f'pos="{_vec(collider["position"])}" contype="{CAR_COLLISION_BIT}" conaffinity="0" '
        'friction="0.8 0.01 0.001" group="3" rgba="0 0 0 0"/>'
    )


class TrafficController:
    """Environment-scoped traffic clock, cars, and MuJoCo bindings."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.cars = [Car(lane, lane.initial_position) for lane in LANES] if self.enabled else []
        self._mocap_ids: dict[str, int] = {}
        self._rolling_geoms: dict[str, list[tuple[int, float, Any]]] = {}
        self._signal_material_ids: dict[str, dict[str, int]] = {}
        self._model: Any | None = None
        self._last_aspects: dict[str, str] | None = None
        self._current_aspects = signals_at(0.0)

    def bodies_xml(self) -> str:
        if not self.enabled:
            return ""
        bodies = []
        for car in self.cars:
            name = escape(f"traffic_car_{car.lane.id}", quote=True)
            visuals = "\n".join(
                _part_xml(part, car.lane.color, f"{name}_part_{index}", index)
                for index, part in enumerate(CAR_MODEL["parts"])
            )
            colliders = "\n".join(_collider_xml(collider) for collider in CAR_MODEL["colliders"])
            bodies.append(f'    <body name="{name}" mocap="true">\n{visuals}\n{colliders}\n    </body>')
        return "\n".join(bodies)

    def assets_xml(self) -> str:
        """Tiny convex side-profile extrusions; shared across all four cars.

        MuJoCo builds their visual hull directly from these inline vertices.
        The car GLB is generated from the same profiles at asset-build time.
        """
        if not self.enabled:
            return ""
        return "\n".join(
            f'<mesh name="traffic_profile_{index}" vertex="'
            + " ".join(_vec([x, sign * part["width"] / 2, z]) for sign in (-1, 1) for x, z in part["profile"])
            + '"/>'
            for index, part in enumerate(CAR_MODEL["parts"])
            if part["shape"] == "prism"
        )

    def configure_robot_spec(self, robot_spec: Any) -> None:
        """Opt robot collisions into the private car category pre-compile.

        MuJoCo aggregates geom masks into body_contype/body_conaffinity while
        compiling. Mutating only model.geom_conaffinity later looks correct in
        an inspector but leaves broad-phase car/robot pairing disabled.
        """
        if not self.enabled:
            return
        colliders = 0
        for geom in robot_spec.geoms:
            if int(geom.contype) == 0:
                continue
            geom.conaffinity = int(geom.conaffinity) | CAR_COLLISION_BIT
            colliders += 1
        if colliders == 0:
            raise RuntimeError("traffic requires at least one collidable robot geom")

    def bind(self, model: Any) -> None:
        if not self.enabled:
            return
        self._model = model
        self._mocap_ids = {
            car.lane.id: int(model.body_mocapid[model.body(f"traffic_car_{car.lane.id}").id]) for car in self.cars
        }
        if any(mocap_id < 0 for mocap_id in self._mocap_ids.values()):
            raise RuntimeError("traffic car body was not compiled as a MuJoCo mocap body")
        for car in self.cars:
            geoms = []
            for index, part in enumerate(CAR_MODEL["parts"]):
                if "rolling_radius" in part:
                    geom_id = model.geom(f"traffic_car_{car.lane.id}_part_{index}").id
                    geoms.append((geom_id, part["rolling_radius"], model.geom_quat[geom_id].copy()))
                    # mjSAMEFRAME_NONE: the compiler's body-aligned shortcut
                    # would otherwise ignore our changing spoke quaternion.
                    model.geom_sameframe[geom_id] = 0
            self._rolling_geoms[car.lane.id] = geoms

        missing = []
        for group, aspects in SIGNAL_MATERIALS.items():
            ids: dict[str, int] = {}
            for aspect, source_name in aspects.items():
                material_name = f"mat_{source_name.lower().replace('_', '-')}"
                try:
                    ids[aspect] = int(model.mat(material_name).id)
                except KeyError:
                    missing.append(material_name)
            self._signal_material_ids[group] = ids
        if missing:
            raise RuntimeError(
                "Crossroads traffic-signal materials are missing ("
                + ", ".join(missing)
                + "). Rebuild Crossroads with sim/tools/build_intersection.py."
            )

    def reset(self, data: Any | None = None) -> None:
        if not self.enabled:
            return
        for car in self.cars:
            car.position = car.lane.initial_position
            car.speed = 0.0
            car.spawn_seq = 0
            car.active = True
            car.committed = False
        self._last_aspects = None
        if data is not None:
            self._current_aspects = self._effective_signals(float(data.time))
            self._write_mocap(data)
            self._apply_signal_materials(self._current_aspects)

    def advance(self, dt: float, sim_time: float, robot_xy: tuple[float, float] | None = None) -> None:
        if not self.enabled:
            return
        aspects = self._effective_signals(sim_time)
        self._current_aspects = dict(aspects)
        occupied = self._occupied_groups()
        for car in self.cars:
            conflicting_group = EW if car.lane.signal_group == NS else NS
            self._advance_car(car, dt, aspects[car.lane.signal_group], occupied[conflicting_group], robot_xy)
        self._apply_signal_materials(aspects)

    def _occupied_groups(self) -> dict[str, bool]:
        occupied = {NS: False, EW: False}
        for car in self.cars:
            if (
                car.active
                and car.position + CAR_HALF_LENGTH_M > INTERSECTION_MIN_M
                and car.position - CAR_HALF_LENGTH_M < INTERSECTION_MAX_M
            ):
                occupied[car.lane.signal_group] = True
        return occupied

    def _effective_signals(self, sim_time: float) -> dict[str, str]:
        """Hold the next direction at red until the conflict box is empty.

        The scheduled all-red interval is normally ample.  This interlock also
        covers an exceptional obstruction (for example, MARS physically
        pinning a committed car in the junction), so the visible lamps never
        invite traffic into a box that the controller itself considers unsafe.
        """
        aspects = signals_at(sim_time)
        occupied = self._occupied_groups()
        if aspects[NS] != RED and occupied[EW]:
            aspects[NS] = RED
        if aspects[EW] != RED and occupied[NS]:
            aspects[EW] = RED
        return aspects

    def step(self, data: Any, dt: float, robot_xy: tuple[float, float]) -> None:
        self.advance(dt, float(data.time) + dt, robot_xy)
        self._write_mocap(data)

    def _advance_car(
        self,
        car: Car,
        dt: float,
        aspect: str,
        conflicting_occupied: bool,
        robot_xy: tuple[float, float] | None,
    ) -> None:
        lane = car.lane
        if not car.active:
            if self._spawn_is_clear(lane, robot_xy):
                car.position = lane.start
                car.speed = 0.0
                car.spawn_seq += 1
                car.active = True
                car.committed = False
            return

        entered = lane.direction * (car.position - lane.stop) > STOP_EPS_M
        # Commitment is a yellow-clearance decision, not permission to wait
        # before the line through an entire all-red and then enter against red.
        # A robot obstruction that lasts until red therefore cancels it; cars
        # already over the line remain committed and keep clearing the box.
        if aspect == RED and not entered:
            car.committed = False
        distance_to_stop = max(0.0, lane.direction * (lane.stop - car.position))
        if entered:
            car.committed = True
        permission = aspect == GREEN and not conflicting_occupied
        if not entered and permission:
            # Dilemma-zone rule: once normal braking can no longer keep the
            # centre behind the line, finish the crossing through yellow and
            # all-red. This avoids impossible instantaneous stops and lets a
            # driver arriving late in green behave naturally.
            braking_distance = car.speed * car.speed / (2 * BRAKE_MPS2)
            if distance_to_stop <= braking_distance + COMMIT_MARGIN_M:
                car.committed = True
        can_enter = permission or car.committed
        limits: list[float] = []  # distance the car centre may still travel
        if not entered and not can_enter:
            limits.append(max(0.0, lane.direction * (lane.stop - car.position)))

        if robot_xy is not None:
            robot_axis = robot_xy[0] if lane.axis == "x" else robot_xy[1]
            robot_fixed = robot_xy[1] if lane.axis == "x" else robot_xy[0]
            if abs(robot_fixed - lane.fixed) <= CAR_WIDTH_M / 2 + ROBOT_RADIUS_M:
                centre_gap = lane.direction * (robot_axis - car.position)
                clearance = CAR_HALF_LENGTH_M + ROBOT_RADIUS_M + ROBOT_FOLLOW_GAP_M
                if centre_gap >= 0:
                    limits.append(max(0.0, centre_gap - clearance))

        desired = CRUISE_SPEED_MPS
        if limits:
            travel = min(limits)
            desired = min(desired, math.sqrt(max(0.0, 2 * BRAKE_MPS2 * travel)))
            if travel <= STOP_EPS_M:
                desired = 0.0

        if car.speed < desired:
            car.speed = min(desired, car.speed + ACCEL_MPS2 * dt)
        else:
            car.speed = max(desired, car.speed - BRAKE_MPS2 * dt)

        travel = car.speed * dt
        if limits:
            travel = min(travel, min(limits))
            if min(limits) <= STOP_EPS_M:
                car.speed = 0.0
        car.position += lane.direction * travel

        if lane.direction * (car.position - lane.end) >= 0:
            car.speed = 0.0
            if self._spawn_is_clear(lane, robot_xy):
                car.position = lane.start
                car.spawn_seq += 1
                car.committed = False
            else:
                # Keep the actor physically and visually out of the world
                # until the next clear step rather than teleporting onto MARS.
                car.position = lane.end
                car.active = False
                car.committed = False

    @staticmethod
    def _spawn_is_clear(lane: Lane, robot_xy: tuple[float, float] | None) -> bool:
        if robot_xy is None:
            return True
        robot_axis = robot_xy[0] if lane.axis == "x" else robot_xy[1]
        robot_fixed = robot_xy[1] if lane.axis == "x" else robot_xy[0]
        if abs(robot_fixed - lane.fixed) > CAR_WIDTH_M / 2 + ROBOT_RADIUS_M:
            return True
        # Traffic is generated at the edge of the authored town.  If MARS is
        # already occupying this approach, keep the next car off-scene instead
        # of introducing a permanent queue between the follow camera and the
        # robot.  An already-visible car still follows the ordinary braking
        # rule above if MARS moves into its lane later.
        route_length = lane.direction * (lane.end - lane.start)
        distance_along_route = lane.direction * (robot_axis - lane.start)
        return not (0.0 <= distance_along_route <= route_length)

    def _write_mocap(self, data: Any) -> None:
        if not self._mocap_ids:
            return
        for index, car in enumerate(self.cars):
            mocap_id = self._mocap_ids[car.lane.id]
            if not car.active:
                data.mocap_pos[mocap_id] = (1_000.0 + 10.0 * index, 1_000.0, 0.0)
                data.mocap_quat[mocap_id] = (1.0, 0.0, 0.0, 0.0)
                continue
            if car.lane.axis == "x":
                x, y = car.position, car.lane.fixed
            else:
                x, y = car.lane.fixed, car.position
            data.mocap_pos[mocap_id] = (x, y, 0.0)
            half_yaw = car.lane.yaw / 2
            data.mocap_quat[mocap_id] = (math.cos(half_yaw), 0.0, 0.0, math.sin(half_yaw))
            # Straight lanes: forward distance comes directly from position.
            # No independent clock, accumulated drift, joints or wheel forces.
            # Rotate only visual geoms; the two native collision boxes stay put.
            for geom_id, radius, base in self._rolling_geoms[car.lane.id]:
                half_roll = car.lane.direction * car.position / radius / 2
                c, s = math.cos(half_roll), math.sin(half_roll)
                w, x, y, z = base
                self._model.geom_quat[geom_id] = (c * w - s * y, c * x + s * z, c * y + s * w, c * z - s * x)

    def _apply_signal_materials(self, aspects: dict[str, str]) -> None:
        if self._model is None or aspects == self._last_aspects:
            return
        for group, selected in aspects.items():
            for aspect, material_id in self._signal_material_ids[group].items():
                active = aspect == selected
                self._model.mat_rgba[material_id] = (1.0, 1.0, 1.0, 1.0) if active else (0.10, 0.10, 0.10, 1.0)
                self._model.mat_emission[material_id] = 0.45 if active else 0.0
        self._last_aspects = dict(aspects)

    def manifest(self) -> list[dict[str, str]]:
        return [{"id": car.lane.id, "color": car.lane.color} for car in self.cars]

    def state(self, world_epoch: int) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        # advance() chose the interlocked aspects used for both vehicle
        # permission and MuJoCo materials.  Publish that exact choice rather
        # than recomputing the static schedule and risking a contradictory UI.
        cars = {}
        for car in self.cars:
            if not car.active:
                continue
            x, y = (car.position, car.lane.fixed) if car.lane.axis == "x" else (car.lane.fixed, car.position)
            cars[car.lane.id] = {
                "pose": [x, y, car.lane.yaw],
                "spawn_seq": car.spawn_seq,
            }
        return {
            "world_epoch": world_epoch,
            "signals": dict(self._current_aspects),
            "cars": cars,
        }
