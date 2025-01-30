#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from maurice_arm.dynamixel import Dynamixel
from maurice_arm.robot import Robot
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
import numpy as np

class LeaderArmNode(Node):
    def __init__(self):
        super().__init__('leader_arm')
        
        # Declare and get parameters
        self.declare_parameter('device_name', '/dev/arm')
        self.declare_parameter('baud_rate', 1000000)
        self.declare_parameter('control_frequency', 100.0)
        self.declare_parameter('servo_ids', [1, 2, 3, 4, 5, 6])
        
        device_name = self.get_parameter('device_name').value
        baud_rate = self.get_parameter('baud_rate').value
        control_freq = self.get_parameter('control_frequency').value
        self.servo_ids = self.get_parameter('servo_ids').value
        
        # Initialize Dynamixel and Robot
        dynamixel = Dynamixel.Config(
            baudrate=baud_rate,
            device_name=device_name
        ).instantiate()
        self.robot = Robot(dynamixel=dynamixel, servo_ids=self.servo_ids)
        
        # Publishers
        self.state_pub = self.create_publisher(JointState, '/leader/state', 10)
        self.cmd_pub = self.create_publisher(Float64MultiArray, '/maurice_arm/commands', 10)
        
        # Timer for state publishing
        self.timer = self.create_timer(1.0/control_freq, self.timer_callback)
        
        self.get_logger().info(f'Leader arm node initialized with device {device_name} at {baud_rate} baud')

    def timer_callback(self):
        try:
            # Read current positions and velocities
            positions = self.robot.read_position()
            velocities = self.robot.read_velocity()
            positions=np.array(positions)+np.array([-1024,1024,0,-1024,-1024,0])
            # Convert to radians (positions are in range [0, 4096])
            positions_rad = [(pos - 2048) * (2 * np.pi / 4096) for pos in positions]
            velocities_rad = [vel * (2 * np.pi / 4096) for vel in velocities]
            
            # Create and publish JointState message
            state_msg = JointState()
            state_msg.header.stamp = self.get_clock().now().to_msg()
            state_msg.name = [f'joint_{i+1}' for i in range(len(self.servo_ids))]
            state_msg.position = positions_rad
            state_msg.velocity = velocities_rad
            self.state_pub.publish(state_msg)
            
            # Create and publish command message
            cmd_msg = Float64MultiArray()
            cmd_msg.data = positions_rad  # Publishing current positions as commands
            self.cmd_pub.publish(cmd_msg)
            
        except Exception as e:
            self.get_logger().error(f'Error in timer callback: {str(e)}')

def main(args=None):
    rclpy.init(args=args)
    node = LeaderArmNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
