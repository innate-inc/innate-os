#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc


import rclpy
from geometry_msgs.msg import Twist
from pynput import keyboard
from rclpy.node import Node
from rclpy.parameter import Parameter

from mars_control.teleop_publish_gate import TeleopPublishGate


class KeyboardController(Node):
    def __init__(self):
        super().__init__("keyboard_controller")

        # No literal defaults: values come from mars_control/config/motion_control.yaml.
        self.declare_parameter("motion_control.max_speed", Parameter.Type.DOUBLE)
        self.declare_parameter("motion_control.max_angular_speed", Parameter.Type.DOUBLE)

        # Initialize speed and turn rate
        self.current_speed = 0.0
        self.current_turn = 0.0
        self.speed_increment = 0.02
        self.turn_increment = 0.1
        self.max_speed = self.get_parameter("motion_control.max_speed").value
        self.max_turn = self.get_parameter("motion_control.max_angular_speed").value

        # Route human commands through the priority mux. Publishing directly
        # to /cmd_vel would compete with Nav2 and bypass autonomous-skill
        # takeover monitoring.
        self.twist_pub = self.create_publisher(Twist, "/cmd_vel_teleop", 10)
        self._publish_gate = TeleopPublishGate()

        # Create timer for publishing commands
        self.create_timer(0.1, self.timer_callback)

        # Set up keyboard listener
        self.listener = keyboard.Listener(on_press=self.on_press)
        self.listener.start()

        self.get_logger().info("Keyboard Controller initialized")
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
            if key_char == "w":
                self.current_speed = min(self.current_speed + self.speed_increment, self.max_speed)
            elif key_char == "x":
                self.current_speed = max(self.current_speed - self.speed_increment, -self.max_speed)
            elif key_char == "a":
                self.current_turn = min(self.current_turn + self.turn_increment, self.max_turn)
            elif key_char == "d":
                self.current_turn = max(self.current_turn - self.turn_increment, -self.max_turn)
            elif key_char == "s":
                self.current_speed = 0.0
                self.current_turn = 0.0
            elif key_char == "q":
                self.destroy_node()
                rclpy.shutdown()
                return False
        except AttributeError:
            pass

        return True

    def timer_callback(self):
        # Teleop is the mux's top-priority input. After an explicit stop,
        # publish one zero and go silent so Nav2 can regain ownership.
        command = self._publish_gate.next_command(self.current_speed, self.current_turn)
        if command is None:
            return
        msg = Twist()
        msg.linear.x, msg.angular.z = command
        self.twist_pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = KeyboardController()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
