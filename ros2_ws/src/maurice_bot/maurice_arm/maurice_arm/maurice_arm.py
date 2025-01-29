#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
import numpy as np
import time

# Direct imports since files are in same directory
from dynamixel import Dynamixel
from robot import Robot


class MauriceArmNode(Node):
    def __init__(self):
        super().__init__('maurice_arm')

        # Declare and get parameters
        self.declare_parameter('device_name', '/dev/arm')
        self.declare_parameter('baud_rate', 1000000)
        self.declare_parameter('joint_limits_lower', [-3.14159, -1.57079, -2.35619, -1.57079, -3.14159, 0.0])
        self.declare_parameter('joint_limits_upper', [3.14159, 1.57079, 2.35619, 1.57079, 3.14159, 0.085])
        self.declare_parameter('servo_ids', [1, 2, 3, 4, 5, 6])
        self.declare_parameter('control_frequency', 100.0)

        device_name = self.get_parameter('device_name').value
        baud_rate = self.get_parameter('baud_rate').value
        servo_ids = self.get_parameter('servo_ids').value
        control_frequency = self.get_parameter('control_frequency').value
        
        # Store joint limits as class attributes
        self.joint_limits_lower = self.get_parameter('joint_limits_lower').value
        self.joint_limits_upper = self.get_parameter('joint_limits_upper').value

        # Initialize Dynamixel interface
        dynamixel = Dynamixel.Config(
            baudrate=baud_rate,
            device_name=device_name
        ).instantiate()
        
        # Initialize robot interface with servo IDs from parameters
        self.robot = Robot(dynamixel=dynamixel, servo_ids=servo_ids)
        
        # Wait 3 seconds then enable torque
        time.sleep(3.0)
        self.get_logger().info('Enabling torque on all motors')
        self.robot._enable_torque()
        
        # Create publishers and subscribers
        self.state_pub = self.create_publisher(
            JointState,
            '/maurice_arm/state',
            10
        )
        
        self.command_sub = self.create_subscription(
            Float64MultiArray,
            '/maurice_arm/commands',
            self.command_callback,
            10
        )

        # Create timer for state publishing using control frequency parameter
        self.timer = self.create_timer(1.0/control_frequency, self.timer_callback)
        
        # Initialize joint state message with 6 joints
        self.joint_state_msg = JointState()
        self.joint_state_msg.name = [f'joint_{i}' for i in range(1, 7)]  # 6 joints

        # Initialize latest command
        self.latest_command = None

    def timer_callback(self):
        """Publish current joint states and write latest command if available"""
        try:
            # Read current positions and velocities
            positions = self.robot.read_position()
            velocities = self.robot.read_velocity()
            
            # Convert positions from Dynamixel units to radians
            positions = [(pos - 2048) * (2 * np.pi / 4096) for pos in positions]
            
            # Convert velocities from Dynamixel units to radians per second
            # Dynamixel velocity units are in 0.229 rev/min
            # Convert to rad/s: (0.229 rev/min) * (2π rad/rev) * (1 min/60 sec)
            velocities = [float(vel) * 2 * np.pi / 4096 for vel in velocities]
            
            # Update message
            self.joint_state_msg.header.stamp = self.get_clock().now().to_msg()
            self.joint_state_msg.position = positions
            self.joint_state_msg.velocity = velocities
            
            # Publish
            self.state_pub.publish(self.joint_state_msg)

            # Write latest command if available
            if self.latest_command is not None:
                self.robot.set_goal_pos(self.latest_command)
                self.latest_command = None
            
        except Exception as e:
            self.get_logger().error(f'Error in timer callback: {str(e)}')

    def command_callback(self, msg: Float64MultiArray):
        """Store incoming position commands after checking joint limits"""
        try:
            # Check if command is within joint limits
            for i, pos in enumerate(msg.data):
                if pos < self.joint_limits_lower[i] or pos > self.joint_limits_upper[i]:
                    self.get_logger().warn(f'Joint {i+1} command {pos:.3f} exceeds limits [{self.joint_limits_lower[i]:.3f}, {self.joint_limits_upper[i]:.3f}]')
                    return  # Exit without updating latest_command if any joint exceeds limits

            # Convert positions from radians to Dynamixel units and cast to integers
            self.latest_command = [int((pos * 4096/(2*np.pi)) + 2048) for pos in msg.data]
                
        except Exception as e:
            self.get_logger().error(f'Error in command callback: {str(e)}')

def main(args=None):
    rclpy.init(args=args)
    node = MauriceArmNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
