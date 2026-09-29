# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
from typing import TypeVar

import numpy as np
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import SetInitialPose
from rclpy.node import Node

from brain_client.relocalization.hypotheses import Decision, Look, Match, decide, relative
from brain_client.relocalization.scan_match import Grid, Pose2D, Scan
from innate import (
    Head,
    Lidar,
    MainImage,
    Map,
    Mobility,
    Odometry,
    Pose,
    Skill,
    SkillOutput,
    SkillReturn,
    SpatialMemory,
    resource,
)

_T = TypeVar("_T")

MAX_VIEWS = 4  # recent views shown to the vision model at once
MAX_FORWARD_M = 0.5  # we're lost, so no map check: glass reads as open space to the lidar
WALL_MARGIN_M = 0.45  # stop this short of the nearest lidar return ahead
SETTLE_S = 0.4
RECOGNIZE_TIMEOUT_S = 150.0  # must outlive the brain's 120 s model call: goals can't be cancelled
POSE_COVARIANCE = (0.05, 0.05, 0.03)  # x, y (m²), yaw (rad²) handed to AMCL with the pose


class _PoseSeeder:
    """AMCL's /set_initial_pose client, on the run's node (the webapp's manual placement uses it too)."""

    def __init__(self, node: Node):
        self._client = node.create_client(SetInitialPose, "/set_initial_pose")

    def seed(self, x: float, y: float, theta: float) -> bool:
        if not self._client.wait_for_service(timeout_sec=2.0):
            return False
        pose = PoseWithCovarianceStamped()
        pose.header.frame_id = "map"
        pose.pose.pose.position.x, pose.pose.pose.position.y = x, y
        pose.pose.pose.orientation.z, pose.pose.pose.orientation.w = math.sin(theta / 2), math.cos(theta / 2)
        cov = [0.0] * 36
        cov[0], cov[7], cov[35] = POSE_COVARIANCE
        pose.pose.covariance = cov
        request = SetInitialPose.Request()
        request.pose = pose
        self._client.call_async(request)
        return True


