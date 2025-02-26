from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
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
        package='maurice_control',
        executable='app.py',
        name='maurice_app',
        output='screen'
    )

    # Return LaunchDescription with both nodes
    return LaunchDescription([
        rosbridge_node,
        app_node
    ])
