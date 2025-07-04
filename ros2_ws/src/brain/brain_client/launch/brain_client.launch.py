from launch import LaunchDescription
from launch_ros.actions import Node
from brain_client.logging_config import get_logging_env_vars
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    # Get logging environment variables
    env_vars = get_logging_env_vars()

    # Declare new launch arguments
    websocket_uri_arg = DeclareLaunchArgument(
        "websocket_uri",
        default_value="wss://innate-agent-websocket-service-533276562345.us-central1.run.app",
        description="Websocket URI",
    )
    token_arg = DeclareLaunchArgument(
        "token",
        default_value="MY_HARDCODED_TOKEN",
        description="Token for authentication",
    )
    image_topic_arg = DeclareLaunchArgument(
        "image_topic",
        default_value="/color/image/compressed",
        description="Image topic",
    )
    cmd_vel_topic_arg = DeclareLaunchArgument(
        "cmd_vel_topic", default_value="/cmd_vel", description="Command velocity topic"
    )
    depth_image_topic_arg = DeclareLaunchArgument(
        "depth_image_topic",
        default_value="/depth/image_raw",
        description="Depth image topic",
    )
    amcl_pose_topic_arg = DeclareLaunchArgument(
        "amcl_pose_topic", default_value="/amcl_pose", description="AMCL pose topic"
    )
    map_topic_arg = DeclareLaunchArgument(
        "map_topic", default_value="/global_costmap/costmap", description="Map topic"
    )
    send_depth_arg = DeclareLaunchArgument(
        "send_depth",
        default_value="False",
        description="Flag to enable sending depth images",
    )
    vertical_fov_arg = DeclareLaunchArgument(
        "vertical_fov", default_value="80.0", description="Vertical field of view"
    )
    horizontal_resolution_arg = DeclareLaunchArgument(
        "horizontal_resolution",
        default_value="1280",
        description="Horizontal resolution",
    )
    vertical_resolution_arg = DeclareLaunchArgument(
        "vertical_resolution", default_value="800", description="Vertical resolution"
    )
    x_cam_arg = DeclareLaunchArgument(
        "x_cam",
        default_value="0.0197",
        description="Camera x position relative to robot base",
    )
    height_cam_arg = DeclareLaunchArgument(
        "height_cam", default_value="0.19663", description="Camera height above ground"
    )
    pose_image_interval_arg = DeclareLaunchArgument(
        "pose_image_interval",
        default_value="0.5",
        description="Send pose images every X seconds",
    )
    log_everything_arg = DeclareLaunchArgument(
        "log_everything",
        default_value="True",
        description="Flag to enable complete vision agent output logging",
    )
    arm_camera_image_topic_arg = DeclareLaunchArgument(
        "arm_camera_image_topic",
        default_value="/image_raw/compressed",
        description="Arm camera image topic",
    )
    send_arm_camera_image_arg = DeclareLaunchArgument(
        "send_arm_camera_image",
        default_value="True",
        description="Flag to enable sending arm camera images",
    )
    simulator_mode_arg = DeclareLaunchArgument(
        "simulator_mode",
        default_value="False",
        description="Flag to enable simulator mode (uses sim navigation and auto-activates brain)",
    )

    # Launch the BrainClientNode
    brain_client_node = Node(
        package="brain_client",
        executable="brain_client_node.py",
        name="brain_client_node",
        parameters=[
            {
                "websocket_uri": LaunchConfiguration("websocket_uri"),
                "token": LaunchConfiguration("token"),
                "image_topic": LaunchConfiguration("image_topic"),
                "map_topic": LaunchConfiguration("map_topic"),
                "cmd_vel_topic": LaunchConfiguration("cmd_vel_topic"),
                "depth_image_topic": LaunchConfiguration("depth_image_topic"),
                "amcl_pose_topic": LaunchConfiguration("amcl_pose_topic"),
                "send_depth": LaunchConfiguration("send_depth"),
                "vertical_fov": LaunchConfiguration("vertical_fov"),
                "horizontal_resolution": LaunchConfiguration("horizontal_resolution"),
                "vertical_resolution": LaunchConfiguration("vertical_resolution"),
                "pose_image_interval": LaunchConfiguration("pose_image_interval"),
                "log_everything": LaunchConfiguration("log_everything"),
                "arm_camera_image_topic": LaunchConfiguration("arm_camera_image_topic"),
                "send_arm_camera_image": LaunchConfiguration("send_arm_camera_image"),
                "simulator_mode": LaunchConfiguration("simulator_mode"),
                "x_cam": LaunchConfiguration("x_cam"),
                "height_cam": LaunchConfiguration("height_cam"),
            }
        ],
        output="screen",
    )

    return LaunchDescription(
        env_vars
        + [
            websocket_uri_arg,
            token_arg,
            image_topic_arg,
            cmd_vel_topic_arg,
            depth_image_topic_arg,
            amcl_pose_topic_arg,
            map_topic_arg,
            send_depth_arg,
            vertical_fov_arg,
            horizontal_resolution_arg,
            vertical_resolution_arg,
            pose_image_interval_arg,
            log_everything_arg,
            arm_camera_image_topic_arg,
            send_arm_camera_image_arg,
            simulator_mode_arg,
            x_cam_arg,
            height_cam_arg,
            brain_client_node,
            # Launch the WSClientNode (handles actual WebSocket connection)
            Node(
                package="brain_client",
                executable="ws_client_node.py",
                name="ws_client_node",
                output="screen",
                parameters=[
                    {
                        "websocket_uri": LaunchConfiguration("websocket_uri"),
                        "token": LaunchConfiguration("token"),
                    }
                ],
            ),
            Node(
                package="brain_client",
                executable="primitive_execution_action_server.py",
                name="primitive_execution_action_server",
                output="screen",
                parameters=[
                    {
                        "image_topic": LaunchConfiguration("image_topic"),
                        "map_topic": LaunchConfiguration("map_topic"),
                        "simulator_mode": LaunchConfiguration("simulator_mode"),
                    }
                ],
            ),
        ]
    )
