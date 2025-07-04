#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
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
        self.declare_parameter('device_name', '/dev/ttyACM1')
        self.declare_parameter('baud_rate', 1000000)
        self.declare_parameter('control_frequency', 100)
        # Declare 'joints' as a string parameter with empty JSON object as default
        self.declare_parameter('joints', '{}')

        device_name = self.get_parameter('device_name').value
        baud_rate = self.get_parameter('baud_rate').value
        control_frequency = self.get_parameter('control_frequency').value
        # Get the joints parameter as a string and parse it as JSON
        joints_str = self.get_parameter('joints').value
        joints_param = json.loads(joints_str)

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

            # Finally, enable torque for this servo.
            self.get_logger().info(f"Enabling torque for {joint_name} (Servo ID {servo_id})")
            dynamixel._enable_torque(servo_id)
            time.sleep(0.5)

        # Initialize robot interface with the collected servo IDs.
        self.robot = Robot(dynamixel=dynamixel, servo_ids=servo_ids)

        # Create publishers and subscribers
        self.state_pub = self.create_publisher(JointState, '/maurice_arm/state', 10)
        self.command_sub = self.create_subscription(
            Float64MultiArray,
            '/maurice_arm/commands',
            self.command_callback,
            10
        )
        self.timer = self.create_timer(1.0 / control_frequency, self.timer_callback)
        
        # Initialize joint state message (assuming number of joints equals len(servo_ids))
        self.joint_state_msg = JointState()
        self.joint_state_msg.name = [f'joint_{i}' for i in range(1, len(servo_ids)+1)]
        
        self.latest_command = None

    def timer_callback(self):
        """Publish current joint states and send latest command if available."""
        try:
            positions = self.robot.read_position()
            velocities = self.robot.read_velocity()

            # Convert positions from encoder counts to radians:
            # Assuming 0 encoder count corresponds to -2048 and full revolution is 4096 counts:
            positions_rad = [((pos - 2048) * (2 * math.pi) / 4096) for pos in positions]

            # Convert velocities to radians per second (if needed)
            velocities_rad = [float(vel) * 2 * math.pi / 4096 for vel in velocities]

            self.joint_state_msg.header.stamp = self.get_clock().now().to_msg()
            self.joint_state_msg.position = positions_rad
            self.joint_state_msg.velocity = velocities_rad

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
            # Then convert the command from radians to encoder counts:
            command_encoder = [int((pos / (2 * math.pi)) * 4096 + 2048) for pos in msg.data]
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
