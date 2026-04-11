from launch import LaunchDescription
from launch_ros.actions import Node
import os

def generate_launch_description():
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    data_directory = os.path.join(maurice_root, 'data')

    default_hardware_revision = 'R6'

    rosbridge_node = Node(
        package='rws',
        executable='rws_server',
        name='ros_websocket_server',
        parameters=[{}],
        output='screen',
        respawn=True,
        respawn_delay=2.0,
        # Disable Zenoh SHM for rws_server to prevent __pthread_tpp_change_priority
        # crash from priority-inheritance mutex contention between websocketpp threads
        # and Zenoh SHM watchdog threads. rws_server is a JSON bridge — no SHM needed.
        additional_env={
            'ZENOH_SESSION_CONFIG_OVERRIDE': (
                'transport/shared_memory/enabled=false'
                ';transport/link/tx/queue/congestion_control/drop/wait_before_drop=900000'
                ';transport/link/tx/queue/congestion_control/drop/max_wait_before_drop_fragments=900000'
            ),
        },
    )

    app_node = Node(
        package='maurice_control',
        executable='app.cpp',
        name='maurice_app',
        output='screen',
        parameters=[{
            'data_directory': data_directory,
            'default_hardware_revision': default_hardware_revision,
        }],
    )

    return LaunchDescription([
        rosbridge_node,
        app_node,
    ])
