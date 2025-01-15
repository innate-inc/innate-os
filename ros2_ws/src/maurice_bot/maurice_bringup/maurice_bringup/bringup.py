#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from maurice_bringup.uart import UartManager
from maurice_bringup.battery import BatteryManager
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from maurice_msgs.srv import LightCommand
from tf2_ros import TransformBroadcaster

class Bringup(Node):
    def __init__(self):
        super().__init__('bringup')
        
        # Get parameters and initialize managers
        self.params = self._get_parameters()
        
        # Initialize managers
        self.battery_manager = BatteryManager(num_cells=self.params['battery']['num_cells'])
        self.uart_manager = UartManager(self, **self.params['uart'])

        # Setup ROS2 services and topics
        self._setup_services_and_topics()

    def _get_parameters(self):
        """Declare and get all node parameters."""
        # Declare parameters
        self.declare_parameters(
            namespace='',
            parameters=[
                ('uart.port', '/dev/ttyTHS1'),
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
                ('ros_topics.odom_frequency', 10.0),
                ('motion_control.max_speed', 2.5),
                ('motion_control.max_angular_speed', 2.5),
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
            },
            'motion_control': {
                'max_speed': self.get_parameter('motion_control.max_speed').value,
                'max_angular_speed': self.get_parameter('motion_control.max_angular_speed').value,
            },
            'ros_topics': {
                'odom_frequency': self.get_parameter('ros_topics.odom_frequency').value,
            }
        }

    def _setup_services_and_topics(self):
        """Setup all ROS2 services and topics for the node."""
        # Create subscription to cmd_vel topic
        self.cmd_vel_sub = self.create_subscription(
            Twist,
            '/cmd_vel',
            self._cmd_vel_callback,
            10  # QoS profile depth
        )
        
        # Add the light command service
        self.light_srv = self.create_service(
            LightCommand,  # Service type
            '/light_command',  # Service name
            self._handle_light_command  # Callback function
        )

        # Create odometry publisher
        self.odom_pub = self.create_publisher(
            Odometry,
            '/odom',
            10  # QoS profile depth
        )
        
        # Initialize the transform broadcaster
        self.tf_broadcaster = TransformBroadcaster(self)

        # Create timer for odometry and transform updates
        odom_period = 1.0 / self.params['ros_topics']['odom_frequency']
        self.odom_timer = self.create_timer(odom_period, self._publish_odometry)

    def _cmd_vel_callback(self, msg: Twist):
        """Handle incoming velocity commands."""
        # Apply speed limits
        limited_linear = max(min(msg.linear.x, self.params['motion_control']['max_speed']), 
                            -self.params['motion_control']['max_speed'])
        limited_angular = max(min(-msg.angular.z, self.params['motion_control']['max_angular_speed']), 
                             -self.params['motion_control']['max_angular_speed'])
        
        # Forward the limited velocities to the UART manager
        self.uart_manager.set_speed_command(
            v=limited_linear,
            omega=limited_angular
        )

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

    def _publish_odometry(self):
        """Publish odometry data and transform from UART readings."""
        transform = self.uart_manager.current_transform
        
        # Broadcast the transform
        self.tf_broadcaster.sendTransform(transform)
        
        # Create and publish odometry message
        odom = Odometry()
        odom.header = transform.header
        odom.child_frame_id = transform.child_frame_id
        
        # Copy pose from transform
        odom.pose.pose.position.x = transform.transform.translation.x
        odom.pose.pose.position.y = transform.transform.translation.y
        odom.pose.pose.position.z = transform.transform.translation.z
        odom.pose.pose.orientation = transform.transform.rotation
        
        # Publish the odometry message
        self.odom_pub.publish(odom)

def main(args=None):
    rclpy.init(args=args)
    node = Bringup()
    rclpy.spin(node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
