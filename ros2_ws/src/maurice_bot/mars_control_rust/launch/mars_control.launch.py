from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([
        Node(
            package="mars_control_rust",
            executable="mars_control_rust",
            name="app_control_node",
            output="screen",
            emulate_tty=True,
            # Map relative topic names to absolute ROS topics
            remappings=[
                ("joystick", "/joystick"),
                ("cmd_vel", "/cmd_vel"),
                ("leader_positions", "/leader_positions"),
                ("mars_arm_commands", "/mars/arm/commands"),
                ("robot_info", "/robot/info"),
            ],
        ),
    ])
