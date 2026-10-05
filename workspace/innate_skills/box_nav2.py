# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Short, box-normal approach paths followed by Nav2 in the odom frame."""

import math
import time

import numpy as np
from innate_skills.box_marker import MarkerDock, MarkerFollower, observe_box_release

from innate.exceptions import SkillFailed


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def approach_curve(marker_xy, yaw, standoff=0.30):
    """Forward cubic with initial robot heading and final box-normal heading.

    Return None when already close and frontal. Reject tight loops rather than
    silently switching to the old diagonal approach or driving backwards.
    """
    marker = np.asarray(marker_xy, dtype=float)
    if marker.shape != (2,) or not np.isfinite(marker).all() or not math.isfinite(yaw):
        raise SkillFailed("Invalid box pose for curved approach")
    normal = np.array([math.cos(yaw), math.sin(yaw)])
    distance = float(marker @ normal)
    lateral = float(marker @ np.array([-normal[1], normal[0]]))
    if 0.18 <= distance <= standoff + 0.05 and abs(lateral) <= 0.04 and abs(wrap(yaw)) <= math.radians(12):
        return None
    goal = marker - standoff * normal
    if goal[0] < 0.12:
        raise SkillFailed("Forward box arc endpoint is too close or behind the robot; repositioning is required")
    length = float(np.linalg.norm(goal))
    if length > 3:
        raise SkillFailed("Box is outside the 3m local approach range")
    t = np.linspace(0, 1, max(41, math.ceil(length / 0.015)))[:, None]
    for start_scale, end_scale in ((0.45, 0.45), (0.3, 0.45), (0.3, 0.3), (0.6, 0.6), (0.2, 0.2)):
        a = np.array([length * start_scale, 0.0])
        b = goal - length * end_scale * normal
        xy = 3 * (1 - t) ** 2 * t * a + 3 * (1 - t) * t**2 * b + t**3 * goal
        d = 3 * (1 - t) ** 2 * a + 6 * (1 - t) * t * (b - a) + 3 * t**2 * (goal - b)
        dd = 6 * (1 - t) * (b - 2 * a) + 6 * t * (goal - 2 * b + a)
        speed = np.linalg.norm(d, axis=1)
        curvature = np.abs(d[:, 0] * dd[:, 1] - d[:, 1] * dd[:, 0]) / np.maximum(speed, 1e-9) ** 3
        # Include the configured rectangular footprint while turning, not just
        # its front extent when square to the box. Retain 2 cm plane clearance.
        heading = np.arctan2(d[:, 1], d[:, 0])
        c = np.cos(yaw - heading)
        footprint_extent = np.where(c >= 0, 0.25 * c, -0.20 * c) + 0.165 * np.abs(np.sin(yaw - heading))
        clearance = (marker - xy) @ normal
        if (
            speed.min() > 0.02
            and curvature.max() <= 10
            and np.all(clearance >= standoff - 0.005)
            and np.all(clearance >= footprint_extent + 0.02)
        ):
            return [(float(p[0]), float(p[1]), math.atan2(v[1], v[0])) for p, v in zip(xy, d, strict=True)]
    raise SkillFailed("Not enough room for a smooth box arc; move farther from the box")


def in_odom(points, origin):
    x, y, yaw = origin
    c, s = math.cos(yaw), math.sin(yaw)
    return [(x + c * px - s * py, y + s * px + c * py, wrap(yaw + a)) for px, py, a in points]


