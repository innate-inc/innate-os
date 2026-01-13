#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from std_msgs.msg import String
from brain_messages.srv import ChangeMap, ChangeNavigationMode, SaveMap, DeleteMap
from nav2_msgs.srv import LoadMap
from lifecycle_msgs.msg import Transition, State
from lifecycle_msgs.srv import GetState, ChangeState
import subprocess
import signal
import os
import time
import glob
import json
from enum import Enum
from typing import Tuple
from nav2_simple_commander.robot_navigator import BasicNavigator

from maurice_nav.service_utils import call_service, get_node_state, send_lifecycle_transition, transition_node

# TF2 imports for transform lookup
import tf2_ros
from geometry_msgs.msg import Pose
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry


# TODO: move this into launch file?
map_server_node = 'navigation_map_server'
bt_node = 'bt_navigator'
modes_nodes = {
    'mapping': ['slam_toolbox'],
    'mapfree': [
            'null_map_node',
            'mapfree/planner_server',  # TBD DO WE WANT TO CLEAR COSTMAPS OR NAH # def has to be unconfigured to reload static map (unless we want to do update topics and stuff. which might be worth doing in iter 2) - do MAP_UPDATES
            'controller_server',  # doesn't have to be deactivated, theoretically action should be cancelled by bt before it dies, and this won't receive a new path to follow - BUT IT HAS COSTMAP!!!
            'mapfree/bt_navigator',              # could stay on but underlying actions r gonna fail and be in a weird state; 
            'behavior_server',            # i guess this can just be deactivated - WHY??
            'velocity_smoother',           # might be able to leave it running throughout any changes
    ],
    'navigation': [                      # on map switch
            'navigation_map_server',      # load_map topic 
            'navigation_grid_localizer',  # localize Trigger service - USE THE TRIGGER SERVICE AND RELOAD MAP SERVICE
            'navigation_amcl',            # either restart or make sure its not first_map_only and send /map and /initialpose
            'mapfree/planner_server',
            'planner_server',  # TBD DO WE WANT TO CLEAR COSTMAPS OR NAH # def has to be unconfigured to reload static map (unless we want to do update topics and stuff. which might be worth doing in iter 2) - do MAP_UPDATES
            'controller_server',  # doesn't have to be deactivated, theoretically action should be cancelled by bt before it dies, and this won't receive a new path to follow - BUT IT HAS COSTMAP!!!
            'bt_navigator',              # could stay on but underlying actions r gonna fail and be in a weird state; 
            'mapfree/bt_navigator',
            'behavior_server',            # i guess this can just be deactivated - WHY??
            'velocity_smoother',           # might be able to leave it running throughout any changes
            # TODO: kill any running BTs or behaviors on switch

    ],
}
class NavigationMode(Enum):
    NAV = "navigation"
    MAPPING = "mapping"
    MAPFREE = "mapfree"

