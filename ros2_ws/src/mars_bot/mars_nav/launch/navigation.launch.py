#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Navigation Launch File

Architecture:
┌─────────────────────┐     ┌─────────────────────┐
│   Grid Localizer    │────▶│        AMCL         │
│  (coarse estimate)  │     │  (fine refinement)  │
└─────────────────────┘     └─────────────────────┘
         │                           │
         │ /initialpose              │ continuous
         │ (transient_local QoS)     │ tracking
         ▼                           ▼
    Seeds AMCL's              Publishes map→odom
    particle filter           transform

Grid Localizer runs as a standalone node (not lifecycle-managed) and publishes
initial pose estimates with transient_local durability so AMCL receives them
even if it starts later.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from mars_bringup.config_loader import (
    load_costmap_rewrites,
    load_motion_limit_overrides,
    load_yaml_param_defaults,
    settings_params,
)


def generate_launch_description():
    # Get the package share directory for your package
    package_name = "mars_nav"
    share_dir = get_package_share_directory(package_name)

    # Define the paths to your YAML files
    planner_params_file = os.path.join(share_dir, "config", "planner.yaml")
    controller_params_file = os.path.join(share_dir, "config", "controller.yaml")
    costmap_params_file = os.path.join(share_dir, "config", "costmap.yaml")
    amcl_params_file = os.path.join(share_dir, "config", "amcl.yaml")
    behavior_params_file = os.path.join(share_dir, "config", "behavior.yaml")  # noqa: F841
    smoother_params_file = os.path.join(share_dir, "config", "velocity_smoother.yaml")  # noqa: F841

    # settings.yaml's /** inflation_layer.* rewrites the costmap inflation knobs. Opt-in:
    # when unset, costmap.yaml is used unchanged. RewrittenYaml is imported lazily so a
    # missing nav2_common never breaks navigation in the no-override case.
    costmap_rewrites = load_costmap_rewrites()
    if costmap_rewrites:
        try:
            from nav2_common.launch import RewrittenYaml

            costmap_params_file = RewrittenYaml(
                source_file=costmap_params_file,
                param_rewrites={key: str(value) for key, value in costmap_rewrites.items()},
                convert_types=True,
            )
        except ImportError:
            print("[navigation.launch] nav2_common not available; ignoring [navigation] costmap overrides.")

    # Use the map file - construct path from environment variable or HOME
    mars_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
    default_map_path = os.path.join(mars_root, "data", "maps", "home.yaml")  # noqa: F841

    # Declare launch arguments so that these paths can be overridden if needed

    amcl_params_arg = DeclareLaunchArgument(
        "amcl_params_file", default_value=amcl_params_file, description="Full path to the AMCL parameters file"
    )

    # ROS time source: false on the real robot (no /clock); the sim launcher
    # passes true (via mode_manager.launch.py) so the nav stack follows the
    # sim driver's /clock and freezes with the world. Layered last so it
    # overrides the yamls' hardcoded use_sim_time: false.
    use_sim_time_arg = DeclareLaunchArgument("use_sim_time", default_value="false")
    use_sim_time = {"use_sim_time": ParameterValue(LaunchConfiguration("use_sim_time"), value_type=bool)}

    # Create the map server node
    map_server_node = Node(
        package="nav2_map_server",
        executable="map_server",
        name="navigation_map_server",
        output="screen",
        parameters=[{"yaml_filename": ""}, use_sim_time],
        # nav2 boot chatter at WARN to keep `innate view` readable.
        arguments=["--ros-args", "--log-level", "warn"],
    )

    # Create the AMCL node
    amcl_node = Node(
        package="nav2_amcl",
        executable="amcl",
        name="navigation_amcl",
        output="screen",
        parameters=[LaunchConfiguration("amcl_params_file"), use_sim_time],
        arguments=["--ros-args", "--log-level", "warn"],
    )

    # Grid localizer for initial pose estimation
    # Provides coarse localization before AMCL for "kidnapped robot" / unknown initial pose
    # Uses transient_local QoS so /initialpose persists for late-starting AMCL
    grid_localizer_node = Node(
        package="mars_nav",
        executable="grid_localizer.py",
        name="navigation_grid_localizer",
        output="screen",
        parameters=[
            {
                "auto_localize": True,
                "auto_localize_timeout": 30.0,
                "max_score_threshold": 0.3,
            },
            use_sim_time,
            *settings_params(),
        ],
    )

    # Create the planner node
    planner_node = Node(
        package="nav2_planner",
        executable="planner_server",
        name="planner_server",
        namespace="navigation",
        output="screen",
        parameters=[planner_params_file, costmap_params_file, use_sim_time],
        remappings=[
            # TF remappings - critical for namespaced nodes
            ("tf", "/tf"),
            ("tf_static", "/tf_static"),
            # Remap costmap footprint to global /footprint
            ("/navigation/global_costmap/footprint", "/footprint"),
        ],
        arguments=["--ros-args", "--log-level", "warn"],
    )

    # Create the controller node
    controller_node = Node(
        package="nav2_controller",
        executable="controller_server",
        name="controller_server",
        output="screen",
        parameters=[
            controller_params_file,
            costmap_params_file,
            load_motion_limit_overrides("mppi", defaults=load_yaml_param_defaults(controller_params_file)),
            use_sim_time,
        ],
        remappings=[
            ("cmd_vel", "cmd_vel_raw"),
            # Remap costmap footprint to global /footprint
            ("/local_costmap/footprint", "/footprint"),
        ],
        arguments=["--ros-args", "--log-level", "warn"],
    )
    # ros2 run --prefix 'valgrind --leak-check=full --track-origins=yes --log-file=valgrind_output.txt' nav2_controller controller_server --ros-args -r __node:=controller_server -r cmd_vel:=cmd_vel_raw -r /local_costmap/footprint:=/footprint --params-file $(ros2 pkg prefix mars_nav)/share/mars_nav/config/controller.yaml --params-file $(ros2 pkg prefix mars_nav)/share/mars_nav/config/costmap.yaml

    return LaunchDescription(
        [
            amcl_params_arg,
            use_sim_time_arg,
            # Nav2 lifecycle-managed nodes
            map_server_node,
            grid_localizer_node,
            amcl_node,
            planner_node,
            controller_node,
        ]
    )
