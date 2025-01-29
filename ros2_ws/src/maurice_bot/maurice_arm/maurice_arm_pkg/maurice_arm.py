#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from dynamixel import Dynamixel
from robot import Robot
import numpy as np


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
        
        # Initialize Dynamixel interface
        dynamixel = Dynamixel.Config(
            baudrate=baud_rate,
            device_name=device_name
        ).instantiate()
        
        # Initialize robot interface with servo IDs from parameters
        self.robot = Robot(dynamixel=dynamixel, servo_ids=servo_ids)
        
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

    def timer_callback(self):
        """Publish current joint states"""
        try:
            # Read current positions and velocities
            positions = self.robot.read_position()
            velocities = self.robot.read_velocity()
            
            # Convert positions from Dynamixel units to radians
            positions = [(pos - 2048) * (2 * np.pi / 4096) for pos in positions]
            
            # Update message
            self.joint_state_msg.header.stamp = self.get_clock().now().to_msg()
            self.joint_state_msg.position = positions
            self.joint_state_msg.velocity = velocities
            
            # Publish
            self.state_pub.publish(self.joint_state_msg)
            
        except Exception as e:
            self.get_logger().error(f'Error in timer callback: {str(e)}')

    def command_callback(self, msg: Float64MultiArray):
        """Handle incoming position commands"""
        try:
            # Convert positions from radians to Dynamixel units and cast to integers
            positions = [int((pos * 4096/(2*np.pi)) + 2048) for pos in msg.data]
            
            # Send positions to the robot
            self.robot.write_position(positions)
                
        except Exception as e:
            self.get_logger().error(f'Error in command callback: {str(e)}')