class PathSession:
    """Bounded action/service calls; scoped limits restored after action stops."""

    def __init__(self, host):
        import rclpy
        from nav2_msgs.action import FollowPath
        from rclpy.action import ActionClient

        self.host, self.ros = host, rclpy
        self.node = rclpy.create_node("box_arc_" + str(time.monotonic_ns()))
        self.client = ActionClient(self.node, FollowPath, "/follow_path")
        self.goal = None
        self.pending_goal = None
        self.saved = []
        self.result = None
        self.scan_at = None
        self.statuses = []
        from action_msgs.msg import GoalStatusArray
        from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
        from sensor_msgs.msg import LaserScan

        self.node.create_subscription(LaserScan, "/scan", self._scan, qos_profile_sensor_data)
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
        self.node.create_subscription(GoalStatusArray, "/follow_path/_action/status", self._status, qos)

    def _scan(self, msg):
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        now = self.node.get_clock().now().nanoseconds / 1e9
        if 0 <= now - stamp < 0.5:
            self.scan_at = time.monotonic()

    def _status(self, msg):
        self.statuses = [goal.status for goal in msg.status_list]

    def ready(self):
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            self.host.check_cancelled()
            self.ros.spin_once(self.node, timeout_sec=0.05)
        if any(status in (1, 2, 3) for status in self.statuses):
            raise SkillFailed("Another Nav2 path is active; box arc refused")
        self.fresh_sensors()

    def fresh_sensors(self):
        self.fresh_odom()
        if self.scan_at is None or time.monotonic() - self.scan_at > 0.75:
            raise SkillFailed("Fresh obstacle scan required for Nav2 box arc")

    def wait(self, future, timeout=3, cancellable=True):
        end = time.monotonic() + timeout
        while not future.done():
            if cancellable:
                self.host.check_cancelled()
            if time.monotonic() >= end:
                raise SkillFailed("Nav2 box approach request timed out")
            self.ros.spin_once(self.node, timeout_sec=0.05)
        return future.result()

    def service(self, kind, name, request, cancellable=True):
        client = self.node.create_client(kind, name)
        try:
            if not client.wait_for_service(timeout_sec=1):
                raise SkillFailed("Nav2 service unavailable: " + name)
            return self.wait(client.call_async(request), cancellable=cancellable)
        finally:
            self.node.destroy_client(client)

    def limits(self):
        from lifecycle_msgs.srv import GetState
        from rcl_interfaces.msg import Parameter as ParameterMsg
        from rcl_interfaces.srv import GetParameters, SetParametersAtomically
        from rclpy.parameter import Parameter

        for name in ("/controller_server", "/velocity_smoother", "/local_costmap/local_costmap"):
            response = self.service(GetState, name + "/get_state", GetState.Request())
            if response.current_state.id != 3:
                raise SkillFailed(name + " is not active")
        configs = {
            "/velocity_smoother": {
                "max_velocity": [0.03, 0.0, 0.12],
                "min_velocity": [-0.016, 0.0, -0.12],
                "max_accel": [0.2 / 3, 0.0, 0.8 / 3],
                "max_decel": [-0.4, 0.0, -2.0],
            },
            "/controller_server": {
                "InnateFollowPath.vx_max": 0.03,
                "InnateFollowPath.vx_min": 0.0,
                "InnateFollowPath.wz_max": 0.12,
                "InnateFollowPath.ax_max": 0.2 / 3,
                "InnateFollowPath.ax_min": -0.4,
                "progress_checker.required_movement_radius": 0.03,
                "progress_checker.movement_time_allowance": 75.0,
            },
        }
        for name, values in configs.items():
            keys = list(values)
            before = self.service(GetParameters, name + "/get_parameters", GetParameters.Request(names=keys)).values
            if any(v.type == 0 for v in before):
                raise SkillFailed("Nav2 carrying limits are unsupported: " + name)
            old = [ParameterMsg(name=k, value=v) for k, v in zip(keys, before, strict=True)]
            desired = [Parameter(k, value=v).to_parameter_msg() for k, v in values.items()]
            # Save before dispatch so an interrupted response still restores settings.
            self.saved.append((name, old))
            result = self.service(
                SetParametersAtomically,
                name + "/set_parameters_atomically",
                SetParametersAtomically.Request(parameters=desired),
            )
            if not result.result.successful:
                raise SkillFailed("Nav2 rejected carrying limits: " + result.result.reason)
        self.host.logger.info("[BoxArc] carrying limits applied: v=0.03m/s w=0.12rad/s accel=(0.067,0.267)")

    def fresh_odom(self):
        odom = self.host.odom
        now = self.node.get_clock().now().nanoseconds / 1e9
        if (
            odom is None
            or odom.frame_id != "odom"
            or not all(math.isfinite(v) for v in (odom.x, odom.y, odom.theta, odom.stamp))
            or not 0 <= now - odom.stamp < 0.5
        ):
            raise SkillFailed("Fresh odometry in odom frame required for box arc")
        return odom

    def follow(self, points):
        from geometry_msgs.msg import PoseStamped
        from nav2_msgs.action import FollowPath
        from nav_msgs.msg import Path

        if not self.client.wait_for_server(timeout_sec=2):
            raise SkillFailed("Nav2 FollowPath is unavailable")
        self.ready()
        self.limits()
        self.fresh_sensors()
        path = Path()
        path.header.frame_id = "odom"
        path.header.stamp = self.node.get_clock().now().to_msg()
        for x, y, yaw in points:
            p = PoseStamped()
            p.header = path.header
            p.pose.position.x, p.pose.position.y = x, y
            p.pose.orientation.z, p.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
            path.poses.append(p)
        # Allow the preceding stop timer to expire before Nav2 owns the base.
        self.host.sleep(0.15)
        self.pending_goal = self.client.send_goal_async(
            FollowPath.Goal(path=path, controller_id="InnateFollowPath", goal_checker_id="goal_checker_precise")
        )
        self.goal = self.wait(self.pending_goal)
        if not self.goal.accepted:
            raise SkillFailed("Nav2 rejected box arc")
        result = self.result = self.goal.get_result_async()
        start = time.monotonic()
        while not result.done():
            self.host.check_cancelled()
            self.fresh_sensors()
            if time.monotonic() - start > 225:
                raise SkillFailed("Nav2 box arc exceeded 225 seconds")
            self.ros.spin_once(self.node, timeout_sec=0.05)
        reply = result.result()
        if reply.status != 4:
            raise SkillFailed(f"Nav2 box arc failed: status={reply.status}, result={reply.result}")
        odom = self.fresh_odom()
        x, y, yaw = points[-1]
        if math.hypot(odom.x - x, odom.y - y) > 0.10 or abs(wrap(odom.theta - yaw)) > math.radians(12):
            raise SkillFailed("Nav2 ended outside the frontal box staging pose")
        self.host.logger.info(f"[BoxArc] arrived frontal in {time.monotonic() - start:.2f}s")

    def close(self):
        from lifecycle_msgs.srv import ChangeState
        from rcl_interfaces.srv import SetParametersAtomically

        stop_confirmed = self.pending_goal is None
        errors = []
        try:
            # A Stop can arrive before the action server acknowledges our goal.
            if self.goal is None and self.pending_goal is not None:
                self.goal = self.wait(self.pending_goal, cancellable=False)
            if self.goal is not None and self.goal.accepted:
                result = self.result or self.goal.get_result_async()
                if not result.done():
                    self.wait(self.goal.cancel_goal_async(), cancellable=False)
                    self.wait(result, cancellable=False)
            stop_confirmed = True
        except Exception as error:
            errors.append(str(error))
            # Do not leave a late-accepted goal driving after skill cancellation.
            # Deactivation also rejects any subsequently delivered goal.
            request = ChangeState.Request()
            request.transition.id = 4  # TRANSITION_DEACTIVATE
            try:
                response = self.service(ChangeState, "/controller_server/change_state", request, cancellable=False)
                stop_confirmed = response.success
            except Exception as shutdown_error:
                errors.append(str(shutdown_error))
            self.host.logger.error("[BoxArc] cancellation not acknowledged; controller deactivation attempted")
        finally:
            self.host.mobility.stop()
            if stop_confirmed:
                for name, parameters in reversed(self.saved):
                    try:
                        reply = self.service(
                            SetParametersAtomically,
                            name + "/set_parameters_atomically",
                            SetParametersAtomically.Request(parameters=parameters),
                            cancellable=False,
                        )
                        if not reply.result.successful:
                            raise SkillFailed(reply.result.reason)
                    except Exception as error:
                        errors.append("restore " + name + ": " + str(error))
            self.node.destroy_node()
        if errors:
            raise SkillFailed("Nav2 box arc cleanup needs attention: " + "; ".join(errors))


