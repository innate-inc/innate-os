"""Sim stand-in for mars_nav's grid_localizer. Same contract: lifecycle node named
navigation_grid_localizer, latched /initialpose, `localize` and
`localization/hand_placed` Triggers -- but
"localization" is just the driver's ground-truth odom pose (map == odom in
sim until AMCL refines it)."""

import math
import time

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import Odometry
from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import Int64
from std_srvs.srv import Trigger

POSITION_MATCH_TOLERANCE_M = 0.35
YAW_MATCH_TOLERANCE_RAD = math.radians(20.0)
AMCL_DRIFT_POSITION_M = 1.0
AMCL_DRIFT_YAW_RAD = math.radians(35.0)
AMCL_DRIFT_MIN_SAMPLES = 4
AMCL_DRIFT_DWELL_S = 0.75
AMCL_MODERATE_DRIFT_POSITION_M = 0.5
AMCL_MODERATE_DRIFT_YAW_RAD = math.radians(25.0)
AMCL_MODERATE_DRIFT_MIN_SAMPLES = 12
AMCL_MODERATE_DRIFT_DWELL_S = 3.0
AMCL_HEALTHY_REARM_S = 5.0
AMCL_RESEED_COOLDOWN_S = 15.0


def _yaw(pose) -> float:
    orientation = pose.orientation
    return math.atan2(
        2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
        1.0 - 2.0 * (orientation.y**2 + orientation.z**2),
    )


def _stamp_ns(msg) -> int:
    return int(msg.header.stamp.sec) * 1_000_000_000 + int(msg.header.stamp.nanosec)


def _pose_error(first, second) -> tuple[float, float]:
    position_error = math.hypot(first.position.x - second.position.x, first.position.y - second.position.y)
    yaw_error = math.atan2(math.sin(_yaw(first) - _yaw(second)), math.cos(_yaw(first) - _yaw(second)))
    return position_error, abs(yaw_error)


