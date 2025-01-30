#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
import pygame
from geometry_msgs.msg import Twist
from maurice_msgs.srv import LightCommand
import random
import matplotlib.pyplot as plt
from collections import deque
import numpy as np


class JoystickAxis:
    def __init__(self, deadzone: float, exponent: float, max_val: float, time_constant: float, dt: float, max_acceleration: float, slow_mode_factor: float):
        self.deadzone = deadzone
        self.exponent = exponent
        self.max_val = max_val
        self.time_constant = time_constant
        self.dt = dt
        self.max_acceleration = max_acceleration
        self.current_val = 0.0
        self.current_acceleration = 0.0
    def shape_input(self, x, slow_mode: bool):
        # Apply deadzone and basic scaling
        direction = 1 if x >= 0 else -1
        max_val = self.max_val * self.slow_mode_factor if slow_mode else self.max_val
        x = abs(x)
        x = x - self.deadzone
        if x < 0:
            return 0.0
        else:
            return direction * max_val * ((x/(1-self.deadzone)) ** self.exponent)
        
    def update(self, value: float, slow_mode: bool):
        shaped_value = self.shape_input(value, slow_mode)
        self.current_val = self.current_val + self.current_acceleration * self.dt
        self.current_acceleration = min(max((shaped_value - self.current_val) / self.time_constant, -self.max_acceleration), self.max_acceleration)
        
        return self.current_val

