#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
# from rcl_interfaces.srv import GetParameters  # Not needed when bypassing servo_manager
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from std_srvs.srv import Trigger
import math
import time
import json

# Import our Dynamixel and Robot classes
from maurice_arm.dynamixel import Dynamixel, OperatingMode
from maurice_arm.robot import Robot

class MauriceArmNode(Node):
    def __init__(self):
        super().__init__('maurice_arm')

        # Get parameters from the new YAML structure
        # Note: parameters are loaded under the node's namespace (e.g. "maurice_arm")
        self.declare_parameter('baud_rate', 115200)
        self.declare_parameter('control_frequency', 100)
        # Declare 'joints' as a string parameter with empty JSON object as default
        self.declare_parameter('joints', '{}')

        baud_rate = self.get_parameter('baud_rate').value
        control_frequency = self.get_parameter('control_frequency').value
        # Get the joints parameter as a string and parse it as JSON
        joints_str = self.get_parameter('joints').value
        joints_param = json.loads(joints_str)
        
        # Store joints config for later use in command callback
        self.joints_config = joints_param

        # Bypass servo_manager and directly use UART device
        device_name = "/dev/ttyTHS1"  # UART device for arm
        self.get_logger().info(f"Using arm device: {device_name}")

        # Create a list to hold servo IDs (extracted from joint parameters)
        servo_ids = []

        # Initialize Dynamixel interface
        dynamixel = Dynamixel.Config(
            baudrate=baud_rate,
            device_name=device_name
        ).instantiate()

        self.get_logger().info("Configuring servos with provided joint parameters...")

        # For each joint, perform the configuration sequence:
        # 1. Torque off, 2. set position limits, 3. set PWM limit,
        # 4. set current limit, then 5. set operating mode, then 6. Torque on.
        for joint_name, joint in joints_param.items():
            servo_id = joint.get("servo_id")
            servo_ids.append(servo_id)

            # Disable torque for this servo
            self.get_logger().info(f"Disabling torque for {joint_name} (Servo ID {servo_id})")
            dynamixel._disable_torque(servo_id)
            time.sleep(0.5)

            # Convert position limits from radians to encoder counts.
            # Using: encoder_value = int( (radian / (2*pi)) * 4096 + 2048 )
            pos_limits = joint.get("position_limits", {})
            min_rad = pos_limits.get("min", 0.0)
            max_rad = pos_limits.get("max", 0.0)
            min_encoder = int((min_rad / (2 * math.pi)) * 4096 + 2048)
            max_encoder = int((max_rad / (2 * math.pi)) * 4096 + 2048)
            self.get_logger().info(
                f"Setting {joint_name} position limits: {min_encoder} (min) and {max_encoder} (max)"
            )
            dynamixel.set_min_position_limit(servo_id, min_encoder)
            dynamixel.set_max_position_limit(servo_id, max_encoder)
            time.sleep(0.5)

            # Set the PWM limit
            pwm_limit = joint.get("pwm_limits", 885)
            self.get_logger().info(f"Setting {joint_name} PWM limit: {pwm_limit}")
            dynamixel.set_pwm_limit(servo_id, pwm_limit)
            time.sleep(0.5)

            # Set current limit if provided
            if "current_limit" in joint:
                current_limit = joint["current_limit"]
                self.get_logger().info(f"Setting {joint_name} current limit: {current_limit}")
                dynamixel.set_current_limit(servo_id, current_limit)
                time.sleep(0.5)

            # Set the operating mode.
            control_mode_param = joint.get("control_mode")
            control_mode_mapping = {
                1: OperatingMode.VELOCITY,
                2: OperatingMode.POSITION,
                4: OperatingMode.CURRENT_CONTROLLED_POSITION,
                5: OperatingMode.PWM
            }
            if control_mode_param not in control_mode_mapping:
                error_msg = (f"Unsupported control mode {control_mode_param} for {joint_name} (Servo ID {servo_id}). "
                           f"Supported modes are {list(control_mode_mapping.keys())}.")
                self.get_logger().error(error_msg)
                raise ValueError(error_msg)
            op_mode = control_mode_mapping[control_mode_param]
            self.get_logger().info(f"Setting {joint_name} operating mode to {op_mode.name} (param: {control_mode_param})")
            dynamixel.set_operating_mode(servo_id, op_mode)
            time.sleep(0.5)

            # Set PID gains AFTER setting operating mode (like in head file)
            if "pid_gains" in joint:
                pid_gains = joint["pid_gains"]
                kp = pid_gains.get("kp", 400)  # Default P gain is 400
                ki = pid_gains.get("ki", 0)    # Default I gain is 0
                kd = pid_gains.get("kd", 0)    # Default D gain is 0
                
                self.get_logger().info(f"Setting {joint_name} PID gains - kp: {kp}, ki: {ki}, kd: {kd}")
                try:
                    dynamixel.set_P(servo_id, int(kp))
                    self.get_logger().info(f"Successfully set P gain to {int(kp)} for {joint_name}")
                    time.sleep(0.1)
                    dynamixel.set_I(servo_id, int(ki))
                    self.get_logger().info(f"Successfully set I gain to {int(ki)} for {joint_name}")
                    time.sleep(0.1)
                    dynamixel.set_D(servo_id, int(kd))
                    self.get_logger().info(f"Successfully set D gain to {int(kd)} for {joint_name}")
                    time.sleep(0.1)
                except Exception as e:
                    self.get_logger().error(f"Failed to set PID gains for {joint_name}: {str(e)}")
                    raise

            # Finally, enable torque for this servo.
            self.get_logger().info(f"Enabling torque for {joint_name} (Servo ID {servo_id})")
            dynamixel._enable_torque(servo_id)
            time.sleep(0.5)

        # Initialize robot interface with the collected servo IDs.
        self.robot = Robot(dynamixel=dynamixel, servo_ids=servo_ids)

        # Store servo_ids as instance variable for service callbacks
        self.servo_ids = servo_ids

        # Create publishers and subscribers
        self.state_pub = self.create_publisher(JointState, '/mars/arm/state', 10)
        self.command_sub = self.create_subscription(
            Float64MultiArray,
            '/mars/arm/commands',
            self.command_callback,
            10
        )
        
        # Create torque control services
        self.torque_on_service = self.create_service(
            Trigger,
            '/maurice_arm/torque_on',
            self.torque_on_callback
        )
        self.torque_off_service = self.create_service(
            Trigger,
            '/maurice_arm/torque_off',
            self.torque_off_callback
        )
        
        self.timer = self.create_timer(1.0 / control_frequency, self.timer_callback)
        
        # Initialize joint state message with actual URDF joint names
        self.joint_state_msg = JointState()
        # Use actual joint names from URDF instead of generic names
        self.joint_state_msg.name = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']  # Update these to match your URDF

        # Add debug logging for joint names
        #self.get_logger().info(f"Publishing joint states with names: {self.joint_state_msg.name}")

        self.latest_command = None


    def torque_on_callback(self, request, response):
        """Service callback to enable torque for all servos."""
        try:
            self.get_logger().info("Enabling torque for all servos...")
            for servo_id in self.servo_ids:
                self.robot.dynamixel._enable_torque(servo_id)
                time.sleep(0.1)  # Small delay between servos
            
            response.success = True
            response.message = f"Successfully enabled torque for {len(self.servo_ids)} servos"
            self.get_logger().info("Torque enabled for all servos")
            
        except Exception as e:
            response.success = False
            response.message = f"Failed to enable torque: {str(e)}"
            self.get_logger().error(f"Error enabling torque: {str(e)}")
        
        return response

    def torque_off_callback(self, request, response):
        """Service callback to disable torque for all servos."""
        try:
            self.get_logger().info("Disabling torque for all servos...")
            for servo_id in self.servo_ids:
                self.robot.dynamixel._disable_torque(servo_id)
                time.sleep(0.1)  # Small delay between servos
            
            response.success = True
            response.message = f"Successfully disabled torque for {len(self.servo_ids)} servos"
            self.get_logger().info("Torque disabled for all servos")
            
        except Exception as e:
            response.success = False
            response.message = f"Failed to disable torque: {str(e)}"
            self.get_logger().error(f"Error disabling torque: {str(e)}")
        
        return response

    def timer_callback(self):
        """Publish current joint states and send latest command if available."""
        try:
            positions = self.robot.read_position()
            velocities = self.robot.read_velocity()

            # Convert positions from encoder counts to radians:
            # Assuming 0 encoder count corresponds to -2048 and full revolution is 4096 counts:
            positions_rad = [((pos - 2048) * (2 * math.pi) / 4096) for pos in positions]
            
            # Flip directions for links 2, 3, 4, 6 (indices 1, 2, 3, 5)
            for i in [1, 2, 3, 5]:
                if i < len(positions_rad):
                    positions_rad[i] = -positions_rad[i]

            # Convert velocities to radians per second (if needed)
            velocities_rad = [float(vel) * 2 * math.pi / 4096 for vel in velocities]
            
            # Flip velocity directions for links 2, 3, 4, 6 (indices 1, 2, 3, 5)
            for i in [1, 2, 3, 5]:
                if i < len(velocities_rad):
                    velocities_rad[i] = -velocities_rad[i]

            self.joint_state_msg.header.stamp = self.get_clock().now().to_msg()
            self.joint_state_msg.position = positions_rad
            self.joint_state_msg.velocity = velocities_rad

            # Add debug logging occasionally to verify we're publishing valid data
            if not hasattr(self, '_position_debug_counter'):
                self._position_debug_counter = 0
            self._position_debug_counter += 1
            if self._position_debug_counter % 100 == 0:  # Log every 100 cycles
                self.get_logger().info(f"Publishing positions (rad): {[f'{p:.3f}' for p in positions_rad[:3]]}")  # Show first 3 joints

            self.state_pub.publish(self.joint_state_msg)

            # If a new command was received, update goal positions.
            if self.latest_command is not None:
                self.robot.set_goal_pos(self.latest_command)
                self.latest_command = None

        except Exception as e:
            self.get_logger().error(f"Error in timer callback: {str(e)}")

    def command_callback(self, msg: Float64MultiArray):
        """Store incoming position commands after checking joint limits."""
        try:
            # In this example, we assume the command array has one value per joint.
            # Here you might check against limits (converted to radians) if desired.
            
            # Create a copy of the command data to modify
            command_data = list(msg.data)
            
            # Intelligent joint limits: adjust joint2 limits based on joint1 position
            # Note: joint2 (index 1) will be flipped later, so we need to account for that
            if len(command_data) >= 2:  # Ensure we have at least joint1 and joint2
                joint1_pos = command_data[0]  # joint1 position in radians
                joint2_pos = command_data[1]  # joint2 position in radians (before flipping)
                
                # Get joint2 limits from config
                joint2_config = self.joints_config.get("joint_2", {})
                joint2_limits = joint2_config.get("position_limits", {})
                config_min = joint2_limits.get("min", -1.5708)  # Default fallback
                config_max = joint2_limits.get("max", 1.22)     # Default fallback
                
                # Since joint2 will be flipped, we need to invert the limits for pre-flip values
                # After flip: joint2_flipped = -joint2_pos
                # So if we want joint2_flipped to be within [config_min, config_max]
                # We need joint2_pos to be within [-config_max, -config_min]
                joint2_min_limit = -config_max  # Will become config_max after flip
                joint2_max_limit = -config_min  # Will become config_min after flip
                
                # Determine joint2's maximum limit based on joint1's position
                if joint1_pos < 1.0:
                    # When joint1 < 1.0, restrict max to 0.4 (in pre-flip regime)
                    joint2_min_limit = max(joint2_min_limit, -0.4)
                
                # Enforce the limits (before flipping)
                if joint2_pos < joint2_min_limit:
                    command_data[1] = joint2_min_limit
                
                if joint2_pos > joint2_max_limit:
                    command_data[1] = joint2_max_limit
            
            # Flip directions for links 2, 3, 4, 6 (indices 1, 2, 3, 5)
            for i in [1, 2, 3, 5]:
                if i < len(command_data):
                    command_data[i] = -command_data[i]
            
            # Then convert the command from radians to encoder counts:
            command_encoder = [int((pos / (2 * math.pi)) * 4096 + 2048) for pos in command_data]
            self.latest_command = command_encoder
        except Exception as e:
            self.get_logger().error(f"Error in command callback: {str(e)}")

def main(args=None):
    rclpy.init(args=args)
    node = MauriceArmNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