class Relocalize(Skill):
    """Work out where the robot is on its map when it is lost: carried somewhere, switched
    on away from where it was, or navigation keeps failing because its position on the map
    is wrong. Compares the camera view with the robot's remembered views, checks every place
    it recognizes against the lidar, and turns or drives a little for a better look when
    unsure. On success the navigation pose is reset; if it still can't tell, it says it is
    lost rather than guessing. Needs a saved map and remembered views of it."""

    mobility: Mobility
    head: Head
    memory: SpatialMemory
    main_image: MainImage | None
    lidar: Lidar | None
    odom: Odometry | None
    map: Map | None
    pose: Pose | None

    @resource
    def seeder(self) -> _PoseSeeder:
        if self.node is None:
            self.fail("No ROS node to reach AMCL from.")
        return _PoseSeeder(self.node)

    def execute(self, max_moves: int = 3) -> SkillReturn:
        grid = Grid.from_map(self.map) if self.map is not None else None
        if grid is None:
            self.fail("No map to relocalize on — load a map in navigation mode first.")
        self.head.set_position(0)
        looks: list[Look] = []
        jpegs: list[bytes] = []
        decision = None
        for step in range(max_moves + 1):
            jpeg, look = self._look()
            looks.append(look)
            jpegs.append(jpeg)
            verdict = self._recognize(looks[-MAX_VIEWS:], jpegs[-MAX_VIEWS:])
            matches = [Match(m.view, m.frame, (m.x, m.y, m.theta), m.confidence) for m in verdict.matches]
            decision = decide(grid, looks[-MAX_VIEWS:], matches)
            self.logger.info(f"[relocalize] look {step + 1}: {len(matches)} recognized place(s) -> {decision.reason}")
            if decision.pose is not None:
                return self._localized(decision.pose, decision, step)
            if step < max_moves:
                self.feedback(f"Not sure yet ({decision.reason}); moving for a better view: {verdict.move_reason}")
                self._move(verdict.turn_deg, verdict.forward_m, look.scan)
        reason = decision.reason if decision is not None else "no look completed"
        self.fail(
            f"Still lost after {max_moves + 1} looks ({reason}). Place me on the map from the app, or drive me somewhere I've been before and try again."
        )

    def _look(self) -> tuple[bytes, Look]:
        """A camera frame and a lidar sweep both taken after the base came to rest."""
        self.mobility.stop()
        stale_image, stale_scan = self.main_image, self.lidar
        self.sleep(SETTLE_S)
        image = self.wait_for(lambda: _newer(self.main_image, stale_image), timeout=2.0)
        lidar = self.wait_for(lambda: _newer(self.lidar, stale_scan), timeout=2.0)
        odom = Mobility.odom_xyt(self.odom)
        if image is None or lidar is None or odom is None:
            self.fail("No fresh camera image, lidar scan or odometry.")
        return image.jpeg, Look(Scan.from_lidar(lidar), odom)

    def _recognize(self, looks: list[Look], jpegs: list[bytes]):
        now = looks[-1]
        views = [(jpeg, _view_label(look, now)) for jpeg, look in zip(jpegs, looks, strict=True)]
        context = f"Lidar free space around the robot now: {_clearance(now.scan)}."
        verdict = None
        for _ in range(2):  # one retry: a proxy timeout shouldn't end the search
            verdict = self.wait_for(self.memory.recognize(views, context), timeout=RECOGNIZE_TIMEOUT_S)
            if verdict is not None and not verdict.error:
                return verdict
        if verdict is None:
            self.fail("Place recognition timed out.")
        self.fail(f"Place recognition failed: {verdict.error}")

    def _move(self, turn_deg: float, forward_m: float, scan: Scan) -> None:
        def get_xyt():
            return Mobility.odom_xyt(self.odom)

        turn = math.radians(max(-180.0, min(180.0, turn_deg)))
        if abs(turn) > math.radians(5):
            self.mobility.rotate_by(get_xyt, turn, logger=self.logger)
        ahead = _free_ahead(scan, turn)
        distance = max(0.0, min(forward_m, MAX_FORWARD_M, ahead - WALL_MARGIN_M))
        if distance > 0.05:
            self.mobility.drive(get_xyt, distance, logger=self.logger)

    def _localized(self, pose: Pose2D, decision: Decision, moves: int) -> SkillReturn:
        if not self.seeder.seed(pose.x, pose.y, pose.theta):
            self.fail(f"Found myself at x={pose.x:.2f} y={pose.y:.2f} but AMCL's /set_initial_pose is unavailable.")
        confirmed = self.wait_for(lambda: self._amcl_near(pose.x, pose.y), timeout=5.0)
        frames = next((sorted(m.frames) for m in decision.modes if m.pose is pose), [])
        evidence = f"recognized remembered view {', '.join(f'#{f}' for f in frames)}" if frames else "lidar alone"
        return SkillOutput(
            f"Relocalized at x={pose.x:.2f} m, y={pose.y:.2f} m, heading {math.degrees(pose.theta):.0f}° "
            f"({evidence}; lidar fit {pose.fit:.0%}; after {moves} move(s))"
            + ("." if confirmed else " — AMCL has not confirmed the new pose yet.")
        )

    def _amcl_near(self, x: float, y: float) -> bool | None:
        pose = self.pose
        return True if pose is not None and math.hypot(pose.x - x, pose.y - y) < 0.3 else None


def _newer(latest: _T | None, stale: _T | None) -> _T | None:
    """Identity, not content: consecutive sim frames of a still scene are byte-identical."""
    return latest if latest is not None and latest is not stale else None


def _view_label(look: Look, now: Look) -> str:
    x, y, t = relative(now.odom, look.odom)
    if math.hypot(x, y) < 0.05 and abs(t) < math.radians(3):
        return "the robot's current view"
    turn = math.degrees(math.atan2(math.sin(t), math.cos(t)))
    side = "left" if turn > 0 else "right"
    return f"earlier view, taken {math.hypot(x, y):.1f} m away with the camera turned {abs(turn):.0f}° {side} of the current heading"


def _bearings(scan: Scan) -> tuple[np.ndarray, np.ndarray]:
    return np.degrees(np.arctan2(scan.py, scan.px)), np.hypot(scan.px, scan.py)


def _nearest(scan: Scan, center_deg: float, half_width_deg: float) -> float:
    bearing, distance = _bearings(scan)
    near = distance[np.abs((bearing - center_deg + 180.0) % 360.0 - 180.0) < half_width_deg]
    return float(near.min()) if len(near) else 12.0


def _free_ahead(scan: Scan, turn: float) -> float:
    return _nearest(scan, math.degrees(turn), 20.0)


def _clearance(scan: Scan) -> str:
    sides = (("ahead", 0.0), ("left", 90.0), ("behind", 180.0), ("right", -90.0))
    return ", ".join(f"{name} {_nearest(scan, deg, 15.0):.1f} m" for name, deg in sides)
