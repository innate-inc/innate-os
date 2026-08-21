#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import glob
import json
import os
import subprocess
import threading
import time
import traceback

import rclpy

# TF2 imports for transform lookup
import tf2_ros
from action_msgs.msg import GoalStatus, GoalStatusArray
from action_msgs.srv import CancelGoal
from brain_messages.srv import ChangeMap, ChangeNavigationMode, DeleteMap, SaveMap
from geometry_msgs.msg import TransformStamped
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import ChangeState, GetState
from nav2_msgs.srv import LoadMap, SetInitialPose
from nav2_simple_commander.robot_navigator import BasicNavigator
from nav_msgs.msg import Odometry
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import PointCloud2
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger

from mars_nav.navigation_policy import (
    MAPPING_MODES,
    MAPPING_SPEED_LIMIT_SERVICE,
    VALID_NAVIGATION_MODES,
    NavigationMode,
    configure_only_nodes,
    modes_nodes,
    skip_cleanup_nodes,
    starts_new_mapping_session,
)
from mars_nav.service_utils import call_service, get_node_state, transition_node

# TODO: move this into launch file?
map_server_node = "navigation_map_server"
bt_node = "bt_navigator"

NAV_CANCEL_SERVICE = "/internal_navigate_to_pose/_action/cancel_goal"


