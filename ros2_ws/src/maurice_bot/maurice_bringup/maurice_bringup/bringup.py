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
from std_srvs.srv import Trigger, SetBool
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
        
        # Track HSSW reset state for torque re-enable
        self.hssw_reset_pending = False
        self.last_reset_status = None
        self.last_reset_target = None
        self.hssw_reset_completion_time = None
        self.torque_enable_attempts = 0
        self.max_torque_enable_attempts = 3

        # Setup ROS2 services and topics
        self._setup_services_and_topics()

        self.i2c_manager.set_light_command(mode=1, r=255, g=255, b=255, interval=100)
        time.sleep(5)
        self.i2c_manager.set_light_command(mode=0, r=0, g=0, b=0, interval=0)
        # Request calibration at startup
        time.sleep(10)
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

        # Add the reset services
        self.reset_hssw_srv = self.create_service(
            Trigger,
            '/reset_hssw',
            lambda req, resp: self._handle_reset_request(req, resp, self.i2c_manager.RESET_HSSW)
        )
        self.reset_efuse_srv = self.create_service(
            Trigger,
            '/reset_efuse',
            lambda req, resp: self._handle_reset_request(req, resp, self.i2c_manager.RESET_EFUSE)
        )
        self.reset_both_srv = self.create_service(
            Trigger,
            '/reset_both',
            lambda req, resp: self._handle_reset_request(req, resp, self.i2c_manager.RESET_BOTH)
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

        # Create clients for arm and head services (for HSSW reset recovery)
        self.arm_torque_client = self.create_client(Trigger, '/mars/arm/torque_on')
        self.head_enable_client = self.create_client(SetBool, '/mars/head/enable_servo')
        
        # Timer to monitor HSSW reset completion and re-enable torque
        self.reset_monitor_timer = self.create_timer(0.1, self._monitor_hssw_reset_completion)

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

    def _handle_reset_request(self, request, response, target):
        """Handle incoming reset requests."""
        response.success = False
        response.message = "Unknown error"
        
        try:
            target_str = {self.i2c_manager.RESET_HSSW: "HSSW", 
                         self.i2c_manager.RESET_EFUSE: "EFUSE", 
                         self.i2c_manager.RESET_BOTH: "Both"}.get(target, "Unknown")
            
            # Reset state tracking for new reset (especially important for HSSW)
            if target == self.i2c_manager.RESET_HSSW:
                # Clear any previous reset state
                self.hssw_reset_pending = False
                self.hssw_reset_completion_time = None
                self.last_reset_status = None
                self.last_reset_target = None
            
            # Request the reset
            self.i2c_manager.request_reset(target)
            
            # Track HSSW-only resets for torque re-enable
            if target == self.i2c_manager.RESET_HSSW:
                self.hssw_reset_pending = True
                self.get_logger().info("HSSW reset initiated - will re-enable arm torque on completion")
            
            response.success = True
            response.message = f"Reset triggered successfully for {target_str}"
                
        except Exception as e:
            response.success = False
            response.message = f"Error triggering reset: {str(e)}"
            self.get_logger().error(f"Error triggering reset: {str(e)}")
        finally:
            return response

    def _monitor_hssw_reset_completion(self):
        """Monitor for HSSW reset completion and re-enable arm torque."""
        current_status = self.i2c_manager.reset_status
        current_target = self.i2c_manager.reset_target_response
        
        # Detect completion: status == 0x02 (Completed) and target == RESET_HSSW
        if (current_status == 0x02 and 
            current_target == self.i2c_manager.RESET_HSSW and
            (self.last_reset_status != 0x02 or self.last_reset_target != self.i2c_manager.RESET_HSSW)):
            
            # First time we see completion - record the time
            if self.hssw_reset_completion_time is None:
                self.hssw_reset_completion_time = self.get_clock().now()
                self.get_logger().info("HSSW reset completed - waiting for arm to power up")
                self.hssw_reset_pending = True
        
        # If we detected completion and enough time has passed, enable torque
        if (self.hssw_reset_pending and 
            self.hssw_reset_completion_time is not None and
            current_status == 0x02 and
            current_target == self.i2c_manager.RESET_HSSW):
            
            # Wait 0.5 seconds after completion for arm to power up
            elapsed = (self.get_clock().now() - self.hssw_reset_completion_time).nanoseconds / 1e9
            if elapsed >= 0.5:
                # Only try once per timer callback to avoid spam
                if self.torque_enable_attempts < self.max_torque_enable_attempts:
                    self.torque_enable_attempts += 1
                    self.get_logger().info(f"Re-enabling arm and head (attempt {self.torque_enable_attempts})")
                    
                    # Call arm torque_on service with timeout
                    if self.arm_torque_client.wait_for_service(timeout_sec=1.0):
                        arm_request = Trigger.Request()
                        try:
                            arm_future = self.arm_torque_client.call_async(arm_request)
                            # Add callback to verify success
                            arm_future.add_done_callback(self._torque_enable_callback)
                        except Exception as e:
                            self.get_logger().error(f"Failed to call arm torque service: {e}")
                            # Will retry on next timer cycle if attempts < max
                    else:
                        self.get_logger().warn(f"Arm torque service unavailable (attempt {self.torque_enable_attempts})")
                        # Will retry on next timer cycle if attempts < max
                    
                    # Call head enable service with timeout
                    if self.head_enable_client.wait_for_service(timeout_sec=1.0):
                        head_request = SetBool.Request()
                        head_request.data = True
                        try:
                            head_future = self.head_enable_client.call_async(head_request)
                            # Add callback to verify success
                            head_future.add_done_callback(self._head_enable_callback)
                        except Exception as e:
                            self.get_logger().error(f"Failed to call head enable service: {e}")
                    else:
                        self.get_logger().warn(f"Head enable service unavailable (attempt {self.torque_enable_attempts})")
                else:
                    # Max attempts reached - give up and clear state
                    if self.torque_enable_attempts == self.max_torque_enable_attempts:
                        self.get_logger().error("Max torque enable attempts reached - manual re-enable required")
                        self.torque_enable_attempts += 1  # Increment to avoid repeated logging
                    self.hssw_reset_pending = False
                    self.hssw_reset_completion_time = None
                    self.torque_enable_attempts = 0
        
        # Update last known state
        self.last_reset_status = current_status
        self.last_reset_target = current_target

    def _torque_enable_callback(self, future):
        """Callback to verify arm torque enable succeeded."""
        try:
            response = future.result()
            if response.success:
                self.get_logger().info(f"Arm torque re-enabled: {response.message}")
                # Clear state on success
                self.hssw_reset_pending = False
                self.hssw_reset_completion_time = None
                self.torque_enable_attempts = 0
            else:
                self.get_logger().warn(f"Arm torque enable failed: {response.message}")
                # Will retry on next timer cycle if attempts < max
        except Exception as e:
            self.get_logger().error(f"Arm torque enable callback error: {e}")
            # Will retry on next timer cycle if attempts < max

    def _head_enable_callback(self, future):
        """Callback to verify head servo enable succeeded."""
        try:
            response = future.result()
            if response.success:
                self.get_logger().info(f"Head servo re-enabled: {response.message}")
            else:
                self.get_logger().warn(f"Head enable failed: {response.message}")
        except Exception as e:
            self.get_logger().error(f"Head enable callback error: {e}")

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

        # Publish battery state
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
