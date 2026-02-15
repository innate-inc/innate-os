from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Use C++ RWS server instead of Python rosbridge for better performance
    # (~4.5x lower CPU usage)
    rws_node = Node(
        package='rws',
        executable='rws_server',
        name='rosbridge_websocket',  # Keep same node name for compatibility
        output='screen',
        parameters=[{
            'port': 9090,
            'rosbridge_compatible': True,
        }]
    )

    return LaunchDescription([rws_node])
