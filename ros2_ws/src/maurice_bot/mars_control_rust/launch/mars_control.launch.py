from launch import LaunchDescription
from launch_ros.actions import Node
import os

def generate_launch_description():
    # Use environment variable if set, otherwise construct from HOME
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    data_directory = os.path.join(maurice_root, 'data')
    
    # Create rosbridge websocket node
    rosbridge_node = Node(
        package='rosbridge_server',
        executable='rosbridge_websocket',
        name='rosbridge_websocket',
        parameters=[{'address': '0.0.0.0'}],
        output='screen'
    )

    # Create your app node
    app_node = Node(
        package="mars_control_rust",
        executable="mars_control_rust",
        name="app_control_node",
        output="screen",
        emulate_tty=True,
        parameters=[{
            'data_directory': data_directory
        }],
        # Map relative topic names to absolute ROS topics
        remappings=[
            ("joystick", "/joystick"),
            ("cmd_vel", "/cmd_vel"),
            ("leader_positions", "/leader_positions"),
            ("mars_arm_commands", "/mars/arm/commands"),
            ("robot_info", "/robot/info"),
        ],
    )

    # Return LaunchDescription with both nodes
    return LaunchDescription([
        rosbridge_node,
        app_node
    ])
