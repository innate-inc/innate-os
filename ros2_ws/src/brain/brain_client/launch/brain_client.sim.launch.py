# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from mars_bringup.config_loader import get_env, load_env_file, settings_params

from brain_client.common.logging import get_logging_env_vars


def generate_launch_description():
    # Load environment variables from .env file
    load_env_file()

    # Get logging environment variables
    env_vars = get_logging_env_vars()

    # Declare new launch arguments
    websocket_uri_arg = DeclareLaunchArgument(
        "websocket_uri",
        # docker-compose sets BRAIN_WEBSOCKET_URI to an empty string when no brain
        # profile is active, so an unguarded get_env default never kicks in and a
        # bare `innate restart` (no --brain-websocket-uri arg) lands an empty URI.
        # Treat empty as unset and fall back to the hosted default, matching the
        # launcher's own resolve_brain_websocket_uri.
        default_value=get_env("BRAIN_WEBSOCKET_URI", "").strip() or "wss://agent-v1.innate.bot",
        description="Websocket URI",
    )
    token_arg = DeclareLaunchArgument(
        "token",
        default_value=get_env("INNATE_SERVICE_KEY", ""),
        description="Token for authentication",
    )
    client_version_arg = DeclareLaunchArgument(
        "client_version",
        default_value=get_env("INNATE_OS_CLIENT_VERSION", ""),
        description="Robot OS client version for backend compatibility checks",
    )
    image_topic_arg = DeclareLaunchArgument(
        "image_topic",
        default_value="/mars/main_camera/left/image_raw/compressed",
        description="Image topic",
    )
    cmd_vel_topic_arg = DeclareLaunchArgument(
        "cmd_vel_topic", default_value="/cmd_vel", description="Command velocity topic"
    )
    depth_image_topic_arg = DeclareLaunchArgument(
        "depth_image_topic",
        default_value="/camera/depth/image_raw",
        description="Depth image topic",
    )
    amcl_pose_topic_arg = DeclareLaunchArgument(
        "amcl_pose_topic", default_value="/amcl_pose", description="AMCL pose topic"
    )
    map_topic_arg = DeclareLaunchArgument("map_topic", default_value="/map", description="Map topic")
    send_depth_arg = DeclareLaunchArgument(
        "send_depth",
        default_value="False",
        description="Flag to enable sending depth images",
    )
    vertical_fov_arg = DeclareLaunchArgument("vertical_fov", default_value="80.0", description="Vertical field of view")
    horizontal_resolution_arg = DeclareLaunchArgument(
        "horizontal_resolution",
        default_value="640",
        description="Horizontal resolution",
    )
    vertical_resolution_arg = DeclareLaunchArgument(
        "vertical_resolution", default_value="480", description="Vertical resolution"
    )
    x_cam_arg = DeclareLaunchArgument(
        "x_cam",
        default_value="0.0",
        description="Camera x position relative to robot base",
    )
    height_cam_arg = DeclareLaunchArgument("height_cam", default_value="0.2", description="Camera height above ground")
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
    send_arm_camera_image_arg = DeclareLaunchArgument(
        "send_arm_camera_image",
        default_value="False",
        description="Flag to enable sending arm camera images",
    )
    use_odom_as_amcl_pose_arg = DeclareLaunchArgument(
        "use_odom_as_amcl_pose",
        default_value="True",
        description="Flag to use odom as amcl pose",
    )
    simulator_mode_arg = DeclareLaunchArgument(
        "simulator_mode",
        default_value="True",
        description="Flag to enable simulator mode (uses sim navigation and auto-activates brain)",
    )
    current_nav_mode_topic_arg = DeclareLaunchArgument(
        "current_nav_mode_topic",
        default_value="/nav/current_mode",
        description="Topic for current navigation mode (mapfree, mapping, navigation)",
    )
    brain_client_node = Node(
        package="brain_client",
        executable="brain_client_node.py",
        name="brain_client_node",
        # Sim runs on /clock (mars_sim_driver publishes it). A process-level
        # --ros-args override, NOT a node parameter: these processes create
        # extra nodes at runtime (BasicNavigator instances in skills and
        # mobility) that a per-node parameters dict would never reach -- the
        # global override applies to every node in the process.
        arguments=["--ros-args", "-p", "use_sim_time:=true"],
        parameters=[
            {
                "websocket_uri": LaunchConfiguration("websocket_uri"),
                "token": LaunchConfiguration("token"),
                "client_version": LaunchConfiguration("client_version"),
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
                "send_arm_camera_image": LaunchConfiguration("send_arm_camera_image"),
                "use_odom_as_amcl_pose": LaunchConfiguration("use_odom_as_amcl_pose"),
                "simulator_mode": LaunchConfiguration("simulator_mode"),
                "x_cam": LaunchConfiguration("x_cam"),
                "height_cam": LaunchConfiguration("height_cam"),
                "current_nav_mode_topic": LaunchConfiguration("current_nav_mode_topic"),
            },
            *settings_params(),
        ],
        output="screen",
    )

    return LaunchDescription(
        env_vars
        + [
            websocket_uri_arg,
            token_arg,
            client_version_arg,
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
            send_arm_camera_image_arg,
            use_odom_as_amcl_pose_arg,
            simulator_mode_arg,
            x_cam_arg,
            height_cam_arg,
            current_nav_mode_topic_arg,
            brain_client_node,
            # WebSocket runs in-process inside brain_client_node; no separate ws_client node.
            Node(
                package="brain_client",
                executable="skills_server.py",
                name="skills_action_server",
                output="screen",
                # Same process-level sim-time override as brain_client_node:
                # skills create BasicNavigator nodes at runtime.
                arguments=["--ros-args", "-p", "use_sim_time:=true"],
                parameters=[
                    {
                        "image_topic": LaunchConfiguration("image_topic"),
                        "map_topic": LaunchConfiguration("map_topic"),
                    }
                ],
            ),
        ]
    )
