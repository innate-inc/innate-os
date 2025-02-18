from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription(
        [
            # Launch the BrainClientNode
            Node(
                package="brain_client",
                executable="brain_client_node.py",
                name="brain_client_node",
                output="screen",
                parameters=[
                    {
                        "websocket_uri": "ws://host.docker.internal:8765",
                        "token": "MY_HARDCODED_TOKEN",
                        "image_topic": "/camera/color/image_raw/compressed",
                        "cmd_vel_topic": "/cmd_vel",
                    }
                ],
            ),
            # Launch the WSClientNode (handles actual WebSocket connection)
            Node(
                package="brain_client",
                executable="ws_client_node.py",
                name="ws_client_node",
                output="screen",
                parameters=[
                    {
                        "websocket_uri": "ws://host.docker.internal:8765",
                        "token": "MY_HARDCODED_TOKEN",
                    }
                ],
            ),
            Node(
                package="brain_client",
                executable="primitive_execution_action_server.py",
                name="primitive_execution_action_server",
                output="screen",
            ),
        ]
    )
