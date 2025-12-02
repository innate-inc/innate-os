#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import math
import numpy as np
import os
import json
import subprocess

from geometry_msgs.msg import Vector3, Twist
from std_msgs.msg import Int32MultiArray, Float64MultiArray, String
from maurice_msgs.srv import SetRobotName

# Import NetworkManager utilities for WiFi SSID
from maurice_bt_provisioner.nmcli_utils import nmcli_get_active_wifi_ssid

def get_bluetooth_device_id():
    """
    Get the Bluetooth device ID (MAC address) of the first available adapter.
    Returns the MAC address as a string, or None if not available.
    """
    try:
        from bluezero import adapter
        available_adapters = list(adapter.Adapter.available())
        if available_adapters:
            return available_adapters[0].address
        return None
    except Exception as e:
        return None

def get_robot_version():
    """
    Get the current robot version.
    - If on main branch and there are tags, returns the latest tag
    - If in development (not on main), returns dev version using latest tag
    - Raises RuntimeError if no tags exist (this should not happen)
    """
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    
    # Get current branch
    result = subprocess.run(['git', 'branch', '--show-current'], 
                          cwd=maurice_root, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get current git branch: {result.stderr}")
    current_branch = result.stdout.strip()
    
    # Get all tags sorted by version
    result = subprocess.run(['git', 'tag', '--list', '--sort=-version:refname'], 
                          cwd=maurice_root, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get git tags: {result.stderr}")
    
    if not result.stdout.strip():
        raise RuntimeError("No git tags found - repository must have at least one tag")
    
    tags = result.stdout.strip().split('\n')
    latest_tag = tags[0] if tags else None
    
    # If on main branch and we have tags, return the latest tag
    if current_branch == 'main' and latest_tag:
        # Check if we're exactly on this tag
        result = subprocess.run(['git', 'describe', '--exact-match', '--tags', 'HEAD'], 
                              cwd=maurice_root, capture_output=True, text=True)
        if result.returncode == 0:
            return latest_tag
    
    # If we have tags, get the dev version using the latest tag
    if latest_tag:
        try:
            # Validate tag format
            parts = latest_tag.split('.')
            if len(parts) == 3:
                major, minor, patch = map(int, parts)
                return f"{major}.{minor}.{patch}-dev"
            else:
                raise RuntimeError(f"Invalid tag format: {latest_tag}. Expected format: x.y.z")
        except ValueError:
            raise RuntimeError(f"Invalid tag format: {latest_tag}. Expected format: x.y.z")
    
    raise RuntimeError("Failed to determine robot version")

class AppControl(Node):
    def __init__(self):
        super().__init__('app_control_node')
        
        # Load app configuration
        self._load_app_config()
        
        # Cache for WiFi SSID to avoid frequent subprocess calls
        self._cached_wifi_ssid = None
        self._last_wifi_check_time = 0
        self._wifi_check_interval = 10.0  # Check WiFi every 10 seconds instead of every 1 second
        
        # Declare parameters
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        self.declare_parameter('data_directory', os.path.join(maurice_root, 'data'))
        self.declare_parameter('robot_name', '')
        
        # Subscribe to joystick messages (Vector3)
        self.joystick_sub = self.create_subscription(
            Vector3,
            '/joystick',
            self.joystick_callback,
            10
        )
        
        # Subscribe to leader positions messages (Int32MultiArray)
        self.leader_sub = self.create_subscription(
            Int32MultiArray,
            '/leader_positions',
            self.leader_positions_callback,
            10
        )
        
        # Publisher for velocity commands (Twist) on /cmd_vel
        self.cmd_vel_pub = self.create_publisher(
            Twist,
            '/cmd_vel',
            10
        )
        
        # Publisher for leader arm commands (Float64MultiArray) on /mars/arm/commands
        self.cmd_pub = self.create_publisher(
            Float64MultiArray,
            '/mars/arm/commands',
            10
        )
        
        # Publisher for robot info
        self.robot_info_pub = self.create_publisher(
            String,
            '/robot/info',
            10
        )
        
        # Timer for publishing robot info
        self.robot_info_timer = self.create_timer(1.0, self.publish_robot_info_callback)
        
        # Service for setting robot name
        self.set_robot_name_srv = self.create_service(
            SetRobotName,
            '/set_robot_name',
            self.set_robot_name_callback
        )
        
        self.get_logger().info("AppControl node started.")

    def _load_app_config(self):
        """Load app configuration from config file."""
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        config_file_path = os.path.join(maurice_root, 'os_config.json')
        
        with open(config_file_path, 'r') as f:
            self.app_config = json.load(f)

    def get_cached_wifi_ssid(self):
        """
        Get WiFi SSID with caching to avoid frequent subprocess calls.
        Only checks for new SSID if the configured interval has passed.
        """
        import time
        current_time = time.time()
        
        # If we haven't checked recently or don't have a cached value, check now
        if (self._cached_wifi_ssid is None or 
            current_time - self._last_wifi_check_time > self._wifi_check_interval):
            
            new_ssid = nmcli_get_active_wifi_ssid()
            
            # Only log if the SSID actually changed or this is the first check
            if new_ssid != self._cached_wifi_ssid:
                if new_ssid is not None:
                    self.get_logger().info(f"WiFi SSID updated: {self._cached_wifi_ssid} -> {new_ssid}")
                else:
                    self.get_logger().warn("Could not retrieve WiFi SSID")
                
                self._cached_wifi_ssid = new_ssid
                self._last_wifi_check_time = current_time
        
        return self._cached_wifi_ssid

    def apply_curve(self, value, deadband=0.15):
        """
        Apply deadband and quadratic curve to input value.
        - Deadband filters out small inputs
        - Quadratic curve provides smooth, balanced response
        """
        if abs(value) < deadband:
            return 0.0
        
        # Normalize value beyond deadband to range [0, 1] or [-1, 0]
        sign = 1.0 if value > 0 else -1.0
        normalized = (abs(value) - deadband) / (1.0 - deadband)
        
        # Apply quadratic curve for balanced progressive response
        curved = normalized ** 2
        
        return sign * curved
    
    def joystick_callback(self, msg: Vector3):
        """
        Converts joystick input to velocity commands:
          - Linear velocity (x) is scaled from [-1, 1] to [-0.7, 0.7].
          - Angular velocity (z) is scaled from [-1, 1] to [-1.5, 1.5].
          - Applies deadband of 0.15 (15%) to filter out small inputs.
          - Applies cubic curve for smooth, progressive control response.
        """
        # Apply deadband and curve
        x = self.apply_curve(msg.x, deadband=0.15)
        y = self.apply_curve(msg.y, deadband=0.15)

        twist_msg = Twist()
        twist_msg.linear.x = y * 0.5  # Max forward speed: 0.5 m/s
        twist_msg.angular.z = -x * 1.0  # Max angular speed: 1.0 rad/s
        
        # Set other components to zero.
        twist_msg.linear.y = 0.0
        twist_msg.linear.z = 0.0
        twist_msg.angular.x = 0.0
        twist_msg.angular.y = 0.0
        
        self.cmd_vel_pub.publish(twist_msg)
        self.get_logger().info(
            f"Joystick: x={msg.x:.2f}, y={msg.y:.2f} -> cmd_vel: linear.x={twist_msg.linear.x:.2f}, angular.z={twist_msg.angular.z:.2f}"
        )

    def leader_positions_callback(self, msg: Int32MultiArray):
        """
        Receives leader positions and converts to radians:
          - Conversion: (position - 2048) * (2 * pi / 4096)
        Then publishes the transformed values as commands.
        
        Note: Offset application is currently disabled:
          - Offsets: [-1024, 1024, 0, -1024, -1024, 0]
        """
        expected_length = 6  # Adjust if your leader arm has a different number of joints
        if len(msg.data) != expected_length:
            self.get_logger().error(f"Received {len(msg.data)} positions; expected {expected_length}.")
            return
        
        # Convert positions to a NumPy array for easy math.
        positions = np.array(msg.data, dtype=float)
        
        # Offset application (currently disabled)
        # offsets = np.array([-1024, 1024, 0, -1024, -1024, 0], dtype=float)
        # positions_corrected = positions + offsets
        
        # Convert to radians:
        # positions_rad = (positions_corrected - 2048) * (2 * math.pi / 4096)
        positions_rad = (positions - 2048) * (2 * math.pi / 4096)
        
        # Publish the transformed command
        cmd_msg = Float64MultiArray()
        cmd_msg.data = positions_rad.tolist()
        self.cmd_pub.publish(cmd_msg)
        
    def publish_robot_info_callback(self):
        """
        Reads robot_info.json and os_config.json, extracts specified keys, and publishes them as a JSON string.
        - robot_name: from robot_info.json
        - minimum_app_version: from os_config.json
        - wifi_ssid: gets the current WiFi SSID from NetworkManager
        - version: from git
        - device_id: Bluetooth MAC address from system
        Logs errors if file/JSON processing fails or keys are missing.
        Publishes "{}" if no keys are found or an error occurs.
        """
        data_directory_param = self.get_parameter('data_directory').get_parameter_value().string_value
        data_dir = os.path.expanduser(data_directory_param)
        robot_info_file_path = os.path.join(data_dir, 'robot_info.json')

        data_to_publish_dict = {}
        final_json_string_to_publish = "{}"

        try:
            # Ensure data directory exists
            os.makedirs(data_dir, exist_ok=True)

            # If robot_info.json does not exist, create it with default values
            if not os.path.exists(robot_info_file_path):
                default_robot_info = {"robot_name": "MARS", "robot_id": None}
                with open(robot_info_file_path, 'w') as f:
                    json.dump(default_robot_info, f)
            
            # Read robot_name and robot_id from robot_info.json
            with open(robot_info_file_path, 'r') as f:
                robot_info = json.load(f)
            data_to_publish_dict['robot_name'] = robot_info.get('robot_name')
            data_to_publish_dict['robot_id'] = robot_info.get('robot_id')
            
            # Read minimum_app_version from os_config.json
            os_config = self.app_config
            data_to_publish_dict['minimum_app_version'] = os_config['minimum_app_version']
            
            # Include WiFi SSID
            wifi_ssid = self.get_cached_wifi_ssid()
            if wifi_ssid is not None:
                data_to_publish_dict['wifi_ssid'] = wifi_ssid
            
            # Include robot version
            robot_version = get_robot_version()
            data_to_publish_dict['version'] = robot_version
            
            # Include Bluetooth device ID
            device_id = get_bluetooth_device_id()
            if device_id is not None:
                data_to_publish_dict['device_id'] = device_id
            
            if data_to_publish_dict:
                final_json_string_to_publish = json.dumps(data_to_publish_dict)

        except Exception as e:
            self.get_logger().error(f"Error processing robot info: {str(e)}")
            # final_json_string_to_publish remains "{}" as initialized

        msg = String()
        msg.data = final_json_string_to_publish
        self.robot_info_pub.publish(msg)
        # Optional: log what was published if it's not an empty dict or if debugging
        # if final_json_string_to_publish != "{}":
        #     self.get_logger().info(f"Published robot data: {final_json_string_to_publish}")

    def set_robot_name_callback(self, request, response):
        """
        Service callback to change the robot name in robot_info.json.
        """
        try:
            data_directory_param = self.get_parameter('data_directory').get_parameter_value().string_value
            data_dir = os.path.expanduser(data_directory_param)
            robot_info_file_path = os.path.join(data_dir, 'robot_info.json')
            
            # Load current robot_info
            with open(robot_info_file_path, 'r') as f:
                robot_info = json.load(f)
            
            # Update robot name
            old_name = robot_info.get('robot_name', 'Not set')
            robot_info['robot_name'] = request.robot_name
            
            # Save updated robot_info
            with open(robot_info_file_path, 'w') as f:
                json.dump(robot_info, f, indent=2)
            
            response.success = True
            response.message = f"Robot name changed from '{old_name}' to '{request.robot_name}'"
            self.get_logger().info(response.message)
            
        except Exception as e:
            response.success = False
            response.message = f"Failed to change robot name: {str(e)}"
            self.get_logger().error(response.message)
        
        return response

def main(args=None):
    rclpy.init(args=args)
    node = AppControl()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
