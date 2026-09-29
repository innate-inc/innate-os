# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math

from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import SetInitialPose
from rclpy.client import Client

from brain_client.relocalization.hypotheses import decide
from brain_client.relocalization.scan_match import Grid, Pose2D, Scan
from innate import Lidar, Map, Mobility, Pose, Skill, SkillOutput, SkillReturn, resource

SETTLE_S = 0.4
POSE_COVARIANCE = (0.05, 0.05, 0.03)  # x, y (m²), yaw (rad²) handed to AMCL with the pose
AMCL_TIMEOUT_S = 5.0
ADOPTED_M = 0.3  # AMCL has taken the pose once its estimate is this close...
ADOPTED_DEG = 15.0  # ...and turned no further than this from it


class Relocalize(Skill):
    """Work out where the robot is on its map when it is lost: carried somewhere, switched
    on away from where it was, or navigation keeps failing because its position on the map
    is wrong. Matches one lidar scan against the whole map, without moving. On success the
    navigation pose is reset; if more than one place fits the scan, it says it is lost
    rather than guessing. Needs a saved map."""

    mobility: Mobility
    lidar: Lidar | None
    map: Map | None
    pose: Pose | None

    @resource
    def amcl(self) -> Client:
        """AMCL's /set_initial_pose, the service the webapp's manual placement uses too."""
        if self.node is None:
            self.fail("No ROS node to reach AMCL from.")
        return self.node.create_client(SetInitialPose, "/set_initial_pose")

    def execute(self) -> SkillReturn:
        nav_map = self.map
        grid = Grid.from_map(nav_map) if nav_map is not None else None
        if nav_map is None or grid is None:
            self.fail("No map to relocalize on — load a map in navigation mode first.")
        decision = decide(grid, self._scan())
        self.logger.info(f"[relocalize] {decision.reason}")
        if decision.pose is None:
            self.fail(
                f"Still lost ({decision.reason}). Place me on the map from the app, or move me somewhere with more distinctive walls and try again."
            )
        if self.map is None or self.map.raw_source is not nav_map.raw_source:
            self.fail("The map changed while I was matching the scan; run me again.")
        self._seed(decision.pose)
        return SkillOutput(f"Relocalized at {_describe(decision.pose)} (lidar fit {decision.pose.fit:.0%}).")

    def _scan(self) -> Scan:
        """A lidar sweep taken after the base came to rest."""
        self.mobility.stop()
        stale = self.lidar
        self.sleep(SETTLE_S)
        lidar = self.wait_for(lambda: _newer(self.lidar, stale), timeout=2.0)
        if lidar is None:
            self.fail("No fresh lidar scan.")
        return Scan.from_lidar(lidar)

    def _seed(self, target: Pose2D) -> None:
        """Hand the pose to AMCL, and return only once AMCL itself reports it."""
        if self.wait_for(lambda: self.amcl.service_is_ready() or None, timeout=2.0) is None:
            self.fail(f"Found myself at {_describe(target)} but AMCL's /set_initial_pose is unavailable.")
        before = self.pose
        call = self.amcl.call_async(_initial_pose(target))
        if self.wait_for(lambda: call.done() or None, timeout=AMCL_TIMEOUT_S) is None or call.exception() is not None:
            self.fail(f"Found myself at {_describe(target)} but AMCL did not accept the pose.")
        if self.wait_for(lambda: self._amcl_reports(target, before), timeout=AMCL_TIMEOUT_S) is None:
            self.fail(f"Found myself at {_describe(target)} but AMCL's estimate has not moved there.")

    def _amcl_reports(self, target: Pose2D, before: Pose | None) -> bool | None:
        """True once AMCL publishes an estimate newer than ``before`` that matches ``target``, heading included."""
        now = self.pose
        if now is None or (before is not None and now.stamp <= before.stamp):
            return None
        estimate = Pose2D(now.x, now.y, now.theta)
        adopted = target.distance(estimate) < ADOPTED_M and target.heading_gap(estimate) < math.radians(ADOPTED_DEG)
        return True if adopted else None


def _initial_pose(pose: Pose2D) -> SetInitialPose.Request:
    msg = PoseWithCovarianceStamped()
    msg.header.frame_id = "map"
    msg.pose.pose.position.x, msg.pose.pose.position.y = pose.x, pose.y
    msg.pose.pose.orientation.z, msg.pose.pose.orientation.w = math.sin(pose.theta / 2), math.cos(pose.theta / 2)
    covariance = [0.0] * 36
    covariance[0], covariance[7], covariance[35] = POSE_COVARIANCE
    msg.pose.covariance = covariance
    return SetInitialPose.Request(pose=msg)


def _describe(pose: Pose2D) -> str:
    return f"x={pose.x:.2f} m, y={pose.y:.2f} m, heading {math.degrees(pose.theta):.0f}°"


def _newer(latest: Lidar | None, stale: Lidar | None) -> Lidar | None:
    """Identity, not content: consecutive sim scans of a still scene are identical."""
    return latest if latest is not None and latest is not stale else None
