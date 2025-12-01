#!/usr/bin/env python3
"""
Tank Drive Controller for Nintendo Switch Pro Controller

TRUE TANK DRIVE:
  - Left stick Y  → Left motor speed
  - Right stick Y → Right motor speed

The MCU expects (v, omega) and does differential mixing:
  left_motor  = v - omega * k
  right_motor = v + omega * k

We invert this: given desired (left_motor, right_motor), compute (v, omega):
  v     = (left + right) / 2
  omega = (right - left) / 2

Note: bringup.py negates angular.z before sending to MCU, so we account for that.
"""

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
import struct
import os
import select
import glob


class TankDriveController(Node):
    # Joystick event types
    JS_EVENT_BUTTON = 0x01
    JS_EVENT_AXIS = 0x02
    JS_EVENT_INIT = 0x80
    JS_EVENT_FORMAT = "IhBB"  # time, value, type, number
    JS_EVENT_SIZE = struct.calcsize(JS_EVENT_FORMAT)
    
    def __init__(self):
        super().__init__('tank_drive_controller')
        
        # Declare parameters
        self.declare_parameter('max_motor_speed', 0.6)  # Use SAME scale for both motors
        self.declare_parameter('deadzone', 0.15)
        self.declare_parameter('update_rate', 50.0)  # Hz
        self.declare_parameter('left_axis', 1)   # Left stick Y
        self.declare_parameter('right_axis', 3)  # Right stick Y
        self.declare_parameter('invert_left', True)   # Invert so push-forward = positive
        self.declare_parameter('invert_right', True)
        self.declare_parameter('joystick_device', '/dev/input/js0')
        
        # Get parameters
        self.max_motor_speed = self.get_parameter('max_motor_speed').value
        self.deadzone = self.get_parameter('deadzone').value
        self.update_rate = self.get_parameter('update_rate').value
        self.left_axis = self.get_parameter('left_axis').value
        self.right_axis = self.get_parameter('right_axis').value
        self.invert_left = self.get_parameter('invert_left').value
        self.invert_right = self.get_parameter('invert_right').value
        self.joystick_device = self.get_parameter('joystick_device').value
        
        # Joystick state
        self.js_fd = None
        self.axis_values = {}  # axis_number -> normalized value (-1 to 1)
        
        # Try to open joystick
        self._init_joystick()
        
        # Publisher for velocity commands
        self.cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        
        # Timer for reading joystick input and publishing
        self.timer = self.create_timer(1.0 / self.update_rate, self.timer_callback)
        
        self.get_logger().info(f'Tank Drive Controller initialized')
        self.get_logger().info(f'  Device: {self.joystick_device}')
        self.get_logger().info(f'  Left axis: {self.left_axis}, Right axis: {self.right_axis}')
        self.get_logger().info(f'  Max motor speed: {self.max_motor_speed}')
        self.get_logger().info(f'  Deadzone: {self.deadzone}')
        self.get_logger().info(f'Controls:')
        self.get_logger().info(f'  Both sticks forward  = drive forward')
        self.get_logger().info(f'  Both sticks backward = drive backward')
        self.get_logger().info(f'  Left only forward    = swing turn RIGHT')
        self.get_logger().info(f'  Right only forward   = swing turn LEFT')
        self.get_logger().info(f'  Left fwd + Right back = spin RIGHT')
        self.get_logger().info(f'  Right fwd + Left back = spin LEFT')
    
    def _init_joystick(self):
        """Initialize the joystick device."""
        # Try the specified device first, then scan for others
        devices_to_try = [self.joystick_device]
        devices_to_try.extend(sorted(glob.glob('/dev/input/js*')))
        
        for device in devices_to_try:
            if os.path.exists(device):
                try:
                    self.js_fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
                    self.get_logger().info(f'Joystick connected: {device}')
                    self.joystick_device = device
                    return
                except OSError as e:
                    self.get_logger().warn(f'Failed to open {device}: {e}')
        
        self.get_logger().error('No joystick device found!')
        self.get_logger().error('Connect controller via USB or Bluetooth')
        self.get_logger().error('If permission denied, run: sudo chmod 666 /dev/input/js0')
    
    def _read_js_events(self):
        """Read all pending joystick events."""
        if self.js_fd is None:
            return
        
        try:
            ready, _, _ = select.select([self.js_fd], [], [], 0)
            if not ready:
                return
            
            while True:
                try:
                    event_data = os.read(self.js_fd, self.JS_EVENT_SIZE)
                    if len(event_data) < self.JS_EVENT_SIZE:
                        break
                    
                    time, value, event_type, number = struct.unpack(
                        self.JS_EVENT_FORMAT, event_data
                    )
                    
                    if event_type & self.JS_EVENT_AXIS:
                        # Normalize to -1.0 to 1.0
                        normalized = value / 32767.0
                        self.axis_values[number] = normalized
                        
                except BlockingIOError:
                    break
                    
        except OSError as e:
            self.get_logger().warn(f'Joystick disconnected: {e}')
            self.js_fd = None
    
    def apply_deadzone(self, value: float) -> float:
        """Apply deadzone to joystick input."""
        if abs(value) < self.deadzone:
            return 0.0
        sign = 1.0 if value > 0 else -1.0
        return sign * (abs(value) - self.deadzone) / (1.0 - self.deadzone)
    
    def timer_callback(self):
        """Read joystick and publish velocity commands."""
        if self.js_fd is None:
            self._init_joystick()
            if self.js_fd is None:
                return
        
        self._read_js_events()
        
        # Get axis values (default to 0)
        left_raw = self.axis_values.get(self.left_axis, 0.0)
        right_raw = self.axis_values.get(self.right_axis, 0.0)
        
        # Apply inversion (joysticks typically have Y inverted: push up = negative)
        if self.invert_left:
            left_raw = -left_raw
        if self.invert_right:
            right_raw = -right_raw
        
        # Apply deadzone
        left = self.apply_deadzone(left_raw)
        right = self.apply_deadzone(right_raw)
        
        # Scale to motor speed (-1 to 1 → -max to +max)
        left_motor = left * self.max_motor_speed
        right_motor = right * self.max_motor_speed
        
        # Convert desired motor speeds to (v, omega) for differential drive
        # MCU does: left = v - omega*k, right = v + omega*k
        # Solving: v = (left + right) / 2
        #          omega = (right - left) / 2
        # BUT bringup.py negates angular.z, so we send -omega as angular.z
        
        v = (left_motor + right_motor) / 2.0
        omega = (right_motor - left_motor) / 2.0
        
        # Send to /cmd_vel
        # angular.z gets negated by bringup, so send -omega
        twist = Twist()
        twist.linear.x = v
        twist.linear.y = 0.0
        twist.linear.z = 0.0
        twist.angular.x = 0.0
        twist.angular.y = 0.0
        twist.angular.z = -omega  # Negated because bringup negates it again
        
        self.cmd_vel_pub.publish(twist)
        
        # Debug logging
        if abs(left) > 0.01 or abs(right) > 0.01:
            self.get_logger().info(
                f'L:{left:.2f} R:{right:.2f} | motors: L={left_motor:.2f} R={right_motor:.2f} | cmd: v={v:.2f} ω={-omega:.2f}'
            )
    
    def destroy_node(self):
        if self.js_fd is not None:
            try:
                os.close(self.js_fd)
            except OSError:
                pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = TankDriveController()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