def dock_with_arc(host, config, final_distance=0.19):
    dock = MarkerDock(host, config)
    follower = MarkerFollower(host.mobility)
    follower.max_angular = 0.12
    dock.search_speed = -0.10
    dock.search_timeout = 175
    try:
        dock._find_marker(follower)
    finally:
        follower._stop()
    session = PathSession(host)
    try:
        session.fresh_odom()
        x, y, yaw = observe_box_release(host, dock.detector, 0.0, 0.0, return_heading=True)
        odom = session.fresh_odom()
        # Keep Nav2 outside its 25 cm front footprint; visual docking covers
        # the remaining short distance to the taught release position.
        if not math.isfinite(final_distance) or final_distance < 0.19:
            raise SkillFailed("Invalid final box docking distance")
        standoff = max(0.30, final_distance + 0.05)
        host.logger.info(
            f"[BoxArc] planning marker=({x:.3f},{y:.3f}) yaw={math.degrees(yaw):.1f}deg "
            f"handoff={standoff:.3f}m final={final_distance:.3f}m"
        )
        points = approach_curve((x, y), yaw, standoff)
        if points is None:
            host.logger.info("[BoxArc] already close and frontal; using final visual approach")
            return
        path = in_odom(points, (odom.x, odom.y, odom.theta))
        host.logger.info(
            f"[BoxArc] marker=({x:.3f},{y:.3f}) yaw={math.degrees(yaw):.1f}deg samples={len(path)} goal={path[-1]}"
        )
        session.follow(path)
    finally:
        session.close()
