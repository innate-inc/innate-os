#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from pynput import keyboard
import threading

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
        
        # Publisher for velocity commands
        self.twist_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        
        # Create timer for publishing commands
        self.create_timer(0.1, self.timer_callback)
        
        # Set up keyboard listener
        self.listener = keyboard.Listener(on_press=self.on_press)
        self.listener.start()
        
        self.get_logger().info('Keyboard Controller initialized')
        self.get_logger().info("""
Control Instructions:
    w: increase forward speed
    x: increase backward speed
    a: turn left
    d: turn right
    s: stop
    q: quit
        """)
    
    def on_press(self, key):
        try:
            key_char = key.char.lower()
            
            # Update speed and turn rate based on key presses
            if key_char == 'w':
                self.current_speed = min(self.current_speed + self.speed_increment, self.max_speed)
            elif key_char == 'x':
                self.current_speed = max(self.current_speed - self.speed_increment, -self.max_speed)
            elif key_char == 'a':
                self.current_turn = min(self.current_turn + self.turn_increment, self.max_turn)
            elif key_char == 'd':
                self.current_turn = max(self.current_turn - self.turn_increment, -self.max_turn)
            elif key_char == 's':
                self.current_speed = 0.0
                self.current_turn = 0.0
            elif key_char == 'q':
                self.destroy_node()
                rclpy.shutdown()
                return False
        except AttributeError:
            pass
        
        return True
    
    def timer_callback(self):
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
