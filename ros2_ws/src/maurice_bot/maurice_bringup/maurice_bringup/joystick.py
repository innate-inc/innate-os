#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
import pygame
from geometry_msgs.msg import Twist
from maurice_msgs.srv import LightCommand
import random

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
        
        # Add slow mode state
        self.slow_mode = False
        self.button_5_previous = False
        
        # Add light control state
        self.light_mode = 0  # 0: solid all, 1: blink all, 2: blink ring
        self.button_4_previous = False
        
        # Create light command service client
        self.light_client = self.create_client(LightCommand, 'light-command')
        while not self.light_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('Light command service not available, waiting...')
        
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
    
    def send_light_command(self):
        # Generate random RGB values
        r = random.randint(0, 255)
        g = random.randint(0, 255)
        b = random.randint(0, 255)
        
        request = LightCommand.Request()
        request.r = r
        request.g = g
        request.b = b
        
        # Set parameters based on current mode
        if self.light_mode == 0:    # Solid all
            request.type = LightCommand.Request.ALL
            request.interval_ms = 0
        elif self.light_mode == 1:  # Blinking all
            request.type = LightCommand.Request.ALL
            request.interval_ms = 300
        else:                       # Blinking ring
            request.type = LightCommand.Request.RING
            request.interval_ms = 300
            
        self.light_client.call_async(request)
        self.get_logger().info(f'Sent light command: mode={self.light_mode}, RGB=({r},{g},{b})')
    
    def timer_callback(self):
        if self.joystick is None:
            return
            
        pygame.event.pump()
        
        # Check button 5 state
        button_5 = self.joystick.get_button(5)
        if button_5 and not self.button_5_previous:
            self.slow_mode = not self.slow_mode
            self.get_logger().info('Slow mode: {}'.format(self.slow_mode))
        self.button_5_previous = button_5
        
        # Check button 4 state
        button_4 = self.joystick.get_button(4)
        if button_4 and not self.button_4_previous:
            self.light_mode = (self.light_mode + 1) % 3
            self.send_light_command()
        self.button_4_previous = button_4
        
        # Read and scale joystick values
        forward = -self.joystick.get_axis(self.forward_axis)
        turn = -self.joystick.get_axis(self.turn_axis)
        
        # Create and publish Twist message
        msg = Twist()
        speed_multiplier = 0.25 if self.slow_mode else 1.0
        msg.linear.x = self.shape_input(forward, max_val=self.max_speed * speed_multiplier)
        msg.angular.z = self.shape_input(turn, max_val=self.max_angular_speed * speed_multiplier)
        self.twist_pub.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = JoystickController()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