class JoystickController(Node):
    def __init__(self, debug=False):
        super().__init__('joystick_controller')
        
        # Initialize pygame and joystick
        pygame.init()
        pygame.joystick.init()
        
        # Initialize joystick
        self.joystick = None
        self.check_for_joystick()
        
        # Load parameters and initialize JoystickAxis controllers
        self._load_parameters()
        
        self.speed_axis = JoystickAxis(
            deadzone=self.joystick_control['speed_deadzone'],
            exponent=self.joystick_control['speed_exponent'],
            max_val=self.motion_control['max_speed'],
            time_constant=self.motion_control['speed_time_constant'],
            dt=self.motion_control['dt'],
            max_acceleration=self.motion_control['max_acceleration'],
            slow_mode_factor=self.joystick_control['slow_mode_factor']
        )
        self.angular_speed_axis = JoystickAxis(
            deadzone=self.joystick_control['angular_speed_deadzone'],
            exponent=self.joystick_control['angular_speed_exponent'],
            max_val=self.motion_control['max_angular_speed'],
            time_constant=self.motion_control['angular_speed_time_constant'],
            dt=self.motion_control['dt'],
            max_acceleration=self.motion_control['max_angular_acceleration'],
            slow_mode_factor=self.joystick_control['slow_mode_factor']
        )
        
        # Publisher for velocity commands
        self.twist_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        
        # Create timer for reading joystick input (use dt from parameters)
        self.create_timer(self.motion_control['dt'], self.timer_callback)
        
        # Add slow mode state
        self.slow_mode = False
        self.button_5_previous = False
        self.slow_mode_button = self.joystick_control['slow_mode_button']
        self.slow_mode_factor = self.joystick_control['slow_mode_factor']
        
        # Add light control state
        self.light_mode = 0  # 0: solid all, 1: blink all, 2: blink ring
        self.button_4_previous = False
        self.light_mode_button = self.joystick_control['light_mode_button']
        self.blink_interval_ms = self.joystick_control['light_control']['blink_interval_ms']
        
        # Create light command service client
        self.light_client = self.create_client(LightCommand, '/light_command')
        while not self.light_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('Light command service not available, waiting...')
        
        # Add debug mode
        self.debug = debug
        if self.debug:
            self._setup_debug_plotting()
        
        self.get_logger().info('Joystick Controller initialized')
    
    def check_for_joystick(self):
        if pygame.joystick.get_count() > 0:
            self.joystick = pygame.joystick.Joystick(0)
            self.joystick.init()
            self.get_logger().info('Joystick found and initialized!')
        else:
            self.get_logger().warn('No joystick found!')
    
    
    
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
            request.interval_ms = self.blink_interval_ms
        else:                       # Blinking ring
            request.type = LightCommand.Request.RING
            request.interval_ms = self.blink_interval_ms
            
        self.light_client.call_async(request)
        self.get_logger().info(f'Sent light command: mode={self.light_mode}, RGB=({r},{g},{b})')
    
    def timer_callback(self):
        if self.joystick is None:
            return
            
        pygame.event.pump()
        
        # Check button 5 state for slow mode
        button_slow = self.joystick.get_button(self.slow_mode_button)
        if button_slow and not self.button_slow_previous:
            self.slow_mode = not self.slow_mode
            self.get_logger().info('Slow mode: {}'.format(self.slow_mode))
        self.button_slow_previous = button_slow
        
        # Check button 4 state for light mode
        button_light = self.joystick.get_button(self.light_mode_button)
        if button_light and not self.button_light_previous:
            self.light_mode = (self.light_mode + 1) % 3
            self.send_light_command()
        self.button_light_previous = button_light
        
        # Read joystick values
        forward = -self.joystick.get_axis(self.joystick_control['speed_axis'])
        turn = -self.joystick.get_axis(self.joystick_control['angular_speed_axis'])
        
        # Update axis controllers and get new values
        linear_speed = self.speed_axis.update(forward, self.slow_mode)
        angular_speed = self.angular_speed_axis.update(turn, self.slow_mode)
        
        # Update debug plots if enabled
        if self.debug:
            current_time = (self.get_clock().now().nanoseconds / 1e9) - self.time_start
            self.time_data.append(current_time)
            self.speed_data.append(linear_speed)
            self.angular_speed_data.append(angular_speed)
            
            # Update plots
            self.speed_line.set_data(list(self.time_data), list(self.speed_data))
            self.angular_speed_line.set_data(list(self.time_data), list(self.angular_speed_data))
            
            # Adjust x-axis limits to show last N seconds
            for ax in [self.ax1, self.ax2]:
                ax.set_xlim(current_time - 5, current_time)
            
            self.fig.canvas.draw()
            self.fig.canvas.flush_events()
        
        # Create and publish Twist message
        msg = Twist()
        msg.linear.x = linear_speed
        msg.angular.z = angular_speed
        self.twist_pub.publish(msg)

    def _load_parameters(self):
        """Declare and load all parameters from the config file."""
        # Declare motion control parameters
        self.motion_params = {
            'max_speed': 2.0,
            'max_angular_speed': 2.5,
            'max_acceleration': 0.8,
            'max_angular_acceleration': 2.0,
            'speed_time_constant': 0.2,
            'angular_speed_time_constant': 0.2,
            'dt': 0.02
        }
        
        # Declare joystick parameters
        self.joystick_params = {
            'speed_deadzone': 0.3,
            'angular_speed_deadzone': 0.3,
            'speed_exponent': 2.0,
            'angular_speed_exponent': 2.0,
            'speed_axis': 1,
            'angular_speed_axis': 0,
            'slow_mode_button': 5,
            'light_mode_button': 4,
            'slow_mode_factor': 0.25,
            'light_control.blink_interval_ms': 300
        }
        
        # Declare all parameters with their default values
        for param, default in self.motion_params.items():
            self.declare_parameter(f'motion_control.{param}', default)
        
        for param, default in self.joystick_params.items():
            if param == 'light_control.blink_interval_ms':
                self.declare_parameter('joystick.light_control.blink_interval_ms', default)
            else:
                self.declare_parameter(f'joystick.{param}', default)
        
        # Store parameters in dictionaries
        self.motion_control = {}
        for param in self.motion_params:
            self.motion_control[param] = self.get_parameter(f'motion_control.{param}').value
        
        self.joystick_control = {}
        for param in self.joystick_params:
            if param == 'light_control.blink_interval_ms':
                if 'light_control' not in self.joystick_control:
                    self.joystick_control['light_control'] = {}
                self.joystick_control['light_control']['blink_interval_ms'] = \
                    self.get_parameter('joystick.light_control.blink_interval_ms').value
            else:
                self.joystick_control[param] = self.get_parameter(f'joystick.{param}').value

    def _setup_debug_plotting(self):
        """Initialize debug plotting setup."""
        # Initialize plotting
        plt.ion()  # Enable interactive mode
        self.fig, (self.ax1, self.ax2) = plt.subplots(2, 1, figsize=(8, 8))
        
        # Initialize data storage
        self.history_length = 200
        self.time_data = deque(maxlen=self.history_length)
        self.speed_data = deque(maxlen=self.history_length)
        self.angular_speed_data = deque(maxlen=self.history_length)
        self.time_start = self.get_clock().now().nanoseconds / 1e9
        
        # Setup plots
        self.speed_line, = self.ax1.plot([], [], 'b-', label='Linear Speed')
        self.ax1.set_title('Linear Speed')
        self.ax1.set_ylim(-self.motion_control['max_speed'], self.motion_control['max_speed'])
        self.ax1.grid(True)
        self.ax1.legend()
        
        self.angular_speed_line, = self.ax2.plot([], [], 'r-', label='Angular Speed')
        self.ax2.set_title('Angular Speed')
        self.ax2.set_ylim(-self.motion_control['max_angular_speed'], self.motion_control['max_angular_speed'])
        self.ax2.grid(True)
        self.ax2.legend()
        
        plt.tight_layout()
        plt.show()

def main(args=None):
    rclpy.init(args=args)
    node = JoystickController(debug=False)  # Enable debug mode
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
