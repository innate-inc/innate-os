#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
import pygame
from geometry_msgs.msg import Twist

class JoystickController(Node):
    def __init__(self):
        super().__init__('joystick_controller')
        
        # Declare and get parameters
        self.declare_parameter('motion_control.max_speed', 2.0)
        self.declare_parameter('motion_control.max_angular_speed', 2.5)
        
        self.max_speed = self.get_parameter('motion_control.max_speed').value
        self.max_angular_speed = self.get_parameter('motion_control.max_angular_speed').value
        
        # Parameters for joystick axes
        self.forward_axis = 1  # Left stick Y
        self.turn_axis = 0     # Left stick X
        
        # Initialize pygame and joystick
        pygame.init()
        pygame.joystick.init()
        
        # Initialize joystick
        self.joystick = None
        self.check_for_joystick()
        
        # Publisher for velocity commands
        self.twist_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        
        # Create timer for reading joystick input
        self.create_timer(0.1, self.timer_callback)
        
        self.get_logger().info('Joystick Controller initialized')
    
    def check_for_joystick(self):
        if pygame.joystick.get_count() > 0:
            self.joystick = pygame.joystick.Joystick(0)
            self.joystick.init()
            self.get_logger().info('Joystick found and initialized!')
        else:
            self.get_logger().warn('No joystick found!')
    
    def shape_input(self, x, max_val=1.0, deadzone=0.05):
        # Apply deadzone and basic scaling
        if abs(x) < deadzone:
            return 0.0
        return max_val * x
    
    def timer_callback(self):
        if self.joystick is None:
            return
            
        pygame.event.pump()
        
        # Read and scale joystick values
        forward = -self.joystick.get_axis(self.forward_axis)
        turn = -self.joystick.get_axis(self.turn_axis)
        
        # Create and publish Twist message
        msg = Twist()
        msg.linear.x = self.shape_input(forward, max_val=self.max_speed)
        msg.angular.z = self.shape_input(turn, max_val=self.max_angular_speed)
        self.twist_pub.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = JoystickController()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