class GridLocalizerSim(LifecycleNode):
    def __init__(self):
        super().__init__("navigation_grid_localizer")
        self._last_odom = None
        self._pose_pub = None
        self._retry_timer = None
        self._localization_started_ns = None
        self._reset_pending = False
        self._world_epoch = None
        self._drift_bad_since = None
        self._drift_bad_samples = 0
        self._drift_armed = True
        self._drift_healthy_since = None
        self._last_auto_reseed_at = None
        self.create_subscription(Odometry, "/odom", self._on_odom, 10)
        # AMCL's /initialpose sub is VOLATILE, so latching can't cover the
        # activation race -- retry until AMCL answers with /amcl_pose.
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self._on_amcl_pose, 10)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(Int64, "/virtual_mars/world_epoch", self._on_world_epoch, latched)
        self.create_service(Trigger, "localize", self._on_localize)
        self.create_service(Trigger, "localization/hand_placed", self._on_hand_placed)

    def _on_odom(self, msg):
        self._last_odom = msg
        if not self._reset_pending:
            return
        self._reset_pending = False
        self.get_logger().info("simulator reset completed; reseeding AMCL")
        self._begin_localization()

    def _on_world_epoch(self, msg):
        if self._world_epoch is None:
            self._world_epoch = msg.data
            return
        if msg.data == self._world_epoch:
            return
        self._world_epoch = msg.data
        self._reset_pending = True
        self._reset_drift_guard()
        self._cancel_retry()

    def _on_amcl_pose(self, msg):
        if self._reset_pending:
            return
        pose = msg.pose.pose
        if self._retry_timer is not None:
            if self._localization_started_ns is not None and _stamp_ns(msg) < self._localization_started_ns:
                return
            automatic_recovery = not self._drift_armed
            if automatic_recovery:
                if self._last_odom is None:
                    return
                position_error, yaw_error = _pose_error(pose, self._last_odom.pose.pose)
                if position_error > POSITION_MATCH_TOLERANCE_M or yaw_error > YAW_MATCH_TOLERANCE_RAD:
                    return
            self._clear_drift_observation()
            if automatic_recovery:
                self._drift_armed = True
                self._drift_healthy_since = None
            self._cancel_retry()
            self.get_logger().info("AMCL localized; stopping /initialpose retries")
            return

        if self._last_odom is None:
            return
        now = time.monotonic()
        position_error, yaw_error = _pose_error(pose, self._last_odom.pose.pose)
        healthy = position_error <= POSITION_MATCH_TOLERANCE_M and yaw_error <= YAW_MATCH_TOLERANCE_RAD
        if healthy:
            self._clear_drift_observation()
            if self._drift_armed:
                return
            if self._drift_healthy_since is None:
                self._drift_healthy_since = now
            cooldown_over = (
                self._last_auto_reseed_at is None or now - self._last_auto_reseed_at >= AMCL_RESEED_COOLDOWN_S
            )
            if now - self._drift_healthy_since >= AMCL_HEALTHY_REARM_S and cooldown_over:
                self._drift_armed = True
                self._drift_healthy_since = None
                self.get_logger().info("AMCL drift guard rearmed after stable localization")
            return

        self._drift_healthy_since = None
        if not self._drift_armed:
            self._clear_drift_observation()
            return
        severe_drift = position_error >= AMCL_DRIFT_POSITION_M or yaw_error >= AMCL_DRIFT_YAW_RAD
        moderate_drift = position_error >= AMCL_MODERATE_DRIFT_POSITION_M or yaw_error >= AMCL_MODERATE_DRIFT_YAW_RAD
        if not moderate_drift:
            self._clear_drift_observation()
            return
        if self._drift_bad_since is None:
            self._drift_bad_since = now
        self._drift_bad_samples += 1
        min_samples = AMCL_DRIFT_MIN_SAMPLES if severe_drift else AMCL_MODERATE_DRIFT_MIN_SAMPLES
        dwell_s = AMCL_DRIFT_DWELL_S if severe_drift else AMCL_MODERATE_DRIFT_DWELL_S
        if self._drift_bad_samples < min_samples or now - self._drift_bad_since < dwell_s:
            return

        self.get_logger().warning(
            f"AMCL diverged from simulator ground truth ({position_error:.2f}m, "
            f"{math.degrees(yaw_error):.0f}deg); reseeding"
        )
        self._clear_drift_observation()
        self._drift_armed = False
        self._last_auto_reseed_at = now
        if not self._begin_localization():
            self._drift_armed = True
            self._last_auto_reseed_at = None

    def _clear_drift_observation(self):
        self._drift_bad_since = None
        self._drift_bad_samples = 0

    def _reset_drift_guard(self):
        self._clear_drift_observation()
        self._drift_armed = True
        self._drift_healthy_since = None
        self._last_auto_reseed_at = None

    def _cancel_retry(self):
        self._localization_started_ns = None
        if self._retry_timer is None:
            return
        self._retry_timer.cancel()
        self._retry_timer = None

    def on_configure(self, state) -> TransitionCallbackReturn:
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._pose_pub = self.create_lifecycle_publisher(PoseWithCovarianceStamped, "/initialpose", latched)
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state) -> TransitionCallbackReturn:
        self._reset_pending = False
        self._reset_drift_guard()
        self._begin_localization()
        return super().on_activate(state)

    def on_deactivate(self, state) -> TransitionCallbackReturn:
        self._reset_pending = False
        self._reset_drift_guard()
        self._cancel_retry()
        return super().on_deactivate(state)

    def _on_localize(self, _request, response):
        response.success = self._begin_localization()
        response.message = "ground-truth pose published" if response.success else "no odom yet"
        return response

    def _on_hand_placed(self, _request, response):
        response.success = True
        response.message = "ground truth needs no vouching"
        return response

    def _begin_localization(self) -> bool:
        self._localization_started_ns = self.get_clock().now().nanoseconds
        if not self._publish_pose():
            self._localization_started_ns = None
            return False
        if self._retry_timer is None:
            self._retry_timer = self.create_timer(2.0, self._publish_pose)
        return True

    def _publish_pose(self) -> bool:
        if self._last_odom is None or self._pose_pub is None:
            return False
        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "map"
        msg.pose.pose = self._last_odom.pose.pose
        msg.pose.covariance[0] = msg.pose.covariance[7] = 0.01
        msg.pose.covariance[35] = 0.01
        self._pose_pub.publish(msg)
        self.get_logger().info("published ground-truth /initialpose")
        return True


def main():
    rclpy.init()
    node = GridLocalizerSim()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
