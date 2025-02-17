#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import math
import numpy as np

from geometry_msgs.msg import Vector3, Twist
from std_msgs.msg import Int32MultiArray, Float64MultiArray

class AppControl(Node):
    def __init__(self):
        super().__init__('app_control_node')
        
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
        
        # Publisher for leader arm commands (Float64MultiArray) on /maurice_arm/commands
        self.cmd_pub = self.create_publisher(
            Float64MultiArray,
            '/maurice_arm/commands',
            10
        )
        
        self.get_logger().info("AppControl node started.")

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
        Receives leader positions, applies an offset and converts to radians:
          - Offsets: [-1024, 1024, 0, -1024, -1024, 0]
          - Conversion: (position - 2048) * (2 * pi / 4096)
        Then publishes the transformed values as commands.
        """
        expected_length = 6  # Adjust if your leader arm has a different number of joints
        if len(msg.data) != expected_length:
            self.get_logger().error(f"Received {len(msg.data)} positions; expected {expected_length}.")
            return
        
        # Convert positions to a NumPy array for easy math.
        positions = np.array(msg.data, dtype=float)
        offsets = np.array([-1024, 1024, 0, -1024, -1024, 0], dtype=float)
        positions_corrected = positions + offsets
        
        # Convert to radians:
        positions_rad = (positions_corrected - 2048) * (2 * math.pi / 4096)
        
        # Publish the transformed command
        cmd_msg = Float64MultiArray()
        cmd_msg.data = positions_rad.tolist()
        self.cmd_pub.publish(cmd_msg)
        
        self.get_logger().info(
            f"Leader positions: {msg.data} -> Transformed (rad): {cmd_msg.data}"
        )

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
