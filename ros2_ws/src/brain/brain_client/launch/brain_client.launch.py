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
    map_topic_arg = DeclareLaunchArgument(
        "map_topic", default_value="/navigation/global_costmap/costmap", description="Map topic (skills server)"
    )
    agent_map_topic_arg = DeclareLaunchArgument(
        "agent_map_topic",
        default_value="/map",
        description="Navigation occupancy map shown to opted-in local agents",
    )
    arm_camera_image_topic_arg = DeclareLaunchArgument(
        "arm_camera_image_topic",
        default_value="/mars/arm/image_raw/compressed",
        description="Arm camera image topic",
    )
    send_arm_camera_image_arg = DeclareLaunchArgument(
        "send_arm_camera_image",
        default_value="True",
        description="Include the arm wrist camera frame in the brain's view",
    )
    simulator_mode_arg = DeclareLaunchArgument(
        "simulator_mode",
        default_value="False",
        description="Flag to enable simulator mode (auto-activates brain)",
    )
    current_nav_mode_topic_arg = DeclareLaunchArgument(
        "current_nav_mode_topic",
        default_value="/nav/current_mode",
        description="Topic for current navigation mode (mapfree, mapping, navigation)",
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

    # --- Proxy service configuration ---
    # These are service configs (not credentials) - credentials come from env vars
    cartesia_voice_id_arg = DeclareLaunchArgument(
        "cartesia_voice_id",
        default_value=get_env("CARTESIA_VOICE_ID", "9fdaae0b-f885-4813-b589-3c07cf9d5fea"),
        description="Cartesia Alfred voice id",
    )
    brain_client_node = Node(
        package="brain_client",
        executable="brain_client_node.py",
        name="brain_client_node",
        parameters=[
            {
                "image_topic": LaunchConfiguration("image_topic"),
                "cmd_vel_topic": LaunchConfiguration("cmd_vel_topic"),
                "arm_camera_image_topic": LaunchConfiguration("arm_camera_image_topic"),
                "send_arm_camera_image": LaunchConfiguration("send_arm_camera_image"),
                "simulator_mode": LaunchConfiguration("simulator_mode"),
                "current_nav_mode_topic": LaunchConfiguration("current_nav_mode_topic"),
                "agent_map_topic": LaunchConfiguration("agent_map_topic"),
                "log_everything": LaunchConfiguration("log_everything"),
                "gemini_model": LaunchConfiguration("gemini_model"),
                # Proxy service config
                "cartesia_voice_id": LaunchConfiguration("cartesia_voice_id"),
            },
            *settings_params(),
        ],
        output="screen",
        # Mute the benign "Publisher already registered" rosout-plumbing warning
        # from in-process helper nodes that can share a name.
        arguments=["--ros-args", "--log-level", "rcl.logging_rosout:=ERROR"],
    )

    return LaunchDescription(
        env_vars
        + [
            image_topic_arg,
            cmd_vel_topic_arg,
            map_topic_arg,
            agent_map_topic_arg,
            arm_camera_image_topic_arg,
            send_arm_camera_image_arg,
            simulator_mode_arg,
            current_nav_mode_topic_arg,
            log_everything_arg,
            gemini_model_arg,
            # Proxy service config args
            cartesia_voice_id_arg,
            brain_client_node,
            Node(
                package="brain_client",
                executable="skills_server.py",
                name="skills_action_server",
                output="screen",
                # Safety net: if the skills server ever dies, bring it back instead of
                # leaving the whole skill system dead until a manual restart.
                respawn=True,
                respawn_delay=2.0,
                parameters=[
                    {
                        "image_topic": LaunchConfiguration("image_topic"),
                        "map_topic": LaunchConfiguration("map_topic"),
                        "cmd_vel_topic": LaunchConfiguration("cmd_vel_topic"),
                    }
                ],
                # Skill loading spins up short-lived helper nodes (camera, tf,
                # action clients) that can share a name; mute the benign
                # "Publisher already registered" rosout-plumbing warning.
                arguments=["--ros-args", "--log-level", "rcl.logging_rosout:=ERROR"],
            ),
            # Backend for the webapp's /armsdk page: an ExecuteArmCommand
            # action (/armsdk/command) plus a slider-stream topic, driven from
            # the browser over rosbridge, that drives the Manipulation SDK.
            # Idles cheap — the arm-state feeds park between commands.
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
            # NOTE: InputManagerNode is launched separately via input_manager.launch.py
        ]
    )
