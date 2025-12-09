from launch import LaunchDescription
from launch_ros.actions import Node
import os

def generate_launch_description():
    # Use environment variable if set, otherwise construct from HOME
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    data_directory = os.path.join(maurice_root, 'data')


    # Default hardware revision for new robots
    default_hardware_revision = 'R6'

    # Create Rosbridge Server - WebSocket connection for web clients

    rosbridge_node = Node(
        package='rosbridge_server',
        executable='rosbridge_websocket',
        name='rosbridge_websocket',
        parameters=[{
            'port': 9090,  # Default rosbridge port
            'address': '0.0.0.0',  # Listen on all interfaces
        }],
        output='screen'
    )

    # Create Foxglove Bridge - much more efficient than rosbridge for high-bandwidth data
    # Uses binary protocol instead of JSON, better memory management, native image compression
    foxglove_bridge_node = Node(
        package='foxglove_bridge',
        executable='foxglove_bridge',
        name='foxglove_bridge',
        parameters=[{
            'port': 8765,  # Default Foxglove port
            'address': '0.0.0.0',  # Listen on all interfaces
            'tls': False,  # No TLS encryption
            'certfile': '',
            'keyfile': '',
            'topic_whitelist': ['.*'],  # Allow all topics (can restrict if needed)
            'service_whitelist': ['.*'],  # Allow all services
            'param_whitelist': ['.*'],  # Allow all parameters
            'max_qos_depth': 10,  # Limit queue depth to prevent memory buildup
            'num_threads': 0,  # Use optimal thread count
            'send_buffer_limit': 10000000,  # 10MB send buffer limit
            'use_compression': True,  # Enable compression for large messages
        }],
        output='screen'
    )

    # Create your app node
    app_node = Node(
        package='maurice_control',
        executable='app.py',
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
        foxglove_bridge_node,
        app_node
    ])
