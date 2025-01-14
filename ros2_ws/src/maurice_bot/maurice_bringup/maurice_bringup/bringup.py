#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from maurice_bringup.uart import UartManager
from maurice_bringup.battery import BatteryManager

class Bringup(Node):
    def __init__(self):
        super().__init__('bringup')
        
        # Get parameters and initialize managers
        params = self._get_parameters()
        
        # Initialize managers
        self.battery_manager = BatteryManager(num_cells=params['battery']['num_cells'])
        self.uart_manager = UartManager(self, **params['uart'])

        # Setup ROS2 services and topics
        self._setup_services_and_topics()

    def _get_parameters(self):
        """Declare and get all node parameters."""
        # Declare parameters
        self.declare_parameters(
            namespace='',
            parameters=[
                ('uart.port', '/dev/ttyTHS0'),
                ('uart.baud_rate', 115200),
                ('uart.data_bits', 8),
                ('uart.parity', 'none'),
                ('uart.stop_bits', 1),
                ('uart.timeout', 0.1),
                ('uart.read_frequency', 30.0),
                ('uart.write_frequency', 30.0),
                ('battery.num_cells', 6),
                ('battery.warning_percentage', 20),
                ('battery.critical_percentage', 10),
            ]
        )

        # Return all parameters in a structured dictionary
        return {
            'uart': {
                'port': self.get_parameter('uart.port').value,
                'baud_rate': self.get_parameter('uart.baud_rate').value,
                'data_bits': self.get_parameter('uart.data_bits').value,
                'parity': self.get_parameter('uart.parity').value,
                'stop_bits': self.get_parameter('uart.stop_bits').value,
                'timeout': self.get_parameter('uart.timeout').value,
                'read_frequency': self.get_parameter('uart.read_frequency').value,
                'write_frequency': self.get_parameter('uart.write_frequency').value,
            },
            'battery': {
                'num_cells': self.get_parameter('battery.num_cells').value,
                'warning_percentage': self.get_parameter('battery.warning_percentage').value,
                'critical_percentage': self.get_parameter('battery.critical_percentage').value,
            }
        }

    def _setup_services_and_topics(self):
        """Setup all ROS2 services and topics for the node."""
        # TODO: Add service and topic initialization here
        pass

    def _handle_light_command(self, request, response):
        """Handle incoming light control requests."""
        try:
            # Convert the type enum to light_type string
            light_type = 'ring' if request.type == request.RING else 'all'
            
            # Forward the command to the UART manager
            self.uart_manager.set_light_command(
                r=request.r,
                g=request.g,
                b=request.b,
                interval=request.interval_ms,
                light_type=light_type
            )
            
            response.success = True
            response.message = "Light command executed successfully"
            
        except Exception as e:
            response.success = False
            response.message = f"Error executing light command: {str(e)}"
        
        return response

def main(args=None):
    rclpy.init(args=args)
    node = Bringup()
    rclpy.spin(node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
