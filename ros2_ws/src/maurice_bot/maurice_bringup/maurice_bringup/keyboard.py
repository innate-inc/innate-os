#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
import pygame
from geometry_msgs.msg import Twist

class KeyboardController(Node):
    def __init__(self):
        super().__init__('keyboard_controller')
        
        # Initialize speed and turn rate
        self.current_speed = 0.0
        self.current_turn = 0.0
        self.speed_increment = 0.02
        self.turn_increment = 0.1
        self.max_speed = 0.5      # Max 0.5 m/s
        self.max_turn = 2.5       # Max 2.5 rad/s
        
        # Initialize pygame for keyboard input
        pygame.init()
        pygame.display.set_mode((100, 100))  # Small window to capture keyboard events
        
        # Publisher for velocity commands
        self.twist_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        
        # Create timer for reading keyboard input
        self.create_timer(0.1, self.timer_callback)
        
        self.get_logger().info('Keyboard Controller initialized')
    
    def timer_callback(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                return
        
        # Get pressed keys
        keys = pygame.key.get_pressed()
        
        # Update speed and turn rate based on key presses
        if keys[pygame.K_w]:  # W - increase speed
            self.current_speed = min(self.current_speed + self.speed_increment, self.max_speed)
        if keys[pygame.K_x]:  # X - decrease speed
            self.current_speed = max(self.current_speed - self.speed_increment, -self.max_speed)
        if keys[pygame.K_a]:  # A - turn left
            self.current_turn = min(self.current_turn + self.turn_increment, self.max_turn)
        if keys[pygame.K_d]:  # D - turn right
            self.current_turn = max(self.current_turn - self.turn_increment, -self.max_turn)
        if keys[pygame.K_s]:  # S - stop everything
            self.current_speed = 0.0
            self.current_turn = 0.0
        
        # Create and publish Twist message
        msg = Twist()
        msg.linear.x = self.current_speed
        msg.angular.z = self.current_turn
        self.twist_pub.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = KeyboardController()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