class ModeManager(Node):
    def __init__(self):
        self.log_num = 0
        super().__init__('mode_manager')

        # Service clients created in callbacks need a re-entrant callback group,
        # otherwise the response callback can be blocked by the running service callback.
        # self._calls_going_outside_group = MutuallyExclusiveCallbackGroup()
        # self._internal_callbacks_group = MutuallyExclusiveCallbackGroup()
        self._calls_going_outside_group = ReentrantCallbackGroup()
        self._internal_callbacks_group = ReentrantCallbackGroup()

        # Will be set in main() after adding this node to an executor.
        self._executor = None
        
        # Pre-create service clients and store in dictionary
        self._service_clients = {}
        
        # Service to switch modes
        self.mode_service = self.create_service(
            ChangeNavigationMode,
            '/nav/change_mode',
            self.change_mode_callback,
            callback_group=self._internal_callbacks_group
        )
        
        # Service to change maps in navigation mode
        self.map_service = self.create_service(
            ChangeMap,
            '/nav/change_navigation_map',
            self.change_map_callback
        )
        
        # Service to save current map in mapping mode
        self.save_map_service = self.create_service(
            SaveMap,
            '/nav/save_map',
            self.save_map_callback
        )
        
        # Service to delete a map
        self.delete_map_service = self.create_service(
            DeleteMap,
            '/nav/delete_map',
            self.delete_map_callback
        )
        
        # Publisher to announce current mode
        self.mode_publisher = self.create_publisher(String, '/nav/current_mode', 10)
        
        # Publisher to announce available maps
        self.maps_publisher = self.create_publisher(String, '/nav/available_maps', 10)
        
        # Publisher to announce current map
        self.current_map_publisher = self.create_publisher(String, '/nav/current_map', 10)
        
        # Pre-create service clients for all nodes and service types we'll use
        self._init_service_clients()
        
        # Use environment variable if set, otherwise construct from HOME
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        
        # Maps directory
        self.maps_dir = os.path.join(maurice_root, 'maps')
        
        # Mode persistence file
        self.mode_file = os.path.join(maurice_root, '.last_mode')
        
        # Map persistence file  
        self.map_file = os.path.join(maurice_root, '.last_map')
        
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

        # --- TF2: Mapping pose publisher ---
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        self.mapping_pose_pub = self.create_publisher(Odometry, '/mapping_pose', 10)
        # Subscribe to odometry topic (for mapping_pose publishing)
        self.odom_sub = self.create_subscription(
            Odometry,
            '/odom',
            self.odom_callback,
            20  # queue size
        )
        
        self.get_logger().info('Mode Manager started with map management capabilities.')
        self.get_logger().info('- Call /nav/change_mode service to switch modes ("navigation" or "mapping")')
        self.get_logger().info('- Call /nav/change_navigation_map service to change map (restarts navigation if running)')
        self.get_logger().info('- Call /nav/save_map service to save current map with new name (mapping mode only, set overwrite=true to replace existing maps)')
        self.get_logger().info('- Call /nav/delete_map service to delete a saved map (cannot delete active map while navigation is running)')
        self.get_logger().info(f'- Current mode: {self.current_mode} (loaded from persistence)')
        self.get_logger().info(f'- Current map: {self.current_map} (loaded from persistence)')
        self.get_logger().info(f'- Available maps: {self.available_maps}')
        
        # Auto-start in the saved mode after a short delay
        self.startup_timer = self.create_timer(3.0, self.auto_start_mode, callback_group=self._internal_callbacks_group)

    def odom_callback(self, msg):
        # Only publish mapping_pose in mapping mode
        if getattr(self, 'current_mode', None) != 'mapping':
            return
        try:
            tf_time = rclpy.time.Time()
            tf: TransformStamped = self.tf_buffer.lookup_transform('map', 'base_link', tf_time)
            odom_msg = Odometry()
            odom_msg.header.stamp = msg.header.stamp
            odom_msg.header.frame_id = 'map'
            odom_msg.child_frame_id = 'base_link'
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
                with open(self.mode_file, 'r') as f:
                    saved_mode = f.read().strip()
                    if saved_mode in ["navigation", "mapping", "mapfree"]:
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
                with open(self.map_file, 'r') as f:
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
            with open(self.mode_file, 'w') as f:
                f.write(mode)
            self.get_logger().debug(f"Saved mode: {mode}")
        except Exception as e:
            self.get_logger().error(f"Error saving mode: {e}")

    def save_last_map(self, map_name):
        """Save the current map to file for persistence"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.map_file), exist_ok=True)
            with open(self.map_file, 'w') as f:
                f.write(map_name)
            self.get_logger().debug(f"Saved map: {map_name}")
        except Exception as e:
            self.get_logger().error(f"Error saving map: {e}")

    def auto_start_mode(self):
        """Auto-start the mode manager in the last saved mode"""
        self.startup_timer.cancel()  # One-time execution

        self._cleanup_orphaned_processes()
        
        # Check if we want to start navigation but have no maps available
        # In this case, automatically switch to mapping mode
        if self.current_mode == "navigation" and not self.available_maps:
            self.get_logger().warn("No maps available for navigation - automatically starting in mapping mode")
            self.get_logger().info("Create a map first, then switch to navigation mode")
            self.current_mode = "mapping"
        
        if self.current_mode in ["navigation", "mapping"]:
            self.get_logger().info(f"Auto-starting in {self.current_mode} mode...")
            # Simulate a service request
            request = ChangeNavigationMode.Request()
            request.mode = self.current_mode
            response = ChangeNavigationMode.Response()
            self.change_mode_callback(request, response, first_start = True)
        elif self.current_mode == "mapfree":
            self.get_logger().info("Auto-starting in mapfree mode (local Nav2)")
            request = ChangeNavigationMode.Request()
            request.mode = self.current_mode
            response = ChangeNavigationMode.Response()
            self.change_mode_callback(request, response, first_start = True)

    def publish_status(self):
        """Publish current mode, available maps, and current map"""
        # self.log_num += 1
        # if not (self.log_num % 10): 
        # self.get_logger().info("Publishing status every second....", throttle_duration_sec = 10)
        # Publish current mode
        mode_msg = String()
        mode_msg.data = self.current_mode
        self.mode_publisher.publish(mode_msg)
        
        # Publish available maps as JSON
        maps_msg = String()
        maps_msg.data = json.dumps({"available_maps": self.available_maps})
        self.maps_publisher.publish(maps_msg)
        
        # Publish current map
        current_map_msg = String()
        current_map_msg.data = self.current_map
        self.current_map_publisher.publish(current_map_msg)

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
            service_key = f'/{node_name}/get_state'
            if service_key not in self._service_clients:
                client = self.create_client(
                    GetState,
                    service_key,
                    callback_group=self._calls_going_outside_group
                )
                self._service_clients[service_key] = client
            
            # ChangeState service
            service_key = f'/{node_name}/change_state'
            if service_key not in self._service_clients:
                client = self.create_client(
                    ChangeState,
                    service_key,
                    callback_group=self._calls_going_outside_group
                )
                self._service_clients[service_key] = client
            
            # LoadMap service (for map_server nodes)
            if 'map_server' in node_name:
                service_key = f'/{node_name}/load_map'
                if service_key not in self._service_clients:
                    client = self.create_client(
                        LoadMap,
                        service_key,
                        callback_group=self._calls_going_outside_group
                    )
                    self._service_clients[service_key] = client
        
        self.get_logger().info(f"Initialized {len(self._service_clients)} service clients")

    def _call_service(self, service_type, service_name: str, request, timeout_sec: float = 2.0):
        """
        Generic helper to call any ROS2 service with timeout.
        Uses pre-created clients from the dictionary.
        Args:
            service_type: The service type class (e.g., GetState, ChangeState)
            service_name: Full service name (e.g., '/node_name/get_state')
            request: The service request object
            timeout_sec: Timeout for both service availability and response
        Returns:
            The service response if successful, None otherwise
        """
        client = self._service_clients[service_name]
        
        try:
            if not client.wait_for_service(timeout_sec=timeout_sec):
                self.get_logger().info(f"Service '{service_name}' not available")
                return None
            
            # result = client.call(request)
            future = client.call_async(request)
            start_time = time.time()
            while not future.done():
                # self.get_logger().info(f"Service not done yet")
                elapsed = time.time() - start_time
                if elapsed >= timeout_sec:
                    self.get_logger().warning(f"Timeout waiting for service '{service_name}' after {elapsed:.2f}s")
                    return None
                time.sleep(.1)
            
            if future.done():
                result = future.result()
                if result is not None:
                    return result
                else:
                    self.get_logger().info(f"Service '{service_name}' returned None")
                    return None
            else:
                self.get_logger().info(f"Timeout calling service '{service_name}'")
                return None
                
        except Exception as e:
            self.get_logger().warning(f"Exception calling service '{service_name}': {e}")
            return None

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
                
                self.get_logger().info(f"{node_name} state {current_state_id}")
                
                # Transition to UNCONFIGURED (handles both ACTIVE and INACTIVE)
                if current_state_id != State.PRIMARY_STATE_UNCONFIGURED:
                    self.get_logger().info(f"Shutting down {node_name}")
                    success = transition_node(self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_UNCONFIGURED)
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

    def request_mode_startup(self, mode: NavigationMode) -> Tuple[bool, str]:
        """
        Request startup of navigation nodes for the given mode.
        First configures all nodes in forward order, then activates them one by one.
        Args:
            mode: The mode type - must be NavigationMode.NAV, NavigationMode.MAPPING, or NavigationMode.MAPFREE
        Returns: (success: bool, message: str)
        """

        try:
            self.get_logger().info(f"Requesting {mode.value} mode startup")
            
            # Stop other modes first
            # for other_mode in NavigationMode:
            #     if other_mode != mode:
            #         self.shutdown_mode(other_mode.value)
            # self.get_logger().info(f"Shut down other nodes")

            all_nodes_except_target = []
            for mode_name, nodes in modes_nodes.items():
                all_nodes_except_target.extend(nodes)
            for node in modes_nodes[mode.value]:
                # implement this: remove `node` from all_nodes_except_target
                if node in all_nodes_except_target:
                    all_nodes_except_target.remove(node)
            
            
            # Get nodes for this mode
            nodes = modes_nodes.get(mode.value, [])
            if not nodes:
                msg = f"No nodes configured for mode '{mode.value}'"
                self.get_logger().error(msg)
                return False, msg
            
            node_names = nodes

            
            failures = []
            for node_name in all_nodes_except_target:
                # Transition node to unconfigured
                success = transition_node(self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_UNCONFIGURED)
                if not success:
                    failures.append(node_name)
                    self.get_logger().warning(f"Failed to transition {node_name} to unconfigured")

            # Phase 1: Configure all nodes in forward order
            self.get_logger().info(f"Configuring {len(node_names)} nodes for {mode.value} mode")
            for node_name in node_names:
                success = transition_node(self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_INACTIVE, only_up = True)
                if not success:
                    failures.append(node_name)
                    self.get_logger().warning(f"Failed to configure {node_name}, continuing...")
            self.get_logger().info(f"Configured nodes")
            
            
            # Phase 2: Activate nodes in forward order
            map_load_success = False
            
            self.get_logger().info(f"Activating {len(node_names)} nodes for {mode.value} mode")
            for node_name in node_names:
                if node_name in failures:
                    self.get_logger().warning(f"{node_name} failed configuration. Not proceeding further.")
                    break # don't process the rest of the nodes
                success = transition_node(self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_ACTIVE)
                if not success:
                    failures.append(node_name)
                    self.get_logger().warning(f"Failed to activate {node_name}. Not proceeding further.")
                    break
                
                # Load map immediately after map server is activated (navigation mode only)
                if mode == NavigationMode.NAV and 'map_server' in node_name and success:
                    map_load_success = self._load_map_on_server(node_name)
                    if not map_load_success:
                        failures.append(f"{node_name}_map_load")
            
            self.get_logger().info(f"Activated nodes")
            
            if failures:
                message = f"{mode.value} mode started with {len(failures)} activation failures: {failures}"
                self.get_logger().warning(message)
            else:
                map_status = "loaded" if map_load_success else "not loaded" if mode == NavigationMode.NAV else "N/A"
                message = f"{mode.value} mode started successfully (map: {map_status})"
                self.get_logger().info(message)
            
            return len(failures) == 0, message
            
        except Exception as e:
            error_msg = f"Error requesting {mode.value} startup: {str(e)}"
            self.get_logger().error(error_msg)
            return False, error_msg
    
    def _load_map_on_server(self, node_name: str, max_retries: int = 20) -> bool:
        """
        Load the current map on the specified map server node.
        Retries until successful or max_retries is reached.
        Args:
            node_name: Name of the map server node
            max_retries: Maximum number of retry attempts
        Returns: True if map loaded successfully, False otherwise
        """

        if node_name is None:
            return False

        map_request = LoadMap.Request()
        map_request.map_url = os.path.join(self.maps_dir, self.current_map)
        
        self.get_logger().info(f"Loading map: {self.current_map} on {node_name}")
        
        retry_count = 0
        while retry_count <= max_retries:
            map_result = call_service(
                self._service_clients,
                self.get_logger(),
                f'/{node_name}/load_map',
                map_request,
                timeout_sec=5.0
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

    def _efficient_map_switch(self) -> Tuple[bool, str]:
        """
        Efficiently switch maps by transitioning bt_navigator down, loading map, then bringing all nodes up.
        
        Algorithm:
        1. Transition bt_navigator to inactive
        2. Load new map on map_server
        3. Transition all nodes to active
        """
        nodes = modes_nodes.get('navigation', [])
        if not nodes:
            return False, "No navigation nodes configured"
        
        failures = []
        
        # Step 1: Transition bt_navigator down to inactive
        self.get_logger().info(f"Step 1: Transitioning {bt_node} to inactive")
        success = transition_node(self._service_clients, self.get_logger(), bt_node, State.PRIMARY_STATE_INACTIVE)
        if not success:
            failures.append('bt_navigator')
            self.get_logger().warning(f"Failed to transition bt_navigator down")
        
        # Step 2: Load new map
        self.get_logger().info("Step 2: Loading new map")
        map_load_success = self._load_map_on_server(map_server_node)
        if not map_load_success:
            failures.append(f"{map_server_node}_map_load")
        
        # Step 3: Transition all nodes to active
        self.get_logger().info("Step 3: Activating all nodes")
        for node_name in nodes:
            success = transition_node(self._service_clients, self.get_logger(), node_name, State.PRIMARY_STATE_ACTIVE, only_up=True)
            if not success:
                failures.append(node_name)
                self.get_logger().warning(f"Failed to activate {node_name}")
        
        if failures:
            return False, f"Map switch completed with failures: {failures}"
        else:
            return True, f"Map switched successfully to {self.current_map}"

    def change_map_callback(self, request, response):
        """
        Service callback to change the map for navigation mode
        request.map_name should contain the map filename (e.g., "home.yaml")
        """
        try:
            requested_map = request.map_name.strip()
            
            # Validate that the requested map exists
            if requested_map not in self.available_maps:
                response.success = False
                response.message = f"Error: Map '{requested_map}' not found. Available maps: {self.available_maps}"
                self.get_logger().error(response.message)
                return response
            
            # Update the current map
            old_map = self.current_map
            self.current_map = requested_map
            
            # Save the new map choice for persistence
            self.save_last_map(requested_map)
            
            # If we're in navigation mode, use efficient map switch
            if self.current_mode == "navigation":
                self.get_logger().info(f"Efficiently switching to new map: {requested_map}")
                
                success, message = self._efficient_map_switch()
                
                if success:
                    response.success = True
                    response.message = f"Successfully changed map to '{requested_map}'"
                    self.get_logger().info(response.message)
                else:
                    response.success = False
                    response.message = f"Failed to switch to map '{requested_map}': {message}"
                    self.get_logger().error(response.message)
            else:
                # If not in navigation mode, just update the map for next time navigation starts
                response.success = True
                response.message = f"Map set to '{requested_map}' for next navigation session"
                self.get_logger().info(response.message)
                
        except Exception as e:
            response.success = False
            response.message = f"Error changing map: {str(e)}"
            self.get_logger().error(response.message)
            
        return response

    def delete_map_callback(self, request, response):
        """
        Service callback to delete a saved map (.yaml and associated image)
        Accepts either the map base name (e.g., "home") or filename (e.g., "home.yaml")
        """
        try:
            requested_name = request.map_name.strip()
            if not requested_name:
                response.success = False
                response.message = "Map name cannot be empty"
                self.get_logger().error(response.message)
                return response

            # Normalize to YAML filename
            map_yaml_name = requested_name if requested_name.endswith('.yaml') else f"{requested_name}.yaml"

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
                fallback = None
                if "home.yaml" in remaining_maps:
                    fallback = "home.yaml"
                elif remaining_maps:
                    fallback = remaining_maps[0]
                else:
                    # No maps left, default to home.yaml placeholder
                    fallback = "home.yaml"

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

        return response

    def save_map_callback(self, request, response):
        """
        Service callback to save the current map with a new name
        Only works when in mapping mode
        request.map_name should contain the new map name (e.g., "my_new_map")
        request.overwrite: if true, allows overwriting existing maps
        """
        try:
            map_name = request.map_name.strip()
            
            # Validate we're in mapping mode
            if self.current_mode != "mapping":
                response.success = False
                response.message = f"Cannot save map - not in mapping mode. Current mode: {self.current_mode}"
                self.get_logger().error(response.message)
                return response
            
            # Validate map name
            if not map_name or not map_name.replace('_', '').replace('-', '').isalnum():
                response.success = False
                response.message = f"Invalid map name '{map_name}'. Use alphanumeric characters, underscores, and hyphens only."
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
                'ros2', 'run', 'nav2_map_server', 'map_saver_cli',
                '-f', map_path,
                '--ros-args', '-p', 'save_map_timeout:=5000.0'
            ]
            
            # Run the map saver command
            result = subprocess.run(
                save_cmd,
                capture_output=True,
                text=True,
                timeout=30  # 30 second timeout
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
                    
                    # Refresh available maps list
                    self.available_maps = self.discover_maps()
                    self.get_logger().info(f"Updated available maps: {self.available_maps}")
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
            
        return response

    def _cleanup_orphaned_processes(self):
        """Kill any orphaned navigation processes from previous mode_manager runs."""
        try:
            # Shutdown all modes gracefully
            self.get_logger().info('Attempting to shutdown all modes...')
            for mode in NavigationMode:
                self.shutdown_mode(mode.value)
            self.get_logger().info('Cleaned up all modes')
        except Exception as e:
            self.get_logger().warn(f'Cleanup warning: {e}')

    def change_mode_callback(self, request, response, first_start = False):
        """
        Service callback to switch between modes
        request.mode = "navigation": Switch to navigation mode
        request.mode = "mapping": Switch to mapping mode
        """

        self.get_logger().info("Attempting to change mode")
        try:
            target_mode = request.mode.strip().lower()
            
            # Validate mode
            if target_mode not in ["navigation", "mapping", "mapfree"]:
                response.success = False
                response.message = f"Invalid mode '{target_mode}'. Use 'navigation', 'mapping', or 'mapfree'"
                self.get_logger().error(response.message)
                return response
            
            # Check if trying to switch to navigation but no maps are available
            if target_mode == "navigation" and not self.available_maps:
                response.success = False
                response.message = "Cannot switch to navigation mode - no maps available. Create a map first using mapping mode."
                self.get_logger().error(response.message)
                return response
            
            # Don't restart if already in the requested mode
            if self.current_mode == target_mode and not first_start:
                response.success = True
                response.message = f"Already in {target_mode} mode"
                self.get_logger().info(response.message)
                return response

            # Set mode to switching
            self.current_mode = "switching"
            self.publish_status() # Immediately publish the change

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
                
                self.get_logger().info(response.message)
                return response

            # Map target_mode string to NavigationMode enum
            mode_enum_map = {
                "navigation": NavigationMode.NAV,
                "mapping": NavigationMode.MAPPING,
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
                self.current_mode = target_mode
                self.save_last_mode(target_mode)
                self.get_logger().info(response.message)
            else:
                response.success = False
                response.message = f"Failed to start {target_mode} mode: {message}"
                self.current_mode = "none"
                self.get_logger().error(response.message)

        except Exception as e:
            response.success = False
            response.message = f"Error switching modes: {str(e)}"
            self.get_logger().error(response.message)
            self.current_mode = "none"

        self.get_logger().info("returning from change mode callback")
        return response

    def __del__(self):
        """Cleanup when node is destroyed"""
        try:
            for mode in NavigationMode:
                self.shutdown_mode(mode.value)
        except Exception as e:
            pass  # Silently ignore cleanup errors during destruction
            # TODO: is this a good idea?

def main(args=None):
    rclpy.init(args=args)
    
    mode_manager = ModeManager()
    
    try:
        executor = MultiThreadedExecutor(num_threads=2)
        executor.add_node(mode_manager)
        mode_manager._executor = executor
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        # Clean up any running processes
        try:
            for mode in NavigationMode:
                mode_manager.shutdown_mode(mode.value)
        except Exception as e:
            pass  # Silently ignore cleanup errors during destruction
            # TODO: is this a good idea?

        try:
            if getattr(mode_manager, '_executor', None) is not None:
                mode_manager._executor.remove_node(mode_manager)
                mode_manager._executor.shutdown()
        except Exception:
            pass

        mode_manager.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main() 