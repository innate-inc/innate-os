#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
import serial

class UartNode(Node):
    def __init__(self):
        super().__init__('bringup_uart')
        
        # Initialize parameters and serial connection
        self.uart_params = self.get_params()
        self.setup_uart()

        # Create a timer for reading UART data (converting frequency to period)
        read_period = 1.0 / self.uart_params['read_frequency']
        self.timer = self.create_timer(read_period, self.uart_callback)

    def get_params(self):
        """Declare and get all parameters."""
        # Declare parameters
        self.declare_parameters(
            namespace='uart',
            parameters=[
                ('port', '/dev/ttyTHS0'),
                ('baud_rate', 115200),
                ('data_bits', 8),
                ('parity', 'none'),
                ('stop_bits', 1),
                ('timeout', 0.1),
                ('read_frequency', 30.0),
                ('write_frequency', 30.0)
            ]
        )

        # Get and return parameters as dictionary
        return {
            'port': self.get_parameter('uart.port').value,
            'baud_rate': self.get_parameter('uart.baud_rate').value,
            'data_bits': self.get_parameter('uart.data_bits').value,
            'parity': self.get_parameter('uart.parity').value,
            'stop_bits': self.get_parameter('uart.stop_bits').value,
            'timeout': self.get_parameter('uart.timeout').value,
            'read_frequency': self.get_parameter('uart.read_frequency').value,
            'write_frequency': self.get_parameter('uart.write_frequency').value
        }

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

    def uart_callback(self):
        if self.ser.in_waiting:
            try:
                data = self.ser.readline().decode('utf-8').strip()
                self.get_logger().info(f'Received: {data}')
                # Add your data processing logic here
            except Exception as e:
                self.get_logger().error(f'Error reading from serial port: {str(e)}')

    def __del__(self):
        if hasattr(self, 'ser'):
            self.ser.close()

def main(args=None):
    rclpy.init(args=args)
    uart_node = UartNode()
    
    try:
        rclpy.spin(uart_node)
    except KeyboardInterrupt:
        pass
    finally:
        uart_node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
