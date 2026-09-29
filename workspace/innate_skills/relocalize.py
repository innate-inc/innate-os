# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math

from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import SetInitialPose
from rclpy.node import Node

from brain_client.relocalization.hypotheses import decide
from brain_client.relocalization.scan_match import Grid, Pose2D, Scan
from innate import Lidar, Map, Mobility, Pose, Skill, SkillOutput, SkillReturn, resource

SETTLE_S = 0.4
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
    is wrong. Matches one lidar scan against the whole map, without moving. On success the
    navigation pose is reset; if more than one place fits the scan, it says it is lost
    rather than guessing. Needs a saved map."""

    mobility: Mobility
    lidar: Lidar | None
    map: Map | None
    pose: Pose | None

    @resource
    def seeder(self) -> _PoseSeeder:
        if self.node is None:
            self.fail("No ROS node to reach AMCL from.")
        return _PoseSeeder(self.node)

    def execute(self) -> SkillReturn:
        grid = Grid.from_map(self.map) if self.map is not None else None
        if grid is None:
            self.fail("No map to relocalize on — load a map in navigation mode first.")
        decision = decide(grid, self._scan())
        self.logger.info(f"[relocalize] {decision.reason}")
        if decision.pose is None:
            self.fail(
                f"Still lost ({decision.reason}). Place me on the map from the app, or move me somewhere with more distinctive walls and try again."
            )
        return self._localized(decision.pose)

    def _scan(self) -> Scan:
        """A lidar sweep taken after the base came to rest."""
        self.mobility.stop()
        stale = self.lidar
        self.sleep(SETTLE_S)
        lidar = self.wait_for(lambda: _newer(self.lidar, stale), timeout=2.0)
        if lidar is None:
            self.fail("No fresh lidar scan.")
        return Scan.from_lidar(lidar)

    def _localized(self, pose: Pose2D) -> SkillReturn:
        if not self.seeder.seed(pose.x, pose.y, pose.theta):
            self.fail(f"Found myself at x={pose.x:.2f} y={pose.y:.2f} but AMCL's /set_initial_pose is unavailable.")
        confirmed = self.wait_for(lambda: self._amcl_near(pose.x, pose.y), timeout=5.0)
        return SkillOutput(
            f"Relocalized at x={pose.x:.2f} m, y={pose.y:.2f} m, heading {math.degrees(pose.theta):.0f}° "
            f"(lidar fit {pose.fit:.0%})" + ("." if confirmed else " — AMCL has not confirmed the new pose yet.")
        )

    def _amcl_near(self, x: float, y: float) -> bool | None:
        pose = self.pose
        return True if pose is not None and math.hypot(pose.x - x, pose.y - y) < 0.3 else None


def _newer(latest: Lidar | None, stale: Lidar | None) -> Lidar | None:
    """Identity, not content: consecutive sim scans of a still scene are identical."""
    return latest if latest is not None and latest is not stale else None
