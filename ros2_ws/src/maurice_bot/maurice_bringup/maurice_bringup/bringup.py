#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from maurice_bringup.i2c import I2CManager
from maurice_bringup.battery import BatteryManager
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from maurice_msgs.srv import LightCommand
from tf2_ros import TransformBroadcaster
from sensor_msgs.msg import BatteryState
from std_srvs.srv import Trigger
import time

class Bringup(Node):
    def __init__(self, debug=False):
        super().__init__('bringup')
        
        # Add debug parameter
        self.debug = debug
        if self.debug:
            self.get_logger().info('Initializing Bringup node in debug mode')
        
        # Get parameters and initialize managers
        self.params = self._get_parameters()
        
        # Initialize managers
        self.battery_manager = BatteryManager(num_cells=self.params['battery']['num_cells'])
        # Only pass the required I2C parameters: bus_number, device_address, and update_frequency
        self.i2c_manager = I2CManager(self, **self.params['i2c'], debug=self.debug)

        # Setup ROS2 services and topics
        self._setup_services_and_topics()

        self.i2c_manager.set_light_command(mode=1, r=255, g=255, b=255, interval=100)
        self.i2c_manager.set_light_command(mode=0, r=0, g=0, b=0, interval=0)
        # Request calibration at startup
        self.get_logger().info('Requesting initial calibration. Ensure robot is stationary.')
        self.i2c_manager.request_calibration()

    def _get_parameters(self):
        """Declare and get all node parameters."""
        if self.debug:
            self.get_logger().debug('Getting node parameters')
        
        # Declare parameters (only declaring the I2C parameters needed for I2CManager)
        self.declare_parameters(
            namespace='',
            parameters=[
                ('i2c.bus_number', 1),
                ('i2c.device_address', 0x42),
                ('i2c.update_frequency', 30.0),
                ('battery.num_cells', 6),
                ('battery.warning_percentage', 20),
                ('battery.critical_percentage', 10),
                ('ros_topics.odom_frequency', 30.0),
                ('ros_topics.battery_state_frequency', 0.2),
                ('motion_control.max_speed', 2.5),
                ('motion_control.max_angular_speed', 2.5),
            ]
        )

        # Build a structured dictionary with only the parameters needed by I2CManager
        params = {
            'i2c': {
                'bus_number': self.get_parameter('i2c.bus_number').value,
                'device_address': self.get_parameter('i2c.device_address').value,
                'update_frequency': self.get_parameter('i2c.update_frequency').value,
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
                'battery_state_frequency': self.get_parameter('ros_topics.battery_state_frequency').value,
            }
        }
        
        if self.debug:
            self.get_logger().debug(f'Retrieved parameters: {str(params)}')
        return params

    def _setup_services_and_topics(self):
        """Setup all ROS2 services and topics for the node."""
        if self.debug:
            self.get_logger().debug('Setting up ROS2 services and topics')
        
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

        # Add the calibrate service
        self.calibrate_srv = self.create_service(
            Trigger,
            '/calibrate',
            self._handle_calibrate_request
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

        # Create battery state publisher
        self.battery_pub = self.create_publisher(
            BatteryState,
            '/battery_state',
            10  # QoS profile depth
        )

        # Every minute, enqueue an I2C health request (will update battery_voltage)
        self.status_timer = self.create_timer(60.0, self.i2c_manager.request_health)

         # Publish battery at configured frequency (default 0.2Hz)
        battery_period = 1.0 / self.params['ros_topics']['battery_state_frequency']
        self.battery_timer = self.create_timer(battery_period, self._publish_battery)

        if self.debug:
            self.get_logger().debug('Finished setting up ROS2 services and topics')

    def _cmd_vel_callback(self, msg: Twist):
        """Handle incoming velocity commands."""
        # Apply speed limits
        limited_linear = max(min(msg.linear.x, self.params['motion_control']['max_speed']), 
                            -self.params['motion_control']['max_speed'])
        limited_angular = max(min(-msg.angular.z, self.params['motion_control']['max_angular_speed']), 
                             -self.params['motion_control']['max_angular_speed'])
        
        if self.debug:
            self.get_logger().debug(f'Limited velocities: linear={limited_linear}, angular={limited_angular}')

        self.get_logger().info(f"Limited velocities: linear={limited_linear}, angular={limited_angular}")
        
        # Forward the limited velocities to the I2C manager
        self.i2c_manager.set_speed_command(
            v=limited_linear,
            omega=limited_angular
        )

    def _handle_light_command(self, request, response):
        """
        Handle incoming light control requests.
        This version uses the new LED command interface that accepts a numeric mode.
        The mode should be:
           0: Off
           1: Solid
           2: Blink
           3: Ring
        and the remaining fields specify the LED color and the time interval.
        """
        # self.get_logger().info(
        #     f"Received light command: mode={request.mode}, r={request.r}, "
        #     f"g={request.g}, b={request.b}, interval={request.interval}"
        # )
        
        if self.debug:
            self.get_logger().debug(
                f"Received light command: mode={request.mode}, r={request.r}, "
                f"g={request.g}, b={request.b}, interval={request.interval}"
            )
        
        try:
            # Use the request.mode field directly
            self.i2c_manager.set_light_command(
                mode=request.mode,
                r=request.r,
                g=request.g,
                b=request.b,
                interval=request.interval
            )
            
            response.success = True
            response.message = "Light command executed successfully"
            
        except Exception as e:
            response.success = False
            response.message = f"Error executing light command: {str(e)}"
            self.get_logger().error(f"Error executing light command: {str(e)}")
        
        if self.debug:
            self.get_logger().debug(f"Light command response: {response.success}, {response.message}")
        
        return response

    def _handle_calibrate_request(self, request, response):
        """Handle incoming calibration requests."""
        if self.debug:
            self.get_logger().debug('Received calibrate request')
        
        try:
            self.i2c_manager.request_calibration()
            response.success = True
            response.message = "Calibration triggered successfully"
            if self.debug:
                self.get_logger().debug(f"Calibration response: {response.success}, {response.message}")
        except Exception as e:
            response.success = False
            response.message = f"Error triggering calibration: {str(e)}"
            self.get_logger().error(f"Error triggering calibration: {str(e)}")
            if self.debug:
                self.get_logger().debug(f"Calibration response: {response.success}, {response.message}")
        
        return response

    def _publish_odometry(self):
        """Publish odometry data, transform, and battery state from I2C readings."""
        transform = self.i2c_manager.current_transform
        
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

    def _publish_battery(self):
        """Publish battery state at low frequency (0.2Hz)."""
        voltage = self.i2c_manager.battery_voltage
        percentage = self.battery_manager.get_percentage(voltage)
        
        # Check battery levels and take appropriate action
        if percentage < self.params['battery']['critical_percentage'] / 100.0:
            #self.get_logger().error(f'Battery critically low ({percentage:.1%})! Shutting down...')
            #rclpy.shutdown()
            pass
        elif percentage < self.params['battery']['warning_percentage'] / 100.0:
            #self.get_logger().warn(f'Battery low ({percentage:.1%})! Please charge soon.')
            pass
        
        msg = BatteryState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.voltage = voltage
        msg.percentage = percentage
        msg.present = True
        msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
        msg.power_supply_health = (
            BatteryState.POWER_SUPPLY_HEALTH_GOOD if percentage > self.params['battery']['critical_percentage'] / 100.0
            else BatteryState.POWER_SUPPLY_HEALTH_DEAD
        )
        msg.power_supply_technology = BatteryState.POWER_SUPPLY_TECHNOLOGY_LIPO
        msg.cell_voltage = [voltage / self.params['battery']['num_cells']] * self.params['battery']['num_cells']
        
        self.battery_pub.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = Bringup(debug=False)
    rclpy.spin(node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
