# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Robot-state provision for skill execution: the state subscriptions,
interface injection, and the 50 Hz continuous-update thread. Ambient reads
land on the current_* accessors, converting the newest cached message."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import OccupancyGrid
from nav_msgs.msg import Odometry as OdometryMsg
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import BatteryState, JointState, LaserScan
from std_msgs.msg import String

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.skills.types import _DEFAULT_STATE_GRACE_S, InterfaceType, RobotStateType, _state_grace_s
from brain_client.state.arm import Arm
from brain_client.state.battery import Battery
from brain_client.state.head import HeadState
from brain_client.state.image import DepthMap, MainImage, WristImage
from brain_client.state.joint_states import JointStates
from brain_client.state.lidar import Lidar
from brain_client.state.map import Map
from brain_client.state.odometry import Odometry
from brain_client.state.pose import Pose

if TYPE_CHECKING:
    from brain_client.robot.spatial_memory import SpatialMemory


class RobotStateProvider:
    def __init__(
        self,
        node,
        camera_node,
        *,
        manipulation,
        mobility,
        head,
        memory: SpatialMemory,
        head_current_position_topic: str,
    ):
        self._node = node
        self._logger = node.get_logger()
        self._camera = camera_node
        self._manipulation = manipulation
        self._mobility = mobility
        self._head = head
        self._memory = memory
        self._head_current_position_topic = head_current_position_topic

        self.last_odom = None
        self.last_map = None
        self.last_head_position = None
        self.last_joint_states = None
        self.last_battery = None
        self.last_amcl_pose = None
        self.last_scan = None
        self._lidar_cache = None  # (msg, Lidar) of the last converted scan
        # (msg, Map) of the last converted map — a fresh Map per 50 Hz tick
        # would discard Map.grid's cached_property and re-decode the whole
        # grid on every skill read
        self._map_cache = None
        # (jpeg, Image) of the last converted frame per camera — the b64
        # encode is far too expensive to redo at 50 Hz for a ~15 Hz camera
        self._main_image_cache = None
        self._wrist_image_cache = None

        self._odom_sub = None
        self._map_sub = None
        self._head_position_sub = None
        self._joint_states_sub = None
        self._battery_sub = None
        self._amcl_pose_sub = None
        self._scan_sub = None
        # Gate high-rate feeds with this flag, never by destroying subscriptions:
        # destroying one the executor already selected as "ready" crashes the
        # process (InvalidHandle -> rmw_zenoh SIGABRT), easiest to hit right
        # after a cancel. Those subs live on manipulation's private node, parked
        # between skills — always-alive feeds (~400 msgs/s) would otherwise
        # cost ~half a Jetson core while idle. The low-rate latched map and pose
        # live on the always-spinning action-server node instead (see start()).
        self._active = False
        self._warned_missing = set()  # warn once per missing state, not at 50 Hz

        # (skill, injection pairs) holding the 50 Hz slot; None when idle
        self._current_skill = None
        self._current_skill_lock = threading.Lock()
        # parents suspended while a chained child holds the 50 Hz slot
        self._skill_stack = []
        self._state_update_thread = None
        self._state_update_stop_event = threading.Event()

        # feed enum -> snapshot accessor; see _state_getters
        self._state_getter_map = {
            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64: self.current_main_image,
            RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64: self.current_wrist_image,
            RobotStateType.LAST_DEPTH_IMAGE: self.current_depth,
            RobotStateType.LAST_ODOM: self.current_odom,
            RobotStateType.LAST_MAP: self.current_map,
            RobotStateType.LAST_JOINT_STATES: self.current_joint_states,
            RobotStateType.LAST_BATTERY: self.current_battery,
            RobotStateType.LAST_HEAD_POSITION: self.current_head_position,
            RobotStateType.LAST_POSE: self.current_pose,
            RobotStateType.LAST_LIDAR: self.current_lidar,
            RobotStateType.LAST_ARM: self.current_arm,
        }

    # --- interface injection (at skill load + before execution) ---
    def inject_required_interfaces(self, skill) -> None:
        """Inject only the interfaces declared by the skill via Interface descriptors."""
        for interface_type in skill.declared_interface_types():
            if interface_type == InterfaceType.MANIPULATION:
                skill.inject_interface(interface_type, self._manipulation)
            elif interface_type == InterfaceType.MOBILITY:
                skill.inject_interface(interface_type, self._mobility)
            elif interface_type == InterfaceType.HEAD:
                skill.inject_interface(interface_type, self._head)
            elif interface_type == InterfaceType.MEMORY:
                skill.inject_interface(interface_type, self._memory)

    # --- subscriptions ---
    def start_subscriptions(self) -> None:
        """Enable robot-state feeds for skill execution (subscriptions are created once)."""
        self._warned_missing.clear()
        # start() first: it spins up the private executor that also dispatches
        # the robot-state subscriptions below
        self._manipulation.start()
        self._active = True
        if self._odom_sub is not None:
            return
        feed_node = self._manipulation.node
        # The navigation map and localized pose are latched: they may publish
        # only once, before a skill activates these parked subscriptions. Match
        # their transient-local durability so every run receives the retained
        # state instead of waiting forever for a new sample.
        latched_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._odom_sub = feed_node.create_subscription(OdometryMsg, "/odom", self._on_odom, 10)
        self._map_sub = self._node.create_subscription(OccupancyGrid, "/map", self._on_map, latched_qos)
        self._head_position_sub = feed_node.create_subscription(
            String, self._head_current_position_topic, self._on_head_position, 10
        )
        self._joint_states_sub = feed_node.create_subscription(JointState, "/joint_states", self._on_joint_states, 10)
        self._battery_sub = feed_node.create_subscription(BatteryState, "/battery_state", self._on_battery, 10)
        self._amcl_pose_sub = self._node.create_subscription(
            PoseWithCovarianceStamped, "/amcl_pose", self._on_amcl_pose, latched_qos
        )
        # the lidar driver publishes with sensor-data QoS (best effort); a
        # reliable subscription would never match it
        self._scan_sub = feed_node.create_subscription(LaserScan, "/scan", self._on_scan, qos_profile_sensor_data)

    def stop_subscriptions(self) -> None:
        """Deactivate feeds — subscriptions are deliberately NOT destroyed
        (see __init__); callbacks early-return and the private executor parks."""
        self._active = False
        self._manipulation.stop()
        self.last_odom = None
        self.last_head_position = None
        self.last_joint_states = None
        self.last_battery = None
        self.last_scan = None
        self._lidar_cache = None
        self._main_image_cache = None
        self._wrist_image_cache = None

    def _on_odom(self, msg: OdometryMsg) -> None:
        if self._active:
            self.last_odom = msg

    def _on_map(self, msg: OccupancyGrid) -> None:
        # Always-on low-rate feed: map switches while no skill is running must
        # replace the retained grid before the next skill reads it.
        self.last_map = msg

    def _on_joint_states(self, msg: JointState) -> None:
        if self._active:
            self.last_joint_states = msg

    def _on_battery(self, msg: BatteryState) -> None:
        if self._active:
            self.last_battery = msg

    def _on_amcl_pose(self, msg: PoseWithCovarianceStamped) -> None:
        # Same lifecycle as the map. Keeping localization current avoids serving
        # the previous skill's pose while the high-rate executor is parked.
        self.last_amcl_pose = msg

    def _on_scan(self, msg: LaserScan) -> None:
        if self._active:
            self.last_scan = msg

    def _on_head_position(self, msg: String) -> None:
        if not self._active:
            return
        try:
            self.last_head_position = json.loads(msg.data)
        except Exception as e:
            self._logger.error(f"Failed to parse head position JSON: {e}")

    def _warn_missing(self, state_name: str) -> None:
        if state_name not in self._warned_missing:
            self._warned_missing.add(state_name)
            self._logger.warn(f"Skill requires {state_name} but none available (yet); will inject when it arrives.")

    # --- continuous updates ---
    def begin_continuous_updates(self, skill) -> None:
        """Start (or hand over) the 50 Hz state feed. Nesting-safe: a parent
        holding the slot is suspended and resumes when the child ends.

        The declared-feed set is fixed once the run is wired, so the
        (state, getter) pairs are computed here, once — not by re-walking the
        sub-skill tree on every 50 Hz tick."""
        entry = (skill, self._injection_pairs(skill))
        with self._current_skill_lock:
            if self._current_skill is not None:
                self._skill_stack.append(self._current_skill)
                self._current_skill = entry
                return  # update thread already running
            self._current_skill = entry
        self._state_update_stop_event.clear()
        self._state_update_thread = threading.Thread(target=self._state_update_thread_func, daemon=True)
        self._state_update_thread.start()

    def end_continuous_updates(self) -> None:
        """End the current skill's state feed, resuming a suspended parent if any."""
        with self._current_skill_lock:
            if self._skill_stack:
                self._current_skill = self._skill_stack.pop()
                return  # parent takes the slot back; thread keeps running
            self._current_skill = None
        if self._state_update_thread is not None:
            self._state_update_stop_event.set()
            self._state_update_thread.join(timeout=1.0)
            self._state_update_thread = None

    def _state_update_thread_func(self) -> None:
        """Continuously refresh robot state for the running skill (~50 Hz)."""
        while not self._state_update_stop_event.is_set():
            with self._current_skill_lock:
                if self._current_skill is not None:
                    skill, pairs = self._current_skill
                    try:
                        self._inject(skill, pairs)
                    except Exception as e:
                        self._logger.error(f"Error in continuous state update: {e}")
            self._state_update_stop_event.wait(0.02)

    def _state_getters(self) -> dict:
        # static per provider (also the test seam: tests stub this per instance)
        return self._state_getter_map

    def wait_for_required_states(self, skill) -> None:
        """Block, bounded per feed, until every required state has a first
        value, then re-inject.

        Feeds start at run start, so the first message is at least one
        publish period away — without this, a required read in execute()'s
        opening lines would fail cold. Anything still missing afterwards
        fails the run (Skill.missing_required_robot_states), never execute().

        Optional declarations (``| None``, legacy descriptors) never block
        the start — a dead feed would stall every run for nothing. They are
        injected as messages land (the 50 Hz refresh); a skill wanting one
        early waits itself: ``self.wait_for(lambda: self.image)``."""
        getters = self._state_getters()
        start = time.monotonic()
        # per-feed grace bounds come from the spec table (types._feed_specs)
        deadlines = {
            state_type: start + _state_grace_s().get(state_type, _DEFAULT_STATE_GRACE_S)
            for state_type in skill.required_robot_state_types()
            if state_type in getters
        }
        pending = dict(deadlines)
        while pending and not skill.cancelled:
            now = time.monotonic()
            pending = {t: deadline for t, deadline in pending.items() if getters[t]() is None and now < deadline}
            if pending:
                time.sleep(0.05)
        timed_out = [t.name for t in deadlines if getters[t]() is None]
        if timed_out:
            self._logger.warn(f"Required state still missing after its grace period: {', '.join(timed_out)}")
        self.update_skill_robot_state(skill)

    # --- snapshot accessors ---
    # Ambient state reads (skill.battery, skill.odom, ...) land here: each
    # call converts the newest cached message, so every read is as fresh as
    # the topic.
    def current_main_image(self) -> MainImage | None:
        # memoized per frame, like lidar: re-encoding ~100 KB JPEGs to base64
        # at 50 Hz is ~10 MB/s of allocations for frames that change at ~15 Hz
        jpeg = self._camera.last_main_camera_jpeg
        if jpeg is None:
            return None
        cached = self._main_image_cache
        if cached is not None and cached[0] is jpeg:
            return cached[1]
        image = MainImage.from_jpeg(jpeg)
        self._main_image_cache = (jpeg, image)
        return image

    def current_wrist_image(self) -> WristImage | None:
        jpeg = self._camera.last_wrist_camera_jpeg
        if jpeg is None:
            return None
        cached = self._wrist_image_cache
        if cached is not None and cached[0] is jpeg:
            return cached[1]
        image = WristImage.from_jpeg(jpeg)
        self._wrist_image_cache = (jpeg, image)
        return image

    def current_depth(self) -> DepthMap | None:
        depth = self._camera.last_depth_image
        # view-cast: same buffer, feed-identifying type
        return None if depth is None else depth.view(DepthMap)

    def current_odom(self) -> Odometry | None:
        msg = self.last_odom
        if msg is None:
            return None
        pos = msg.pose.pose.position
        twist = msg.twist.twist
        return Odometry(
            x=pos.x,
            y=pos.y,
            theta=quaternion_to_yaw(msg.pose.pose.orientation),
            linear_velocity=twist.linear.x,
            angular_velocity=twist.angular.z,
            stamp=msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9,
            frame_id=msg.header.frame_id,
            child_frame_id=msg.child_frame_id,
            # .raw builds lazily from the message, so
            # high-rate reads pay nothing for the full-fidelity dict
            raw_source=msg,
        )

    def current_map(self) -> Map | None:
        msg = self.last_map
        if msg is None:
            return None
        # memoized per message, like lidar: a fresh Map every 50 Hz tick would
        # throw away Map.grid's cached_property, making every skill read of
        # .grid re-decode the whole grid
        cached = self._map_cache
        if cached is not None and cached[0] is msg:
            return cached[1]
        # cheap: the grid itself decodes lazily on Map.grid access
        current = Map(
            resolution=msg.info.resolution,
            width=msg.info.width,
            height=msg.info.height,
            origin_x=msg.info.origin.position.x,
            origin_y=msg.info.origin.position.y,
            origin_theta=quaternion_to_yaw(msg.info.origin.orientation),
            stamp=msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9,
            frame_id=msg.header.frame_id,
            raw_source=msg,
        )
        self._map_cache = (msg, current)
        return current

    def current_joint_states(self) -> JointStates | None:
        msg = self.last_joint_states
        if msg is None:
            return None
        return JointStates(
            name=tuple(msg.name),
            position=tuple(msg.position),
            velocity=tuple(msg.velocity),
            effort=tuple(msg.effort),
        )

    def current_battery(self) -> Battery | None:
        msg = self.last_battery
        if msg is None:
            return None
        return Battery(
            percentage=msg.percentage,
            voltage=msg.voltage,
            current=msg.current,
            charging=msg.power_supply_status == BatteryState.POWER_SUPPLY_STATUS_CHARGING,
        )

    def current_head_position(self) -> HeadState | None:
        payload = self.last_head_position
        if payload is None:
            return None
        pitch = payload.get("current_position")
        if pitch is None:
            return None
        return HeadState(
            pitch_degrees=pitch,
            min_degrees=payload.get("min_angle"),
            max_degrees=payload.get("max_angle"),
            default_degrees=payload.get("default_angle"),
            raw_source=payload,
        )

    def current_pose(self) -> Pose | None:
        msg = self.last_amcl_pose
        if msg is None:
            return None
        pose = msg.pose.pose
        return Pose(
            x=pose.position.x,
            y=pose.position.y,
            theta=quaternion_to_yaw(pose.orientation),
            stamp=msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9,
            frame_id=msg.header.frame_id,
        )

    def current_lidar(self) -> Lidar | None:
        msg = self.last_scan
        if msg is None:
            return None
        # memoized per message: tuple(msg.ranges) copies ~1100 floats at 50 Hz
        # for a ~10 Hz scan
        cached = self._lidar_cache
        if cached is not None and cached[0] is msg:
            return cached[1]
        lidar = Lidar(
            ranges=tuple(msg.ranges),
            angle_min=msg.angle_min,
            angle_increment=msg.angle_increment,
            range_min=msg.range_min,
            range_max=msg.range_max,
            stamp=msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9,
            frame_id=msg.header.frame_id,
        )
        self._lidar_cache = (msg, lidar)
        return lidar

    def current_arm(self) -> Arm | None:
        # last_fk_pose, not the pose property: pose blocks waiting for a
        # first fix and raises on None — both wrong at 50 Hz
        fk = self._manipulation.last_fk_pose
        if fk is None:
            return None
        js = self.last_joint_states
        gripper = js.position[5] if js is not None and len(js.position) > 5 else None
        return Arm.from_fk(fk, gripper=gripper)

    # --- state injection ---
    def _injection_pairs(self, skill) -> list[tuple[RobotStateType, Callable[[], object]]]:
        """(state, getter) for every declared feed, sub-skills included."""
        getters = self._state_getters()
        return [(t, getters[t]) for t in skill.declared_robot_state_types() if t in getters]

    def update_skill_robot_state(self, skill) -> None:
        """Inject a current value for every state the skill declared (one-shot
        path: the 50 Hz thread injects via precomputed pairs instead)."""
        self._inject(skill, self._injection_pairs(skill))

    def _inject(self, skill, pairs) -> None:
        to_inject = {}
        for state_type, getter in pairs:
            value = getter()
            if value is not None:
                to_inject[state_type.value] = value
            else:
                self._warn_missing(state_type.name)
        if to_inject:
            skill.update_robot_state(**to_inject)
