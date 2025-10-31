#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import math
import numpy as np
import os
import json

from geometry_msgs.msg import Vector3, Twist
from std_msgs.msg import Int32MultiArray, Float64MultiArray, String

# Import NetworkManager utilities for WiFi SSID
from maurice_bt_provisioner.nmcli_utils import nmcli_get_active_wifi_ssid

class AppControl(Node):
    KEYS_TO_EXTRACT = ['robot_name'] # Define keys to extract from JSON
    
    def __init__(self):
        super().__init__('app_control_node')
        
        # Cache for WiFi SSID to avoid frequent subprocess calls
        self._cached_wifi_ssid = None
        self._last_wifi_check_time = 0
        self._wifi_check_interval = 10.0  # Check WiFi every 10 seconds instead of every 1 second
        
        # Declare parameters
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        self.declare_parameter('data_directory', os.path.join(maurice_root, 'data'))
        
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
        
        self.get_logger().info("AppControl node started.")

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

    def joystick_callback(self, msg: Vector3):
        """
        Converts joystick input to velocity commands:
          - Linear velocity (x) is scaled from [-1, 1] to [-0.4, 0.4].
          - Angular velocity (z) is scaled from [-1, 1] to [-1.5, 1.5].
          - Applies deadband of 0.1 to filter out small inputs.
        """
        # Apply deadband
        x = 0.0 if abs(msg.x) < 0.1 else msg.x
        y = 0.0 if abs(msg.y) < 0.1 else msg.y

        twist_msg = Twist()
        twist_msg.linear.x = y * 0.4
        twist_msg.angular.z = -x * 1.5
        
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
        
        self.get_logger().info(
            f"Leader positions: {msg.data} -> Transformed (rad): {cmd_msg.data}"
        )

    def publish_robot_info_callback(self):
        """
        Reads robot_info.json, extracts specified keys, and publishes them as a JSON string.
        For wifi_ssid, gets the current WiFi SSID from NetworkManager.
        Logs errors if file/JSON processing fails or keys are missing.
        Publishes "{}" if no keys are found or an error occurs.
        """
        data_directory_param = self.get_parameter('data_directory').get_parameter_value().string_value
        data_dir = os.path.expanduser(data_directory_param)
        robot_info_file_path = os.path.join(data_dir, 'robot_info.json')

        data_to_publish_dict = {}
        final_json_string_to_publish = "{}"

        try:
            with open(robot_info_file_path, 'r') as f:
                content = json.load(f)
            
            # Extract keys from JSON file
            for key in self.KEYS_TO_EXTRACT:
                if key in content:
                    data_to_publish_dict[key] = content[key]
                else:
                    self.get_logger().warn(f"Key '{key}' not found in {robot_info_file_path}.")
            
            # Always include WiFi SSID (separate from JSON extraction)
            wifi_ssid = self.get_cached_wifi_ssid()
            if wifi_ssid is not None:
                data_to_publish_dict['wifi_ssid'] = wifi_ssid
            
            if data_to_publish_dict:
                final_json_string_to_publish = json.dumps(data_to_publish_dict)

        except Exception as e:
            self.get_logger().error(f"Error processing robot info from {robot_info_file_path}: {str(e)}")
            # final_json_string_to_publish remains "{}" as initialized

        msg = String()
        msg.data = final_json_string_to_publish
        self.robot_info_pub.publish(msg)
        # Optional: log what was published if it's not an empty dict or if debugging
        # if final_json_string_to_publish != "{}":
        #     self.get_logger().info(f"Published robot data: {final_json_string_to_publish}")

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
