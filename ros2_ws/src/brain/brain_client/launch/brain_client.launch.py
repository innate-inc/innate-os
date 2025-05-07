from launch import LaunchDescription
from launch_ros.actions import Node
from brain_client.logging_config import get_logging_env_vars


def generate_launch_description():
    # Get logging environment variables
    env_vars = get_logging_env_vars()

    return LaunchDescription(
        env_vars
        + [
            # Launch the BrainClientNode
            Node(
                package="brain_client",
                executable="brain_client_node.py",
                name="brain_client_node",
                output="screen",
                parameters=[
                    {
                        "websocket_uri": "wss://innate-agent-websocket-service-533276562345.us-central1.run.app",
                        "token": "MY_HARDCODED_TOKEN",
                        "image_topic": "/color/image/compressed",
                        "cmd_vel_topic": "/cmd_vel",
                        "pose_image_interval": 0.5,  # Send pose images every 0.5 seconds
                        "log_everything": True,  # Flag to enable complete vision agent output logging
                        "send_depth": False,
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
                        "websocket_uri": "wss://innate-agent-websocket-service-533276562345.us-central1.run.app",
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