class ModeManager(Node):
    def __init__(self):
        self.log_num = 0
        super().__init__("mode_manager")

        # Service clients created in callbacks need a re-entrant callback group,
        # otherwise the response callback can be blocked by the running service callback.
        # self._calls_going_outside_group = MutuallyExclusiveCallbackGroup()
        # self._internal_callbacks_group = MutuallyExclusiveCallbackGroup()
        self._calls_going_outside_group = ReentrantCallbackGroup()
        self._internal_callbacks_group = ReentrantCallbackGroup()

        # Will be set in main() after adding this node to an executor.
        self._executor = None

        # main() binds this to the in-process NavigateToPoseRouter.  DDS remains
        # the public status transport, while this sink closes admission before
        # a lifecycle teardown can race topic delivery.
        self._navigation_mode_sink = None
        self._navigation_pending_goals_getter = None

        # Lock to prevent concurrent mode changes

        self._mode_change_lock = threading.Lock()

        # Pre-create service clients and store in dictionary
        self._service_clients = {}

        # Service to switch modes
        self.mode_service = self.create_service(
            ChangeNavigationMode,
            "/nav/change_mode",
            self.change_mode_callback,
            callback_group=self._internal_callbacks_group,
        )

        # Cancel-all for NavigateToPose goals (skill nav and /goal_pose alike
        # both terminate at the internal bt_navigator action). Goal activity is
        # tracked from the action's status topic, not service availability.
        self._nav_active_goals = 0
        self._service_clients[NAV_CANCEL_SERVICE] = self.create_client(
            CancelGoal, NAV_CANCEL_SERVICE, callback_group=self._calls_going_outside_group
        )
        self._service_clients[MAPPING_SPEED_LIMIT_SERVICE] = self.create_client(
            SetBool,
            MAPPING_SPEED_LIMIT_SERVICE,
            callback_group=self._calls_going_outside_group,
        )
        self._nav_status_sub = self.create_subscription(
            GoalStatusArray,
            "/internal_navigate_to_pose/_action/status",
            self._nav_status_callback,
            QoSProfile(
                depth=1, reliability=QoSReliabilityPolicy.RELIABLE, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL
            ),
            callback_group=self._internal_callbacks_group,
        )
        self.cancel_navigation_service = self.create_service(
            Trigger,
            "/nav/cancel_navigation",
            self.cancel_navigation_callback,
            callback_group=self._internal_callbacks_group,
        )
        self.reset_mapping_service = self.create_service(
            Trigger,
            "/nav/reset_mapping",
            self.reset_mapping_callback,
            callback_group=self._internal_callbacks_group,
        )

        # Service to change maps in navigation mode. Same reentrant group as
        # the mode service: both drive lifecycle transitions on the same nodes
        # and are serialized by _mode_change_lock (in the default group the
        # long-blocking callback would also starve the status timer and the
        # relocalization client's response).
        self.map_service = self.create_service(
            ChangeMap,
            "/nav/change_navigation_map",
            self.change_map_callback,
            callback_group=self._internal_callbacks_group,
        )

        # Service to save current map in mapping mode
        self.save_map_service = self.create_service(SaveMap, "/nav/save_map", self.save_map_callback)

        # Service to delete a map (same group + lock as mode/map changes: it
        # reads current_mode and can reassign current_map).
        self.delete_map_service = self.create_service(
            DeleteMap, "/nav/delete_map", self.delete_map_callback, callback_group=self._internal_callbacks_group
        )

        # Publisher to announce current mode.  The router and late-starting
        # skills need the current value immediately; a volatile publisher is
        # incompatible with their transient-local subscriptions.
        mode_qos = QoSProfile(
            depth=1,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.mode_publisher = self.create_publisher(String, "/nav/current_mode", mode_qos)

        # Publisher to announce available maps
        self.maps_publisher = self.create_publisher(String, "/nav/available_maps", 10)

        # Publisher to announce current map
        self.current_map_publisher = self.create_publisher(String, "/nav/current_map", 10)

        # Each successful save as {"map": yaml name, "stamp": epoch seconds};
        # brain_client promotes its staged mapping memories on it. Latched so a
        # recorder respawning across the save still hears it — hence the stamp,
        # which lets it tell that replay from an old save's.
        self.map_saved_publisher = self.create_publisher(
            String,
            "/nav/map_saved",
            QoSProfile(
                depth=1,
                reliability=QoSReliabilityPolicy.RELIABLE,
                durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )

        # Identity of the live SLAM session as {"started": epoch seconds},
        # published on each entry into mapping mode. Latched so a brain_client
        # respawning mid-tour can tell whether the memory stage it finds on
        # disk was built by this very session (adopt) or an earlier one (wipe).
        self._mapping_session_started = None
        self.mapping_session_publisher = self.create_publisher(
            String,
            "/nav/mapping_session",
            QoSProfile(
                depth=1,
                reliability=QoSReliabilityPolicy.RELIABLE,
                durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )

        # Pre-create service clients for all nodes and service types we'll use
        self._init_service_clients()

        # Use environment variable if set, otherwise construct from HOME
        mars_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))

        # State directory — groups maps, last_mode, and last_map together
        state_dir = os.path.join(mars_root, "data")

        # Maps directory
        self.maps_dir = os.path.join(state_dir, "maps")

        # Mode persistence file
        self.mode_file = os.path.join(state_dir, ".last_mode")

        # Map persistence file
        self.map_file = os.path.join(state_dir, ".last_map")

        # BasicNavigator for map operations
        self.navigator = None

        # Discover available maps first (needed for loading last map)
        self.available_maps = self.discover_maps()

        # Load last mode or default to navigation
        self.current_mode = self.load_last_mode()

        # Load last map or default to home.yaml (must be after discovering maps)
        self.current_map = self.load_last_map()

        # Timer to publish current mode and maps
        self.timer = self.create_timer(1, self.publish_status)

        # Client to re-trigger grid_localizer after map switches (a switch
        # otherwise carries the previous map's AMCL pose along silently).
        # Reentrant group: change_map_callback blocks the default group while
        # it waits, so a default-group client could never receive its response.
        self._localize_client = self.create_client(Trigger, "/localize", callback_group=self._internal_callbacks_group)
        # Direct AMCL seeding for the mapping->navigation handoff: AMCL can
        # accept a pose and still drop it internally on a TF race, so the
        # handoff re-seeds until map->base_link actually appears.
        self._amcl_seed_client = self.create_client(
            SetInitialPose, "/set_initial_pose", callback_group=self._calls_going_outside_group
        )
        # (map->base_link TransformStamped, monotonic capture time): where the
        # robot ended its last mapping session, in the slam session's frame.
        self._last_mapping_pose = None
        # grid_localizer's status feed: lets the post-switch relocalization
        # wait for confirmation instead of guessing when the new map landed.
        self._last_localization_status = ("", 0.0)
        # Bumped (under _mode_change_lock) by every mode/map/current_map
        # mutation; a relocalization waiter holding an older generation knows
        # it has been superseded even if the newer switch already released
        # the lock between the waiter's polls.
        self._switch_generation = 0
        self._localization_status_sub = self.create_subscription(
            String,
            "/localization/status",
            self._localization_status_cb,
            10,
            callback_group=self._internal_callbacks_group,
        )

        # One-shot check: the costmaps' camera voxel layer is silently inert
        # when /mars/main_camera/points never publishes (e.g. missing stereo
        # calibration). Warn once so lidar-only operation is visible.
        self._camera_points_seen = False
        self._camera_points_sub = self.create_subscription(
            PointCloud2, "/mars/main_camera/points", self._camera_points_cb, qos_profile_sensor_data
        )
        self._camera_check_timer = self.create_timer(30.0, self._check_camera_obstacle_source)

        # --- TF2: Mapping pose publisher ---
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        self.mapping_pose_pub = self.create_publisher(Odometry, "/mapping_pose", 10)
        # Subscribe to odometry topic (for mapping_pose publishing)
        self.odom_sub = self.create_subscription(
            Odometry,
            "/odom",
            self.odom_callback,
            20,  # queue size
        )

        self.get_logger().info("Mode Manager starting with map management capabilities.")
        self.get_logger().info(
            '- Call /nav/change_mode service to switch modes ("navigation", "mapping", '
            '"autonomous_mapping", or "mapfree")'
        )
        self.get_logger().info(
            "- Call /nav/change_navigation_map service to change map (restarts navigation if running)"
        )
        self.get_logger().info(
            "- Call /nav/save_map service to save current map with new name (mapping mode only, set overwrite=true to replace existing maps)"
        )
        self.get_logger().info(
            "- Call /nav/delete_map service to delete a saved map (cannot delete active map while navigation is running)"
        )
        self.get_logger().info(f"- Current mode: {self.current_mode} (loaded from persistence)")
        self.get_logger().info(f"- Current map: {self.current_map} (loaded from persistence)")
        self.get_logger().info(f"- Available maps: {self.available_maps}")

        # Auto-start in the saved mode after a short delay
        self.startup_timer = self.create_timer(3.0, self.auto_start_mode, callback_group=self._internal_callbacks_group)

        # Lifecycle watchdog: launch respawns crashed nav2 nodes (see
        # controller_server in navigation.launch.py), but a respawned
        # lifecycle node comes back UNCONFIGURED and useless. Re-drive it for
        # the current mode. Armed after the first successful mode startup so
        # it never races the initial bringup.
        self._lifecycle_watchdog_armed = False
        self._watchdog_busy = False
        self._lifecycle_watchdog_timer = self.create_timer(
            5.0, self._lifecycle_watchdog, callback_group=self._internal_callbacks_group
        )

    def odom_callback(self, msg):
        # Both mapping modes use slam_toolbox as the map->odom authority.
        if getattr(self, "current_mode", None) not in MAPPING_MODES:
            return
        try:
            tf_time = rclpy.time.Time()
            tf: TransformStamped = self.tf_buffer.lookup_transform("map", "base_link", tf_time)
            self._last_mapping_pose = (tf, time.monotonic())
            odom_msg = Odometry()
            # Stamp the pose with the TF sample it actually represents. Using
            # the incoming odometry time would make a frozen SLAM transform
            # look fresh as long as wheel odometry kept arriving.
            odom_msg.header.stamp = tf.header.stamp
            odom_msg.header.frame_id = "map"
            odom_msg.child_frame_id = "base_link"
            odom_msg.pose.pose.position.x = tf.transform.translation.x
            odom_msg.pose.pose.position.y = tf.transform.translation.y
            odom_msg.pose.pose.position.z = tf.transform.translation.z
            odom_msg.pose.pose.orientation = tf.transform.rotation
            # Set covariance to zeros (or small value if desired)
            odom_msg.pose.covariance = [0.0] * 36
            # Set twist to zero
            odom_msg.twist.twist.linear.x = 0.0
            odom_msg.twist.twist.linear.y = 0.0
            odom_msg.twist.twist.linear.z = 0.0
            odom_msg.twist.twist.angular.x = 0.0
            odom_msg.twist.twist.angular.y = 0.0
            odom_msg.twist.twist.angular.z = 0.0
            odom_msg.twist.covariance = [0.0] * 36
            self.mapping_pose_pub.publish(odom_msg)
        except Exception as e:
            self.get_logger().warn(f"[mapping_pose] TF lookup failed in odom_callback: {e}")

    def discover_maps(self):
        """Discover available map files in the maps directory"""
        map_files = []
        try:
            # Look for .yaml files in the maps directory
            yaml_pattern = os.path.join(self.maps_dir, "*.yaml")
            yaml_files = glob.glob(yaml_pattern)

            for yaml_file in yaml_files:
                # Extract just the filename
                map_name = os.path.basename(yaml_file)
                map_files.append(map_name)

            self.get_logger().info(f"Discovered {len(map_files)} maps: {map_files}")
        except Exception as e:
            self.get_logger().error(f"Error discovering maps: {e}")

        return sorted(map_files)

    def load_last_mode(self):
        """Load the last used mode from file, default to navigation"""
        try:
            if os.path.exists(self.mode_file):
                with open(self.mode_file) as f:
                    saved_mode = f.read().strip()
                    if saved_mode in VALID_NAVIGATION_MODES:
                        self.get_logger().info(f"Loaded last mode: {saved_mode}")
                        return saved_mode
            # Default to navigation mode
            self.get_logger().info("No saved mode found, defaulting to navigation")
            return "navigation"
        except Exception as e:
            self.get_logger().error(f"Error loading last mode: {e}, defaulting to navigation")
            return "navigation"

    def load_last_map(self):
        """Load the last used map from file, default to home.yaml"""
        try:
            if os.path.exists(self.map_file):
                with open(self.map_file) as f:
                    saved_map = f.read().strip()
                    # Validate that the saved map exists
                    if saved_map and saved_map in self.available_maps:
                        self.get_logger().info(f"Loaded last map: {saved_map}")
                        return saved_map
                    else:
                        self.get_logger().warning(f"Saved map '{saved_map}' not found, defaulting to home.yaml")

            # Default to home.yaml
            default_map = None
            if default_map in self.available_maps:
                self.get_logger().info(f"No saved map found, defaulting to {default_map}")
                return default_map
            elif self.available_maps:
                # If home.yaml doesn't exist, use the first available map
                first_map = self.available_maps[0]
                self.get_logger().info(f"Default map '{default_map}' not found, using first available: {first_map}")
                return first_map
            else:
                # No maps available, return None
                self.get_logger().warning("No maps available")
                return None
        except Exception as e:
            self.get_logger().error(f"Error loading last map: {e}")
            return None

    def save_last_mode(self, mode):
        """Save the current mode to file for persistence"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.mode_file), exist_ok=True)
            with open(self.mode_file, "w") as f:
                f.write(mode)
            self.get_logger().debug(f"Saved mode: {mode}")
        except Exception as e:
            self.get_logger().error(f"Error saving mode: {e}")

    def save_last_map(self, map_name):
        """Save the current map to file for persistence; None clears the saved map"""
        try:
            if map_name is None:
                if os.path.exists(self.map_file):
                    os.remove(self.map_file)
                self.get_logger().debug("Cleared saved map")
                return
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.map_file), exist_ok=True)
            with open(self.map_file, "w") as f:
                f.write(map_name)
            self.get_logger().debug(f"Saved map: {map_name}")
        except Exception as e:
            self.get_logger().error(f"Error saving map: {e}")

    def auto_start_mode(self):
        """Auto-start the mode manager in the last saved mode"""
        self.get_logger().info("Mode Manager auto-starting")
        self.startup_timer.cancel()  # One-time execution

        self._cleanup_orphaned_processes()

        # Check if we want to start navigation but have no maps available
        # In this case, automatically switch to mapping mode
        if self.current_mode == "navigation" and not self.available_maps:
            self.get_logger().warn("No maps available for navigation - automatically starting in mapping mode")
            self.get_logger().info("Create a map first, then switch to navigation mode")
            self.current_mode = "mapping"

        if self.current_mode in VALID_NAVIGATION_MODES:
            self.get_logger().info(f"Auto-starting in {self.current_mode} mode...")
            # Simulate a service request
            request = ChangeNavigationMode.Request()
            request.mode = self.current_mode
            response = ChangeNavigationMode.Response()
            self.change_mode_callback(request, response, first_start=True)

    def publish_status(self):
        """Publish current mode, available maps, and current map"""
        # self.log_num += 1
        # if not (self.log_num % 10):
        # self.get_logger().info("Publishing status every second....", throttle_duration_sec = 10)
        # Update the co-located router synchronously before the DDS publish.
        # In particular, `switching` must close admission before cancellation
        # checks and lifecycle teardown begin.
        if self._navigation_mode_sink is not None:
            try:
                self._navigation_mode_sink(self.current_mode)
            except Exception as e:
                self.get_logger().error(f"Failed to synchronize navigation admission: {e}")

        # Publish current mode for all other consumers and late subscribers.
        mode_msg = String()
        mode_msg.data = self.current_mode
        self.mode_publisher.publish(mode_msg)

        # Publish available maps as JSON
        maps_msg = String()
        maps_msg.data = json.dumps({"available_maps": self.available_maps})
        self.maps_publisher.publish(maps_msg)

        # Publish current map
        current_map_msg = String()
        current_map_msg.data = self.current_map if self.current_map is not None else ""
        self.current_map_publisher.publish(current_map_msg)

    def set_navigation_mode_sink(self, sink):
        """Bind the co-located router's synchronous mode update hook."""
        self._navigation_mode_sink = sink

    def set_navigation_pending_goals_getter(self, getter):
        """Bind the co-located router's accepted-goal reservation count."""
        self._navigation_pending_goals_getter = getter

    def _navigation_pending_goals(self):
        if self._navigation_pending_goals_getter is None:
            return 0
        try:
            return max(0, int(self._navigation_pending_goals_getter()))
        except Exception as e:
            self.get_logger().error(f"Failed to read router goal reservations: {e}")
            return None

    def _init_service_clients(self):
        """Pre-create all service clients needed for lifecycle management."""
        # Collect all nodes from all modes
        all_nodes = set()
        for nodes_list in modes_nodes.values():
            for node_name in nodes_list:
                all_nodes.add(node_name)

        # Create clients for each node's lifecycle services and map loading
        for node_name in all_nodes:
            # GetState service
            service_key = f"/{node_name}/get_state"
            if service_key not in self._service_clients:
                client = self.create_client(GetState, service_key, callback_group=self._calls_going_outside_group)
                self._service_clients[service_key] = client

            # ChangeState service
            service_key = f"/{node_name}/change_state"
            if service_key not in self._service_clients:
                client = self.create_client(ChangeState, service_key, callback_group=self._calls_going_outside_group)
                self._service_clients[service_key] = client

            # LoadMap service (for map_server nodes)
            if "map_server" in node_name:
                service_key = f"/{node_name}/load_map"
                if service_key not in self._service_clients:
                    client = self.create_client(LoadMap, service_key, callback_group=self._calls_going_outside_group)
                    self._service_clients[service_key] = client

        self.get_logger().info(f"Initialized {len(self._service_clients)} service clients")

    def shutdown_mode(self, mode: str) -> None:
        """Shutdown all nodes for a given mode in reverse order using proper lifecycle transitions"""
        if mode not in modes_nodes:
            self.get_logger().debug(f"Mode '{mode}' not found in modes_nodes")
            return

        nodes = modes_nodes.get(mode, [])
        if not nodes:
            self.get_logger().debug(f"No nodes configured for mode '{mode}'")
            return

        # Iterate in reverse order for shutdown
        for node_name in reversed(nodes):
            try:
                # Get current state of the node
                current_state_id = get_node_state(self._service_clients, self.get_logger(), node_name)

                if current_state_id is None:
                    self.get_logger().warning(f"Failed to get state for {node_name}")
                    continue

                self.get_logger().debug(f"{node_name} state {current_state_id}")

                # Transition to UNCONFIGURED (handles both ACTIVE and INACTIVE)
                if current_state_id != State.PRIMARY_STATE_UNCONFIGURED:
                    self.get_logger().info(f"Shutting down {node_name}")
                    success = transition_node(
                        self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_UNCONFIGURED
                    )
                    if success:
                        self.get_logger().info(f"Shut down {node_name}")
                    else:
                        self.get_logger().warning(f"Failed to shut down {node_name}")

                elif current_state_id == State.PRIMARY_STATE_UNCONFIGURED:
                    self.get_logger().debug(f"Node {node_name} already unconfigured, skipping")
                else:
                    self.get_logger().debug(f"Node {node_name} in unknown state {current_state_id}, no shutdown needed")

            except Exception as e:
                self.get_logger().debug(f"Error shutting down {node_name}: {e}")

    def reset_mapping_callback(self, _request, response):
        """Discard the unsaved SLAM graph and restart the current mapping stack."""

        if not self._mode_change_lock.acquire(blocking=False):
            response.success = False
            response.message = "Navigation mode change already in progress"
            return response

        previous_mode = self.current_mode
        try:
            if previous_mode not in MAPPING_MODES:
                response.success = False
                response.message = "A map can only be reset while mapping is active"
                return response

            limit_ok, limit_message = self._set_mapping_speed_limit(True)
            if not limit_ok:
                response.success = False
                response.message = f"Map reset aborted before transition: {limit_message}"
                return response

            self.current_mode = "switching"
            self.publish_status()
            cancelled_ok, cancel_message = self._cancel_active_navigation()
            if not cancelled_ok:
                self.current_mode = previous_mode
                self.publish_status()
                response.success = False
                response.message = f"Map reset aborted: could not stop active navigation ({cancel_message})"
                return response

            # A same-mode startup deliberately preserves slam_toolbox.  Reset
            # instead tears the complete mapping stack down, with SLAM last,
            # so its next configure creates a genuinely empty pose graph.
            for node_name in reversed(modes_nodes[previous_mode]):
                target_state = (
                    State.PRIMARY_STATE_INACTIVE
                    if node_name in skip_cleanup_nodes
                    else State.PRIMARY_STATE_UNCONFIGURED
                )
                if not transition_node(self._service_clients, self.get_logger(), node_name, target_state):
                    rollback_ok, rollback_message = self.request_mode_startup(NavigationMode(previous_mode))
                    self.current_mode = previous_mode if rollback_ok else "none"
                    self.publish_status()
                    response.success = False
                    response.message = f"Map reset failed while stopping {node_name}"
                    if not rollback_ok:
                        response.message += f"; failed to restore mapping: {rollback_message}"
                    return response

            success, message = self.request_mode_startup(NavigationMode(previous_mode))
            if not success:
                self.current_mode = "none"
                self.publish_status()
                response.success = False
                response.message = f"Map reset failed while restarting mapping: {message}"
                return response

            self._mapping_session_started = time.time()
            self.mapping_session_publisher.publish(String(data=json.dumps({"started": self._mapping_session_started})))
            self.current_mode = previous_mode
            self.save_last_mode(previous_mode)
            self.publish_status()
            response.success = True
            response.message = "Discarded the old map and started a fresh SLAM session"
            return response
        except Exception as exc:
            self.current_mode = "none"
            self.publish_status()
            response.success = False
            response.message = f"Error resetting map: {exc}"
            self.get_logger().error(response.message)
            return response
        finally:
            self._mode_change_lock.release()

    def request_mode_startup(self, mode: NavigationMode) -> tuple[bool, str]:
        """
        Request startup of navigation nodes for the given mode.
        First configures all nodes in forward order, then activates them one by one.
        Args:
            mode: One of the NavigationMode values.
        Returns: (success: bool, message: str)
        """

        try:
            self.get_logger().info(f"Requesting {mode.value} mode startup")

            # Log current state of all nodes before any transitions
            state_names = {
                0: "UNKNOWN",
                1: "UNCONFIGURED",
                2: "INACTIVE",
                3: "ACTIVE",
                4: "FINALIZED",
                10: "CONFIGURING",
                11: "CLEANINGUP",
                12: "SHUTTINGDOWN",
                13: "ACTIVATING",
                14: "DEACTIVATING",
                15: "ERRORPROCESSING",
            }
            all_nodes = set()
            for nodes_list in modes_nodes.values():
                all_nodes.update(nodes_list)
            state_summary = []
            for n in sorted(all_nodes):
                s = get_node_state(self._service_clients, self.get_logger(), n)
                state_summary.append(f"{n}={state_names.get(s, str(s)) if s is not None else 'UNREACHABLE'}")
            self.get_logger().info(f"Node states before {mode.value} startup: {', '.join(state_summary)}")

            target_nodes = set(modes_nodes[mode.value])
            all_nodes_except_target = []
            seen = set()
            for mode_name, nodes in modes_nodes.items():  # noqa: B007
                for node in nodes:
                    if node not in target_nodes and node not in seen:
                        all_nodes_except_target.append(node)
                        seen.add(node)

            # Get nodes for this mode
            nodes = modes_nodes.get(mode.value, [])
            if not nodes:
                msg = f"No nodes configured for mode '{mode.value}'"
                self.get_logger().error(msg)
                return False, msg

            node_names = nodes

            for node_name in all_nodes_except_target:
                # Check if this node should skip cleanup (only deactivate to INACTIVE)
                if node_name in skip_cleanup_nodes:
                    self.get_logger().info(f"Deactivating {node_name} (skip cleanup due to RMW bug)")
                    success = transition_node(
                        self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_INACTIVE
                    )
                    # Small delay to let RMW layer stabilize after deactivation
                    time.sleep(0.2)
                else:
                    # Transition node to unconfigured
                    success = transition_node(
                        self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_UNCONFIGURED
                    )

                if not success:
                    self.get_logger().warning(f"Failed to shutdown non-target node {node_name} (continuing)")

            # Get nodes that should only be configured (not activated) for this mode
            configure_only = configure_only_nodes.get(mode.value, set())

            # Startup runs in up to two passes. A node can miss the first pass
            # transiently — most commonly it is stuck in a lifecycle transition
            # waiting on a peer this very pass brings up (e.g. a costmap's
            # on_activate blocked on a map publisher), which also makes its
            # lifecycle services unresponsive. The second pass re-runs the
            # sweep once the failed nodes have settled back into a primary
            # state (bounded poll) and picks up whatever recovered.
            # (Previously the first failure left the mode down and a manual
            # retry of change_mode succeeded.)
            # Exception: a map-load failure alone is not retried — the load
            # already retries internally, so a second sweep can't fix it and
            # would only double the time spent holding the mode lock.
            failures, map_load_success = self._startup_pass(mode, node_names, configure_only)
            failed_nodes = [f for f in failures if not f.endswith("_map_load")]
            if failed_nodes:
                self.get_logger().warning(
                    f"{mode.value} startup pass 1 failed on {failures}; retrying once after settle"
                )
                self._wait_for_nodes_to_settle(failed_nodes)
                failures, map_load_success = self._startup_pass(
                    mode, node_names, configure_only, map_already_loaded=map_load_success
                )

            if failures:
                message = f"{mode.value} mode started with {len(failures)} activation failures: {failures}"
                self.get_logger().warning(message)
            else:
                map_status = "loaded" if map_load_success else "not loaded" if mode == NavigationMode.NAV else "N/A"
                message = f"{mode.value} mode started successfully (map: {map_status})"
                self.get_logger().info(message)

            self._lifecycle_watchdog_armed = True
            return len(failures) == 0, message

        except Exception as e:
            error_msg = f"Error requesting {mode.value} startup: {str(e)}"
            self.get_logger().error(error_msg)
            return False, error_msg

    def _startup_pass(
        self, mode: NavigationMode, node_names: list, configure_only: set, map_already_loaded: bool = False
    ) -> tuple[list, bool]:
        """One configure+activate sweep over the mode's nodes.

        Safe to run repeatedly, with two caveats the code below handles:
        transitions are idempotent (nodes already at/above the target are
        left alone) EXCEPT configure-only nodes, which are deliberately
        pulled back down to INACTIVE; and the navigation map load is not a
        lifecycle transition — pass map_already_loaded=True on a retry pass
        to skip reloading a map a previous pass already loaded (only_up
        transitions never take the map server back down, so it persists).
        Returns the nodes that failed plus whether the map load succeeded
        (navigation mode only).
        """
        failures = []
        map_load_success = False

        # Phase 1: Configure all nodes in forward order
        self.get_logger().info(f"Configuring {len(node_names)} nodes for {mode.value} mode")
        for node_name in node_names:
            # Configure-only nodes must land exactly at INACTIVE: a leftover
            # ACTIVE one (e.g. recovered from a wedged activation) would keep
            # its costmap running against this mode's stand-in map, spamming
            # warnings and wasting CPU — so no only_up for them.
            success = transition_node(
                self._service_clients,
                self.get_logger(),
                node_name,
                State.PRIMARY_STATE_INACTIVE,
                only_up=node_name not in configure_only,
            )
            if not success:
                # Keep configuring the rest: a later peer coming up is exactly
                # what un-wedges a node stuck mid-transition (see two-pass
                # comment in request_mode_startup), so recovery must not
                # depend on the wedged node's position in the list.
                failures.append(node_name)
                self.get_logger().warning(f"Failed to configure {node_name}, continuing...")
        self.get_logger().info("Configured nodes")

        # Phase 2: Activate nodes in forward order
        self.get_logger().info(f"Activating {len(node_names)} nodes for {mode.value} mode")
        for node_name in node_names:
            if node_name in failures:
                self.get_logger().warning(f"{node_name} failed configuration. Not proceeding further.")
                break  # don't process the rest of the nodes

            # Skip activation for configure-only nodes
            if node_name in configure_only:
                self.get_logger().info(f"Skipping activation for {node_name} (configure-only)")
                continue

            success = transition_node(self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_ACTIVE)
            if not success:
                failures.append(node_name)
                self.get_logger().warning(f"Failed to activate {node_name}. Not proceeding further.")
                break

            # Costmap2DROS::on_activate blocks UNBOUNDED on canTransform(map->
            # base_link) — invisibly at --log-level warn — so activating the
            # costmap-bearing nodes while AMCL holds no pose wedges the whole
            # mode (17s change_state timeouts, observed live). Nothing
            # guarantees a pose here: grid_localizer may be absent and the
            # latched /initialpose replay stale or missing. Confirm
            # localization first, seeding the end-of-mapping pose if one exists.
            if mode == NavigationMode.NAV and node_name == "navigation_amcl":
                self._ensure_localized_before_costmaps()

            # Load map immediately after map server is activated (navigation mode only)
            if mode == NavigationMode.NAV and "map_server" in node_name and success:
                if map_already_loaded:
                    self.get_logger().info(f"Map already loaded on {node_name} in a previous pass; skipping reload")
                    map_load_success = True
                else:
                    map_load_success = self._load_map_on_server(node_name)
                    if not map_load_success:
                        failures.append(f"{node_name}_map_load")

        self.get_logger().info("Activated nodes")
        return failures, map_load_success

    def _wait_for_nodes_to_settle(self, node_names: list, timeout_sec: float = 10.0, poll_sec: float = 0.5) -> None:
        """Wait for the given nodes to settle into a primary lifecycle state.

        A pass-1 failure usually means the node is wedged mid-transition
        (e.g. ACTIVATING until a peer's map arrives), with its lifecycle
        services unresponsive. Rather than guessing a fixed delay, poll
        get_state until every node answers with a primary state, so the
        retry pass runs exactly when it can succeed. Bounded by timeout_sec
        so a permanently dead node can't hold the mode lock long; the retry
        pass still runs and reports whatever remains broken.
        """

        def is_settled(node_name: str) -> bool:
            state = get_node_state(self._service_clients, self.get_logger(), node_name, timeout_sec=2.0)
            return state is not None and state < State.TRANSITION_STATE_CONFIGURING

        deadline = time.monotonic() + timeout_sec
        pending = list(node_names)
        while pending and time.monotonic() < deadline:
            pending = [n for n in pending if not is_settled(n)]
            if pending:
                time.sleep(poll_sec)
        if pending:
            self.get_logger().warning(f"Nodes still unsettled after {timeout_sec:.0f}s: {pending}; retrying anyway")

    def _localized_since(self, t0) -> bool:
        """map->base_link exists with a stamp newer than t0 — only AMCL can
        have published it (slam is already down), so the seed really took."""
        try:
            tf = self.tf_buffer.lookup_transform("map", "base_link", rclpy.time.Time())
        except tf2_ros.TransformException:
            return False
        return rclpy.time.Time.from_msg(tf.header.stamp) > t0

    def _end_of_mapping_seed(self) -> SetInitialPose.Request | None:
        """The robot's final mapping pose, while a session just ended.

        The pose is in the slam session's frame: exact for the map that
        session saved (the finish flow switches to it right after), and only
        approximate when returning to a previous map — no worse than the
        latched-replay behavior it replaces, and relocalization refines it.
        """
        if self._last_mapping_pose is None:
            return None
        tf, captured_at = self._last_mapping_pose
        if time.monotonic() - captured_at > 120.0:
            return None
        request = SetInitialPose.Request()
        pose = request.pose
        pose.header.frame_id = "map"
        pose.pose.pose.position.x = tf.transform.translation.x
        pose.pose.pose.position.y = tf.transform.translation.y
        pose.pose.pose.orientation = tf.transform.rotation
        pose.pose.covariance[0] = 0.1
        pose.pose.covariance[7] = 0.1
        pose.pose.covariance[35] = 0.05
        return request

    def _ensure_localized_before_costmaps(self) -> None:
        """Bounded wait for AMCL to actually hold a pose, re-seeding as needed.

        Trusts only the outcome — a map->base_link transform published after
        this method started (only AMCL can produce one; slam is already down)
        — and re-sends the seed until it appears. On timeout it proceeds with
        a warning rather than blocking the mode change; the two-pass startup
        and the lifecycle watchdog remain the backstop.
        """
        t0 = self.get_clock().now()
        seed = self._end_of_mapping_seed()
        deadline = time.monotonic() + (10.0 if seed is not None else 5.0)
        next_seed_at = 0.0
        while time.monotonic() < deadline:
            if self._localized_since(t0):
                if seed is not None:
                    self.get_logger().info("AMCL confirmed localized at the end-of-mapping pose")
                return
            if seed is not None and time.monotonic() >= next_seed_at:
                self.get_logger().info("Seeding AMCL with the end-of-mapping pose")
                seed.pose.header.stamp = self.get_clock().now().to_msg()
                call_service(
                    {"/set_initial_pose": self._amcl_seed_client},
                    self.get_logger(),
                    "/set_initial_pose",
                    seed,
                    timeout_sec=2.0,
                )
                next_seed_at = time.monotonic() + 2.0
            time.sleep(0.3)
        self.get_logger().warning(
            "Activating navigation without confirmed localization; costmap activation may stall "
            "until a pose is set (relocalize from the app)"
        )

    def _lifecycle_watchdog(self):
        """Restore nodes that crashed and respawned mid-session.

        Observed live: controller_server dies with a double-free SIGABRT after
        repeated "Failed to make progress" aborts (a sibling of the known
        teardown crashes listed in skip_cleanup_nodes). launch respawns it,
        and this watchdog notices and re-drives it to where the current mode
        needs it -- turning a crash into a few seconds of downtime instead of
        a dead nav stack.

        Restores nodes found UNCONFIGURED (fresh respawn) or stuck INACTIVE
        when the mode expects ACTIVE (a previous restore's activate failed).
        States are probed WITHOUT the mode lock on a short timeout, so a slow
        or dead node never blocks user mode/map operations; the lock is taken
        only to perform an actual repair, with the state re-checked under it.
        """
        if not self._lifecycle_watchdog_armed or self._watchdog_busy:
            return
        mode = getattr(self, "current_mode", None)
        if mode not in modes_nodes:
            return
        self._watchdog_busy = True
        try:

            def restore_target(node_name):
                """(needs_restore, target_state) for a current-mode node."""
                state = get_node_state(self._service_clients, self.get_logger(), node_name, timeout_sec=2.0)
                expect_active = node_name not in configure_only_nodes.get(mode, set())
                target = State.PRIMARY_STATE_ACTIVE if expect_active else State.PRIMARY_STATE_INACTIVE
                wrong = state == State.PRIMARY_STATE_UNCONFIGURED or (
                    state == State.PRIMARY_STATE_INACTIVE and expect_active
                )
                return wrong, target

            needs_restore = [n for n in modes_nodes[mode] if restore_target(n)[0]]
            if not needs_restore:
                return
            # A mode/map operation owns the transitions while it holds the lock.
            if not self._mode_change_lock.acquire(blocking=False):
                return
            try:
                # The unlocked probes above can overlap an entire mode change.
                # Never resurrect nodes from the captured stack after that
                # transition has finished (e.g. slam_toolbox beside AMCL).
                if self.current_mode != mode:
                    self.get_logger().info(
                        f"Navigation mode changed from {mode} to {self.current_mode} during watchdog probes; "
                        "discarding stale repairs"
                    )
                    return
                for node_name in needs_restore:
                    # Re-check under the lock: a mode operation may have just moved it.
                    wrong, target = restore_target(node_name)
                    if not wrong:
                        continue
                    self.get_logger().warning(
                        f"{node_name} is below its expected lifecycle state while {mode} mode is active -- "
                        "it likely crashed and respawned; re-driving its lifecycle"
                    )
                    # only_up: the watchdog may raise a node, never lower one.
                    if transition_node(self._service_clients, self.get_logger(), node_name, target, only_up=True):
                        self.get_logger().info(f"{node_name} restored after respawn")
                    else:
                        self.get_logger().error(f"Failed to restore {node_name}; retrying on the next watchdog tick")
            finally:
                self._mode_change_lock.release()
        finally:
            self._watchdog_busy = False

    def _load_map_on_server(self, node_name: str, max_retries: int = 20) -> bool:
        """
        Load the current map on the specified map server node.
        Retries until successful or max_retries is reached.
        Args:
            node_name: Name of the map server node
            max_retries: Maximum number of retry attempts
        Returns: True if map loaded successfully, False otherwise
        """

        if node_name is None or self.current_map is None:
            return False

        map_request = LoadMap.Request()
        map_request.map_url = os.path.join(self.maps_dir, self.current_map)

        self.get_logger().info(f"Loading map: {self.current_map} on {node_name}")

        retry_count = 0
        while retry_count <= max_retries:
            map_result = call_service(
                self._service_clients, self.get_logger(), f"/{node_name}/load_map", map_request, timeout_sec=5.0
            )

            if map_result is not None and map_result.result == 0:
                self.get_logger().info(f"Map loaded successfully after {retry_count + 1} attempt(s)")
                return True
            else:
                retry_count += 1
                if retry_count <= max_retries:
                    self.get_logger().warning(f"Map load attempt {retry_count} failed, retrying...")
                    time.sleep(0.25)

        self.get_logger().error(f"Failed to load map after {max_retries} attempts")
        return False

    def _efficient_map_switch(self) -> tuple[bool, str]:
        """
        Efficiently switch maps by transitioning bt_navigator down, loading map, then bringing all nodes up.

        Algorithm:
        1. Transition bt_navigator to inactive
        2. Load new map on map_server
        3. Transition all nodes to active
        """
        nodes = modes_nodes.get("navigation", [])
        if not nodes:
            return False, "No navigation nodes configured"

        failures = []

        # Step 1: Transition bt_navigator down to inactive
        self.get_logger().info(f"Step 1: Transitioning {bt_node} to inactive")
        success = transition_node(self._service_clients, self.get_logger(), bt_node, State.PRIMARY_STATE_INACTIVE)
        if not success:
            failures.append("bt_navigator")
            self.get_logger().warning("Failed to transition bt_navigator down")

        # Step 2: Load new map
        self.get_logger().info("Step 2: Loading new map")
        map_load_success = self._load_map_on_server(map_server_node)
        if not map_load_success:
            failures.append(f"{map_server_node}_map_load")

        # Step 3: Transition all nodes to active
        self.get_logger().info("Step 3: Activating all nodes")
        for node_name in nodes:
            success = transition_node(
                self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_ACTIVE, only_up=True
            )
            if not success:
                failures.append(node_name)
                self.get_logger().warning(f"Failed to activate {node_name}")

        if failures:
            return False, f"Map switch completed with failures: {failures}"
        else:
            return True, f"Map switched successfully to {self.current_map}"

    def _localization_status_cb(self, msg):
        self._last_localization_status = (msg.data, time.monotonic())

    def _trigger_relocalization(self, since: float, generation: int):
        """Confirm grid_localizer re-localized after a map switch.

        grid_localizer restarts auto-localization itself whenever a new map
        arrives on the latched /map topic (load_map always republishes), so
        the primary path is to watch /localization/status for a result newer
        than `since` — a fixed sleep + blind /localize call could race the map
        handoff and search against the previous map. The explicit /localize
        Trigger is only a fallback when no status update ever arrives.
        Failure is logged but never fails the map switch.
        """
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            # A newer mode/map/current_map change supersedes this one: the
            # status feed is map-agnostic, so a stale waiter must not claim a
            # newer switch's "localized" as its own confirmation. The
            # generation check catches this even when the newer switch
            # acquired AND released the lock between our polls.
            if self._switch_generation != generation:
                self.get_logger().info("Another mode/map change superseded this one; leaving relocalization to it")
                return
            status, stamp = self._last_localization_status
            if stamp >= since and status.startswith("localized"):
                self.get_logger().info(f"Grid localizer re-localized on the new map (status: {status})")
                return
            if stamp >= since and status in ("error", "timeout"):
                break  # auto path gave up; fall through to the explicit trigger
            time.sleep(0.2)

        if self._switch_generation != generation:
            self.get_logger().info("Another mode/map change superseded this one; leaving relocalization to it")
            return
        result = call_service(
            {"/localize": self._localize_client}, self.get_logger(), "/localize", Trigger.Request(), timeout_sec=6.0
        )
        if result is not None and result.success:
            self.get_logger().info(f"Re-localized on new map: {result.message}")
        else:
            self.get_logger().warning(
                "Re-localization after map switch failed or timed out; AMCL may still hold the previous map's pose"
            )

    def _camera_points_cb(self, _msg):
        self._camera_points_seen = True
        if self._camera_points_sub is not None:
            self.destroy_subscription(self._camera_points_sub)
            self._camera_points_sub = None

    def _check_camera_obstacle_source(self):
        self._camera_check_timer.cancel()
        if not self._camera_points_seen:
            self.get_logger().warning(
                "No camera pointcloud on /mars/main_camera/points 30s after start: the costmaps' camera "
                "obstacle layer is inert (missing stereo calibration?) — navigating with lidar obstacles only"
            )

    def change_map_callback(self, request, response):
        """
        Service callback to change the map for navigation mode
        request.map_name should contain the map filename (e.g., "home.yaml")
        """
        release_warning = ""
        try:
            requested_map = request.map_name.strip()

            # Validate that the requested map exists
            if requested_map not in self.available_maps:
                response.success = False
                response.message = f"Error: Map '{requested_map}' not found. Available maps: {self.available_maps}"
                self.get_logger().error(response.message)
                return response

            # Serialize against mode changes: both callbacks drive lifecycle
            # transitions on the same nodes, and interleaving them leaves a
            # half-active stack (e.g. one thread deactivating bt_navigator
            # while the other activates it). Non-blocking like change_mode:
            # queued blocking acquires would each pin an executor thread.
            if not self._mode_change_lock.acquire(blocking=False):
                response.success = False
                response.message = "Another mode or map change is already in progress; try again"
                self.get_logger().warning(response.message)
                return response

            relocalize_after_switch = False
            switch_started = time.monotonic()
            self._switch_generation += 1
            my_generation = self._switch_generation
            previous_map = self.current_map
            previous_mode = self.current_mode
            admission_closed = False
            speed_limit_armed = False
            try:
                # A map replacement tears bt_navigator down just like a mode
                # switch. Close the co-located router synchronously and wait
                # for both handed-off and reserved goals to terminate before
                # touching current_map or any lifecycle node. The web client's
                # skill cancel is only a request acknowledgement and cannot be
                # this server-side safety barrier.
                if previous_mode == "navigation":
                    limit_ok, limit_message = self._set_mapping_speed_limit(True)
                    if not limit_ok:
                        response.success = False
                        response.message = f"Map switch aborted before transition: {limit_message}"
                        self.get_logger().error(response.message)
                        return response
                    speed_limit_armed = True
                    self.current_mode = "switching"
                    admission_closed = True
                    self.publish_status()
                    cancelled_ok, cancel_message = self._cancel_active_navigation()
                    if not cancelled_ok:
                        response.success = False
                        response.message = f"Map switch aborted: could not stop active navigation ({cancel_message})"
                        self.current_mode = previous_mode
                        self.publish_status()
                        admission_closed = False
                        self.get_logger().error(response.message)
                        return response

                # _efficient_map_switch loads self.current_map, so set it for
                # the attempt but only persist (and keep) it on success.
                self.current_map = requested_map

                # If we're in navigation mode, use efficient map switch
                if previous_mode == "navigation":
                    self.get_logger().info(f"Efficiently switching to new map: {requested_map}")

                    success, message = self._efficient_map_switch()

                    if success:
                        self.save_last_map(requested_map)
                        relocalize_after_switch = True
                        response.success = True
                        response.message = f"Successfully changed map to '{requested_map}'"
                        self.get_logger().info(response.message)
                    else:
                        self.current_map = previous_map
                        response.success = False
                        response.message = f"Failed to switch to map '{requested_map}': {message}"
                        self.get_logger().error(response.message)
                else:
                    # If not in navigation mode, just update the map for next time navigation starts
                    self.save_last_map(requested_map)
                    response.success = True
                    response.message = f"Map set to '{requested_map}' for next navigation session"
                    self.get_logger().info(response.message)
            except Exception:
                # An exception mid-switch must not leave current_map pointing
                # at a map that never loaded.
                self.current_map = previous_map
                raise
            finally:
                try:
                    if admission_closed:
                        self.current_mode = previous_mode
                        self.publish_status()
                    if (
                        speed_limit_armed
                        and self.current_mode == previous_mode
                        and previous_mode in {NavigationMode.NAV.value, NavigationMode.MAPFREE.value}
                    ):
                        limit_ok, limit_message = self._release_mapping_speed_limit_after_stable_mode()
                        if not limit_ok:
                            release_warning = f"; Slow drive envelope remains active ({limit_message})"
                            if getattr(response, "message", ""):
                                response.message += release_warning
                finally:
                    self._mode_change_lock.release()

            # Outside the lock: relocalization waits up to ~12 s on the grid
            # localizer and must not block concurrent mode/map service calls —
            # the lifecycle switch itself is already complete.
            if relocalize_after_switch:
                self._trigger_relocalization(since=switch_started, generation=my_generation)

        except Exception as e:
            if getattr(response, "success", None) is False and getattr(response, "message", ""):
                response.message += f"; additional error while changing map: {str(e)}"
            else:
                response.success = False
                response.message = f"Error changing map: {str(e)}"
            response.message += release_warning
            self.get_logger().error(response.message)

        return response

    def delete_map_callback(self, request, response):
        """
        Service callback to delete a saved map (.yaml and associated image)
        Accepts either the map base name (e.g., "home") or filename (e.g., "home.yaml")
        """
        # Same lock as mode/map changes: this callback reads current_mode and
        # can reassign current_map — unserialized, it could observe (or mutate)
        # state mid-switch, e.g. deleting the map a locked change_map is
        # loading right now.
        if not self._mode_change_lock.acquire(blocking=False):
            response.success = False
            response.message = "A mode or map change is in progress; try again"
            self.get_logger().warning(response.message)
            return response
        try:
            requested_name = request.map_name.strip()
            if not requested_name:
                response.success = False
                response.message = "Map name cannot be empty"
                self.get_logger().error(response.message)
                return response

            # Normalize to YAML filename
            map_yaml_name = requested_name if requested_name.endswith(".yaml") else f"{requested_name}.yaml"

            # Validate that the map exists
            if map_yaml_name not in self.available_maps:
                response.success = False
                response.message = f"Error: Map '{map_yaml_name}' not found. Available maps: {self.available_maps}"
                self.get_logger().error(response.message)
                return response

            # Prevent deleting the active map while navigation is running
            if map_yaml_name == self.current_map and self.current_mode == "navigation":
                response.success = False
                response.message = f"Cannot delete the active map '{map_yaml_name}' while navigation is running. Change map or stop navigation first."
                self.get_logger().error(response.message)
                return response

            # If we are deleting the current map (but not running navigation), pick a fallback
            if map_yaml_name == self.current_map:
                remaining_maps = [m for m in self.available_maps if m != map_yaml_name]
                if "home.yaml" in remaining_maps:
                    fallback = "home.yaml"
                elif remaining_maps:
                    fallback = remaining_maps[0]
                else:
                    # No maps left: None forces mapping mode until a new map is saved
                    fallback = None

                self._switch_generation += 1  # supersede any in-flight relocalization waiter
                self.current_map = fallback
                self.save_last_map(fallback)
                self.get_logger().info(f"Current map changed to '{fallback}' prior to deletion of '{map_yaml_name}'")

            # Delete files (.yaml and associated image files)
            base_name = os.path.splitext(map_yaml_name)[0]
            yaml_path = os.path.join(self.maps_dir, map_yaml_name)
            pgm_path = os.path.join(self.maps_dir, f"{base_name}.pgm")
            png_path = os.path.join(self.maps_dir, f"{base_name}.png")

            removed_any = False
            for path in [yaml_path, pgm_path, png_path]:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                        removed_any = True
                except Exception as e:
                    self.get_logger().warning(f"Could not remove '{path}': {e}")

            if not removed_any:
                response.success = False
                response.message = f"No files found to delete for map '{map_yaml_name}'"
                self.get_logger().error(response.message)
                return response

            # Refresh available maps list
            self.available_maps = self.discover_maps()

            response.success = True
            response.message = f"Successfully deleted map '{map_yaml_name}'"
            self.get_logger().info(response.message)

        except Exception as e:
            response.success = False
            response.message = f"Error deleting map: {str(e)}"
            self.get_logger().error(response.message)
        finally:
            self._mode_change_lock.release()

        return response

    def save_map_callback(self, request, response):
        """
        Service callback to save the current map with a new name
        Autonomous mapping is first quiesced into manual mapping so the saved
        occupancy grid is not taken while exploration is still driving.
        request.map_name should contain the new map name (e.g., "my_new_map")
        request.overwrite: if true, allows overwriting existing maps
        """
        # Reject malformed requests before stopping an autonomous run.
        map_name = request.map_name.strip()
        if not map_name or not map_name.replace("_", "").replace("-", "").isalnum():
            response.success = False
            response.message = (
                f"Invalid map name '{map_name}'. Use alphanumeric characters, underscores, and hyphens only."
            )
            self.get_logger().error(response.message)
            return response

        # Do not hold _mode_change_lock while entering the existing mode-change
        # path: it owns that lock itself and provides the full admission-close,
        # goal-cancel, terminal-wait, and lifecycle transition barrier. A
        # standalone save intentionally leaves the robot in manual mapping.
        if self.current_mode == NavigationMode.AUTONOMOUS_MAPPING.value:
            mode_request = ChangeNavigationMode.Request()
            mode_request.mode = NavigationMode.MAPPING.value
            mode_response = self.change_mode_callback(mode_request, ChangeNavigationMode.Response())
            if not mode_response.success:
                response.success = False
                response.message = f"Cannot save map - failed to stop autonomous exploration: {mode_response.message}"
                self.get_logger().error(response.message)
                return response

        # Saving reads current_mode/current_map and mutates the maps directory.
        # Serialize it with mode switches and map changes so slam_toolbox cannot
        # be torn down halfway through map_saver_cli or an overwrite cannot race
        # another map operation. Keep the acquire non-blocking so executor
        # threads never queue behind a long map save.
        if not self._mode_change_lock.acquire(blocking=False):
            response.success = False
            response.message = "A mode or map operation is already in progress; try again"
            self.get_logger().warning(response.message)
            return response
        try:
            map_name = request.map_name.strip()

            # Revalidate under the lock. Autonomous mapping may have restarted
            # after the optimistic quiesce returned but before this acquire;
            # never delete or write map files unless manual mapping still owns
            # the lifecycle stack.
            if self.current_mode != NavigationMode.MAPPING.value:
                response.success = False
                response.message = f"Cannot save map - manual mapping is not active. Current mode: {self.current_mode}"
                self.get_logger().error(response.message)
                return response

            # Validate map name
            if not map_name or not map_name.replace("_", "").replace("-", "").isalnum():
                response.success = False
                response.message = (
                    f"Invalid map name '{map_name}'. Use alphanumeric characters, underscores, and hyphens only."
                )
                self.get_logger().error(response.message)
                return response

            # Check if map already exists
            map_yaml_name = f"{map_name}.yaml"
            is_overwriting = map_yaml_name in self.available_maps
            if is_overwriting:
                if not request.overwrite:
                    response.success = False
                    response.message = f"Map '{map_yaml_name}' already exists. Set overwrite=true to replace it, or choose a different name."
                    self.get_logger().error(response.message)
                    return response
                else:
                    self.get_logger().info(f"Overwriting existing map: {map_yaml_name}")
                    # Remove old files before saving new ones
                    try:
                        old_yaml = os.path.join(self.maps_dir, map_yaml_name)
                        old_pgm = os.path.join(self.maps_dir, f"{map_name}.pgm")
                        if os.path.exists(old_yaml):
                            os.remove(old_yaml)
                        if os.path.exists(old_pgm):
                            os.remove(old_pgm)
                        self.get_logger().info(f"Removed old map files for: {map_name}")
                    except Exception as e:
                        self.get_logger().warning(f"Could not remove old map files: {e}")

            # Ensure maps directory exists
            os.makedirs(self.maps_dir, exist_ok=True)

            # Create full path for the map (without extension)
            map_path = os.path.join(self.maps_dir, map_name)

            self.get_logger().info(f"Saving current map as: {map_name}")

            # TODO: make this better
            # Use nav2_map_server map_saver_cli to save the map
            save_cmd = [
                "ros2",
                "run",
                "nav2_map_server",
                "map_saver_cli",
                "-f",
                map_path,
                "--ros-args",
                "-p",
                "save_map_timeout:=5000.0",
            ]

            # Run the map saver command
            result = subprocess.run(
                save_cmd,
                capture_output=True,
                text=True,
                timeout=30,  # 30 second timeout
            )

            if result.returncode == 0:
                # Check if the files were actually created
                yaml_file = f"{map_path}.yaml"
                pgm_file = f"{map_path}.pgm"

                if os.path.exists(yaml_file) and os.path.exists(pgm_file):
                    response.success = True
                    action_word = "overwritten" if is_overwriting else "saved"
                    response.message = f"Successfully {action_word} map as '{map_name}.yaml'"
                    self.get_logger().info(response.message)
                    save_announcement = {"map": map_yaml_name, "stamp": time.time()}
                    if self._mapping_session_started is not None:
                        save_announcement["mapping_started"] = self._mapping_session_started
                    self.map_saved_publisher.publish(String(data=json.dumps(save_announcement)))

                    # Refresh available maps list
                    self.available_maps = self.discover_maps()
                    self.get_logger().info(f"Updated available maps: {self.available_maps}")

                    # Adopt the saved map if there is no valid current map
                    if self.current_map not in self.available_maps:
                        self.current_map = map_yaml_name
                        self.save_last_map(map_yaml_name)
                        self.get_logger().info(f"Current map set to newly saved '{map_yaml_name}'")
                else:
                    response.success = False
                    response.message = f"Map saver completed but files not found at {map_path}"
                    self.get_logger().error(response.message)
            else:
                response.success = False
                response.message = f"Map saver failed with return code {result.returncode}: {result.stderr}"
                self.get_logger().error(response.message)

        except subprocess.TimeoutExpired:
            response.success = False
            response.message = "Map saving timed out after 30 seconds"
            self.get_logger().error(response.message)
        except Exception as e:
            response.success = False
            response.message = f"Error saving map: {str(e)}"
            self.get_logger().error(response.message)
        finally:
            self._mode_change_lock.release()

        return response

    def _cleanup_orphaned_processes(self):
        """Kill any orphaned navigation processes from previous mode_manager runs."""
        try:
            # Shutdown all modes gracefully
            self.get_logger().info("Attempting to shutdown all modes...")
            for mode in NavigationMode:
                self.shutdown_mode(mode.value)
            self.get_logger().info("Cleaned up all modes")
        except Exception as e:
            self.get_logger().warn(f"Cleanup warning: {e}")

    # bt_navigator goal states that are not yet terminal.
    _NAV_ACTIVE_STATUSES = (
        GoalStatus.STATUS_ACCEPTED,
        GoalStatus.STATUS_EXECUTING,
        GoalStatus.STATUS_CANCELING,
    )

    def _nav_status_callback(self, msg):
        self._nav_active_goals = sum(1 for s in msg.status_list if s.status in self._NAV_ACTIVE_STATUSES)

    def _cancel_active_navigation(self, rpc_timeout_sec=5.0, terminal_timeout_sec=5.0):
        """Cancel all active NavigateToPose goals and wait for them to reach a
        terminal state. Returns (success, message).

        Waits for terminal (not just cancel-acknowledged) so the caller can
        tear Nav2 down without deactivating bt_navigator before it has
        delivered the cancelled goal's result — which would strand the router
        or skill waiting forever. Never reports success while a goal is still
        live: skipping the cancel would strand it once Nav2 is torn down.
        """
        pending_goals = self._navigation_pending_goals()
        if pending_goals is None:
            return False, "Could not verify router goal reservations"
        if self._nav_active_goals == 0 and pending_goals == 0:
            return True, "No active navigation goals"

        response = call_service(
            self._service_clients, self.get_logger(), NAV_CANCEL_SERVICE, CancelGoal.Request(), rpc_timeout_sec
        )
        if response is None:
            return False, "Navigation is active but its cancel service did not respond"

        # The cancel RPC has its own availability/response budget.  Start a
        # fresh terminal-settle budget only after its acknowledgement arrives;
        # otherwise a slow-but-successful RPC could consume the entire deadline
        # and force lifecycle rollback before goals have a chance to terminate.
        terminal_deadline = time.monotonic() + terminal_timeout_sec
        while time.monotonic() < terminal_deadline:
            pending_goals = self._navigation_pending_goals()
            if pending_goals is None:
                return False, "Could not verify router goal reservations"
            if self._nav_active_goals == 0 and pending_goals == 0:
                break
            time.sleep(0.05)
        pending_goals = self._navigation_pending_goals()
        if pending_goals is None:
            return False, "Could not verify router goal reservations"
        if self._nav_active_goals > 0 or pending_goals > 0:
            return (
                False,
                "Cancelled goals did not reach a terminal state in time "
                f"(internal={self._nav_active_goals}, router={pending_goals})",
            )
        return True, f"Cancelled {len(response.goals_canceling)} navigation goal(s)"

    def cancel_navigation_callback(self, request, response):
        """Trigger service: stop all active navigation (app Stop button)."""
        response.success, response.message = self._cancel_active_navigation()
        self.get_logger().info(f"/nav/cancel_navigation: {response.message}")
        return response

    def _set_mapping_speed_limit(self, enabled: bool) -> tuple[bool, str]:
        """Synchronously set the mux's authoritative final-drive envelope."""

        state = "enable" if enabled else "disable"
        try:
            request = SetBool.Request()
            request.data = enabled
            result = call_service(
                self._service_clients,
                self.get_logger(),
                MAPPING_SPEED_LIMIT_SERVICE,
                request,
                timeout_sec=2.0,
            )
        except Exception as e:
            message = f"Could not {state} the mapping Slow envelope: {e}"
            self.get_logger().error(message)
            return False, message
        if result is None:
            message = f"Could not {state} the mapping Slow envelope: cmd_vel_mux did not acknowledge"
            self.get_logger().error(message)
            return False, message
        if not result.success:
            message = f"Could not {state} the mapping Slow envelope: {result.message}"
            self.get_logger().error(message)
            return False, message
        return True, result.message

    def _release_mapping_speed_limit_after_stable_mode(self) -> tuple[bool, str]:
        """Release Slow only after navigation admission has a stable mode."""

        stable_nonmapping_modes = {
            NavigationMode.NAV.value,
            NavigationMode.MAPFREE.value,
        }
        if self.current_mode not in stable_nonmapping_modes:
            message = f"Refusing to release mapping Slow envelope while mode is '{self.current_mode}'"
            self.get_logger().error(message)
            return False, message
        return self._set_mapping_speed_limit(False)

    def change_mode_callback(self, request, response, first_start=False):
        """
        Service callback to switch between modes
        request.mode = "navigation": Switch to navigation mode
        request.mode = "mapping": Switch to mapping mode
        """

        self.get_logger().info("Attempting to change mode")

        # Prevent concurrent mode changes (ReentrantCallbackGroup allows parallel calls)
        if not self._mode_change_lock.acquire(blocking=False):
            response.success = False
            response.message = "Mode change already in progress, ignoring duplicate request"
            self.get_logger().warning(response.message)
            return response
        self._switch_generation += 1  # supersede any in-flight relocalization waiter

        try:
            target_mode = request.mode.strip().lower()
            # Validate mode
            if target_mode not in VALID_NAVIGATION_MODES:
                response.success = False
                response.message = (
                    f"Invalid mode '{target_mode}'. Use 'navigation', 'mapping', 'autonomous_mapping', or 'mapfree'"
                )
                self.get_logger().error(response.message)
                return response

            if self.current_map is None and target_mode not in MAPPING_MODES:
                target_mode = "mapping"

            # Check if trying to switch to navigation but no maps are available
            if target_mode == "navigation" and not self.available_maps:
                response.success = False
                response.message = (
                    "Cannot switch to navigation mode - no maps available. Create a map first using mapping mode."
                )
                self.get_logger().error(response.message)
                return response

            # Don't restart if already in the requested mode
            if self.current_mode == target_mode and not first_start:
                if target_mode in MAPPING_MODES:
                    limit_ok, limit_message = self._set_mapping_speed_limit(True)
                    response.success = limit_ok
                    response.message = (
                        f"Already in {target_mode} mode" if limit_ok else f"Mapping mode is not safe: {limit_message}"
                    )
                else:
                    limit_ok, limit_message = self._release_mapping_speed_limit_after_stable_mode()
                    response.success = True
                    response.message = f"Already in {target_mode} mode"
                    if not limit_ok:
                        response.message += f"; Slow drive envelope remains active ({limit_message})"
                self.get_logger().info(response.message)
                return response

            # A stale current_map (e.g. deleted from disk) would fail every map
            # load attempt; fall back to a map that actually exists.
            if target_mode == "navigation" and self.current_map not in self.available_maps:
                fallback = self.available_maps[0]
                self.get_logger().warning(
                    f"Current map '{self.current_map}' not found on disk, falling back to '{fallback}'"
                )
                self.current_map = fallback
                self.save_last_map(fallback)

            # Close router admission before checking/cancelling active goals. If
            # cancellation ran first, a new goal could be admitted in the gap
            # between its final status check and lifecycle teardown.
            previous_mode = self.current_mode
            limit_ok, limit_message = self._set_mapping_speed_limit(True)
            if not limit_ok:
                response.success = False
                response.message = f"Mode switch aborted before transition: {limit_message}"
                if first_start:
                    # auto_start_mode already tore every lifecycle stack down.
                    # Keep the persisted desired mode untouched, but publish
                    # truthful runtime state so a later request does not take
                    # the same-mode fast path without starting its stack.
                    self.current_mode = "none"
                    self.publish_status()
                self.get_logger().error(response.message)
                return response
            self.current_mode = "switching"
            self.publish_status()

            # Cancel active nav goals before tearing down their lifecycle nodes.
            # If cancellation can't be confirmed (service unreachable, or goals
            # not terminal in time), restore the still-running previous mode so
            # callers can Stop explicitly and retry.
            cancelled_ok, cancel_message = self._cancel_active_navigation()
            if not cancelled_ok:
                response.success = False
                response.message = f"Mode switch aborted: could not stop active navigation ({cancel_message})"
                self.current_mode = previous_mode
                self.publish_status()
                if previous_mode in {NavigationMode.NAV.value, NavigationMode.MAPFREE.value}:
                    limit_ok, limit_message = self._release_mapping_speed_limit_after_stable_mode()
                    if not limit_ok:
                        response.message += f"; Slow drive envelope remains active ({limit_message})"
                self.get_logger().error(response.message)
                return response

            # For mapfree, launch local-only Nav2 (planner, controller, costmaps) without map/AMCL
            if target_mode == "mapfree":
                self.get_logger().info("Starting mapfree local navigation stack...")
                success, message = self.request_mode_startup(NavigationMode.MAPFREE)

                if success:
                    response.success = True
                    response.message = "Switched to mapfree mode (local Nav2 running)"
                    self.current_mode = "mapfree"
                    self.save_last_mode("mapfree")
                else:
                    response.success = False
                    response.message = f"Failed to start mapfree local navigation: {message}"
                    self.current_mode = "none"

                self.publish_status()
                if success:
                    limit_ok, limit_message = self._release_mapping_speed_limit_after_stable_mode()
                    if not limit_ok:
                        response.message += f"; Slow drive envelope remains active ({limit_message})"
                self.get_logger().info(response.message)
                return response

            # Map target_mode string to NavigationMode enum
            mode_enum_map = {
                "navigation": NavigationMode.NAV,
                "mapping": NavigationMode.MAPPING,
                "autonomous_mapping": NavigationMode.AUTONOMOUS_MAPPING,
            }
            target_mode_enum = mode_enum_map.get(target_mode)

            self.get_logger().info(f"Starting {target_mode} mode...")
            success, message = self.request_mode_startup(target_mode_enum)

            if success:
                response.success = True
                response.message = f"Successfully switched to {target_mode} mode"
                if target_mode == "navigation":
                    response.message += f" with map '{self.current_map}'"
                    # Initialize BasicNavigator for navigation mode
                    try:
                        if self.navigator is None:
                            self.navigator = BasicNavigator()
                        self.get_logger().info("BasicNavigator initialized for navigation mode")
                    except Exception as e:
                        self.get_logger().warning(f"Could not initialize BasicNavigator: {e}")
                elif starts_new_mapping_session(previous_mode, target_mode, first_start=first_start):
                    # Every slam_toolbox activation is a new coordinate frame.
                    # Manual <-> autonomous mapping keeps the same live SLAM
                    # node, so that handoff must retain the session identity
                    # and its staged spatial memories through save/finalize.
                    self._mapping_session_started = time.time()
                    self.mapping_session_publisher.publish(
                        String(data=json.dumps({"started": self._mapping_session_started}))
                    )
                self.current_mode = target_mode
                self.save_last_mode(target_mode)
                self.publish_status()
                if target_mode == NavigationMode.NAV.value:
                    limit_ok, limit_message = self._release_mapping_speed_limit_after_stable_mode()
                    if not limit_ok:
                        response.message += f"; Slow drive envelope remains active ({limit_message})"
                self.get_logger().info(response.message)
            else:
                response.success = False
                response.message = f"Failed to start {target_mode} mode: {message}"
                if not first_start and previous_mode in MAPPING_MODES and target_mode in MAPPING_MODES:
                    # Both mapping stacks share slam_toolbox. A partial
                    # expansion or contraction can fail after changing only
                    # the autonomous Nav2 nodes while SLAM remains in the same
                    # coordinate frame. Restore the previous stack and retain
                    # that frame's session identity so a retry cannot wipe
                    # staged memories.
                    rollback_ok, rollback_message = self.request_mode_startup(NavigationMode(previous_mode))
                    if rollback_ok:
                        self.current_mode = previous_mode
                        response.message += f"; restored {previous_mode} mode"
                    else:
                        self.current_mode = "none"
                        response.message += f"; failed to restore {previous_mode} mode: {rollback_message}"
                else:
                    self.current_mode = "none"
                self.publish_status()
                self.get_logger().error(response.message)

        except Exception as e:
            response.success = False
            response.message = f"Error switching modes: {str(e)}"
            self.get_logger().error(response.message)
            self.current_mode = "none"
            self.publish_status()
        finally:
            self._mode_change_lock.release()

        self.get_logger().debug("returning from change mode callback")
        return response

    def __del__(self):
        """Cleanup when node is destroyed"""
        self.get_logger().info("Using __del__ to delete mode manager")
        try:
            for mode in NavigationMode:
                self.shutdown_mode(mode.value)
        except Exception as e:
            self.get_logger().warning(f"Error during cleanup in __del__: {e}")


def main(args=None):
    rclpy.init(args=args)

    mode_manager = ModeManager()

    # Import and create NavigateToPoseRouter to run in the same process
    from mars_nav.navigate_to_pose_router import NavigateToPoseRouter

    navigate_to_pose_router = NavigateToPoseRouter()
    mode_manager.set_navigation_mode_sink(navigate_to_pose_router.set_current_mode)
    mode_manager.set_navigation_pending_goals_getter(navigate_to_pose_router.get_pending_goal_count)

    try:
        executor = MultiThreadedExecutor(num_threads=4)
        executor.add_node(mode_manager)
        executor.add_node(navigate_to_pose_router)
        mode_manager._executor = executor
        executor.spin()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Unexpected error in main: {e}")
        traceback.print_exc()
        # Attempt to spin briefly to allow cleanup callbacks to process
        try:
            print("Spinning executor for graceful shutdown...")
            executor.shutdown(timeout_sec=7.0)
        except Exception as shutdown_error:
            print(f"Error during shutdown spin: {shutdown_error}")
        try:
            if getattr(mode_manager, "_executor", None) is not None:
                mode_manager._executor.remove_node(mode_manager)
                mode_manager._executor.remove_node(navigate_to_pose_router)
                mode_manager._executor.shutdown()
        except Exception:
            pass

        mode_manager.destroy_node()
        navigate_to_pose_router.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
