# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from mars_bringup.config_loader import get_env, load_env_file, settings_params

from brain_client.common.logging import get_logging_env_vars


def generate_launch_description():
    # Load environment variables from .env file (includes GEMINI_API_KEY for the
    # local brain — read by the node from the environment, never a ROS param).
    load_env_file()

    # Get logging environment variables
    env_vars = get_logging_env_vars()

    image_topic_arg = DeclareLaunchArgument(
        "image_topic",
        default_value="/mars/main_camera/left/image_raw/compressed",
        description="Image topic",
    )
    cmd_vel_topic_arg = DeclareLaunchArgument(
        "cmd_vel_topic",
        default_value="/cmd_vel_skills",
        description="Command velocity topic — skills-priority input of the cmd_vel mux",
    )
    map_topic_arg = DeclareLaunchArgument("map_topic", default_value="/map", description="Map topic (skills server)")
    send_arm_camera_image_arg = DeclareLaunchArgument(
        "send_arm_camera_image",
        default_value="False",
        description="Include the arm wrist camera frame in the brain's view",
    )
    simulator_mode_arg = DeclareLaunchArgument(
        "simulator_mode",
        default_value="True",
        description="Enable simulator-specific behavior",
    )
    current_nav_mode_topic_arg = DeclareLaunchArgument(
        "current_nav_mode_topic",
        default_value="/nav/current_mode",
        description="Topic for current navigation mode (mapfree, mapping, autonomous_mapping, navigation)",
    )
    log_everything_arg = DeclareLaunchArgument(
        "log_everything",
        default_value="True",
        description="Flag to enable full brain turn logging",
    )
    gemini_model_arg = DeclareLaunchArgument(
        "gemini_model",
        default_value=get_env("GEMINI_MODEL", "gemini-3.6-flash"),
        description="Gemini model powering the local brain",
    )

    brain_client_node = Node(
        package="brain_client",
        executable="brain_client_node.py",
        name="brain_client_node",
        parameters=[
            {
                "image_topic": LaunchConfiguration("image_topic"),
                "cmd_vel_topic": LaunchConfiguration("cmd_vel_topic"),
                "send_arm_camera_image": LaunchConfiguration("send_arm_camera_image"),
                "simulator_mode": LaunchConfiguration("simulator_mode"),
                "current_nav_mode_topic": LaunchConfiguration("current_nav_mode_topic"),
                "log_everything": LaunchConfiguration("log_everything"),
                "gemini_model": LaunchConfiguration("gemini_model"),
                # Sim camera mount (the config.py defaults are the hardware's).
                "x_cam": 0.0,
                "height_cam": 0.2,
            },
            *settings_params(),
        ],
        output="screen",
    )

    return LaunchDescription(
        env_vars
        + [
            image_topic_arg,
            cmd_vel_topic_arg,
            map_topic_arg,
            send_arm_camera_image_arg,
            simulator_mode_arg,
            current_nav_mode_topic_arg,
            log_everything_arg,
            gemini_model_arg,
            brain_client_node,
            Node(
                package="brain_client",
                executable="skills_server.py",
                name="skills_action_server",
                output="screen",
                parameters=[
                    {
                        "image_topic": LaunchConfiguration("image_topic"),
                        "map_topic": LaunchConfiguration("map_topic"),
                        "cmd_vel_topic": LaunchConfiguration("cmd_vel_topic"),
                    }
                ],
            ),
            # Backend for the webapp's /armsdk page, same as on the robot. The
            # sim serves everything Manipulation drives — mars_arm's ik.py is
            # the same hardware-independent KDL node, and mars_sim_driver
            # answers goto_js_v2/goto_js_trajectory and publishes
            # /mars/arm/state. Two gaps, both cosmetic here: /mars/arm/status
            # never publishes (the page shows "torque ?") and torque/reboot are
            # no-op stubs, so recover() cannot actually fix anything.
            Node(
                package="brain_client",
                executable="arm_sdk_server.py",
                name="arm_sdk_server",
                output="screen",
                respawn=True,
                respawn_delay=2.0,
                # Manipulation spins in-process helper nodes that can share a
                # name; mute the benign "Publisher already registered" warning.
                arguments=["--ros-args", "--log-level", "rcl.logging_rosout:=ERROR"],
            ),
        ]
    )
