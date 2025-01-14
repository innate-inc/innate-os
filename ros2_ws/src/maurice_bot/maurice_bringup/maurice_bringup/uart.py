#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
import serial
from geometry_msgs.msg import TransformStamped
from tf2_ros import TransformBroadcaster
import math

class UartManager(Node):
    def __init__(self, node, 
                 port='/dev/ttyTHS0',
                 read_frequency=30.0,
                 write_frequency=30.0,
                 baud_rate=115200,
                 data_bits=8,
                 parity='none',
                 stop_bits=1,
                 timeout=0.1):
        self.node = node
        
        # Add new instance variables
        self.battery_voltage = 0.0
        self.tf_broadcaster = TransformBroadcaster(node)
        
        # Add new instance variables for speed commands
        self.cmd_linear_velocity = 0.0
        self.cmd_angular_velocity = 0.0
        
        # Store parameters directly
        self.uart_params = {
            'port': port,
            'read_frequency': read_frequency,
            'write_frequency': write_frequency,
            'baud_rate': baud_rate,
            'data_bits': data_bits,
            'parity': parity,
            'stop_bits': stop_bits,
            'timeout': timeout
        }
        
        self.setup_uart()
        
        # Create separate timers for reading and writing
        read_period = 1.0 / self.uart_params['read_frequency']
        write_period = 1.0 / self.uart_params['write_frequency']
        self.read_timer = self.node.create_timer(read_period, self.read_callback)
        self.write_timer = self.node.create_timer(write_period, self.write_callback)

    def setup_uart(self):
        """Initialize the serial connection with the configured parameters."""
        try:
            self.ser = serial.Serial(
                port=self.uart_params['port'],
                baudrate=self.uart_params['baud_rate'],
                bytesize=self.uart_params['data_bits'],
                parity=self.uart_params['parity'][0].upper(),
                stopbits=self.uart_params['stop_bits'],
                timeout=self.uart_params['timeout']
            )
            self.get_logger().info(
                f"Connected to {self.uart_params['port']} at "
                f"{self.uart_params['baud_rate']} baud"
            )
        except serial.SerialException as e:
            self.get_logger().error(f'Failed to open serial port: {str(e)}')
            raise

    def read_callback(self):
        """Callback for reading data from UART"""
        if self.ser.in_waiting:
            try:
                data = self.ser.readline().decode('utf-8').strip()
                # Parse the comma-separated values
                values = data.split(',')
                if len(values) >= 4:
                    x_pos, y_pos, heading_deg, battery_voltage = map(float, values[:4])
                    
                    # Store battery voltage
                    self.battery_voltage = battery_voltage
                    
                    # Create transform message
                    t = TransformStamped()
                    t.header.stamp = self.node.get_clock().now().to_msg()
                    t.header.frame_id = 'odom'
                    t.child_frame_id = 'base_link'
                    
                    # Set translation
                    t.transform.translation.x = x_pos
                    t.transform.translation.y = y_pos
                    t.transform.translation.z = 0.0
                    
                    # Convert heading to quaternion
                    heading_rad = math.radians(heading_deg)
                    t.transform.rotation.x = 0.0
                    t.transform.rotation.y = 0.0
                    t.transform.rotation.z = math.sin(heading_rad / 2.0)
                    t.transform.rotation.w = math.cos(heading_rad / 2.0)
                    
                    # Broadcast the transform
                    self.tf_broadcaster.sendTransform(t)
                    
                    self.get_logger().debug(f'Processed pose: x={x_pos}, y={y_pos}, heading={heading_deg}°, battery={battery_voltage}V')
                else:
                    self.get_logger().warning('Received incomplete data from serial port')
                    
            except Exception as e:
                self.get_logger().error(f'Error reading from serial port: {str(e)}')

    def write_callback(self):
        """Callback for writing data to UART"""
        try:
            command = f"S,{self.cmd_linear_velocity:.3f},{self.cmd_angular_velocity:.3f}\n"
            self.ser.write(command.encode('utf-8'))
        except Exception as e:
            self.get_logger().error(f'Error writing to serial port: {str(e)}')

    def set_speed_command(self, v, omega):
        """Set the desired linear and angular velocities.
        
        Args:
            v (float): Linear velocity in m/s
            omega (float): Angular velocity in rad/s
        """
        self.cmd_linear_velocity = v
        self.cmd_angular_velocity = omega
        self.get_logger().debug(f'Set speed command: v={v} m/s, omega={omega} rad/s')

    def set_light_command(
        self,
        r: int,
        g: int,
        b: int,
        interval: int = 0,
        light_type: str = 'all'
    ) -> None:
        """Set the LED light colors and behavior.
        
        Args:
            r (int): Red component (0-255)
            g (int): Green component (0-255)
            b (int): Blue component (0-255)
            interval (int): Effect interval in milliseconds
            light_type (str): 'ring' (rotating) or 'all' (all LEDs)
        """
        try:
            # Validate and convert inputs to integers
            r = int(max(0, min(255, int(r))))
            g = int(max(0, min(255, int(g))))
            b = int(max(0, min(255, int(b))))
            interval = int(max(0, int(interval)))
            
            # Determine command type based on light_type
            cmd_type = 'R' if light_type.lower() == 'ring' else 'C'
            
            command = f"{cmd_type},{r:d},{g:d},{b:d},{interval:d}\n"
            self.ser.write(command.encode('utf-8'))
            self.get_logger().debug(
                f'Set light command: type={cmd_type}, RGB=({r},{g},{b}), '
                f'interval={interval}ms'
            )
        except Exception as e:
            self.get_logger().error(f'Error sending light command: {str(e)}')

    def __del__(self):
        if hasattr(self, 'ser'):
            self.ser.close()

