from launch import LaunchDescription
from launch_ros.actions import Node
import os

def generate_launch_description():
    # Use environment variable if set, otherwise construct from HOME
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    data_directory = os.path.join(maurice_root, 'data')
    
    # Default hardware revision for new robots
    default_hardware_revision = 'R6'
    
    # Create rosbridge websocket node
    rosbridge_node = Node(
        package='rws',
        executable='rws_server',
        name='ros_websocket_server',
        parameters=[{}],
        output='screen'
    )

    app_node = Node(
        package='maurice_control',
        executable='app.cpp',
        name='maurice_app',
        output='screen',
        parameters=[{
            'data_directory': data_directory,
            'default_hardware_revision': default_hardware_revision
        }]
    )

    # Return LaunchDescription with all nodes
    return LaunchDescription([
        rosbridge_node,
        app_node,
    ])
