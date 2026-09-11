#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    use_sim_time = DeclareLaunchArgument("use_sim_time", default_value="true")
    model_package = DeclareLaunchArgument("model_package", default_value="mars_cam")
    model_executable = DeclareLaunchArgument("model_executable", default_value="fast_foundation_stereo_node")
    left_image_topic = DeclareLaunchArgument("left_image_topic", default_value="/mars/main_camera/left/image_raw")
    right_image_topic = DeclareLaunchArgument("right_image_topic", default_value="/mars/main_camera/right/image_raw")
    left_camera_info_topic = DeclareLaunchArgument("left_camera_info_topic", default_value="/mars/main_camera/left/camera_info")
    right_camera_info_topic = DeclareLaunchArgument(
        "right_camera_info_topic", default_value="/mars/main_camera/right/camera_info"
    )
    depth_topic = DeclareLaunchArgument("depth_topic", default_value="/stereo/fast_foundation/depth")
    disparity_topic = DeclareLaunchArgument("disparity_topic", default_value="/stereo/fast_foundation/disparity")
    pointcloud_topic = DeclareLaunchArgument("pointcloud_topic", default_value="/stereo/fast_foundation/points")
    model_left_image_topic = DeclareLaunchArgument("model_left_image_topic", default_value="left/image_raw")
    model_right_image_topic = DeclareLaunchArgument("model_right_image_topic", default_value="right/image_raw")
    model_left_camera_info_topic = DeclareLaunchArgument("model_left_camera_info_topic", default_value="left/camera_info")
    model_right_camera_info_topic = DeclareLaunchArgument(
        "model_right_camera_info_topic", default_value="right/camera_info"
    )
    model_depth_topic = DeclareLaunchArgument("model_depth_topic", default_value="depth/image_rect_raw")
    model_disparity_topic = DeclareLaunchArgument("model_disparity_topic", default_value="disparity")
    model_pointcloud_topic = DeclareLaunchArgument("model_pointcloud_topic", default_value="points")
    model_repo = DeclareLaunchArgument(
        "model_repo",
        default_value="/home/jetson1/innate-os/ros2_ws/src/third_party/stereo_models/Fast-FoundationStereo",
    )
    model_path = DeclareLaunchArgument("model_path", default_value="")
    venv_path = DeclareLaunchArgument("venv_path", default_value="/home/jetson1/innate-os/.venvs/fast_foundation_stereo")
    add_venv_site_packages = DeclareLaunchArgument("add_venv_site_packages", default_value="false")
    disable_torch_compile_helpers = DeclareLaunchArgument("disable_torch_compile_helpers", default_value="true")
    valid_iters = DeclareLaunchArgument("valid_iters", default_value="8")
    max_disp = DeclareLaunchArgument("max_disp", default_value="192")
    scale = DeclareLaunchArgument("scale", default_value="1.0")
    pointcloud_stride = DeclareLaunchArgument("pointcloud_stride", default_value="2")
    max_depth_m = DeclareLaunchArgument("max_depth_m", default_value="5.0")
    rectify_inputs = DeclareLaunchArgument("rectify_inputs", default_value="true")
    use_amp = DeclareLaunchArgument("use_amp", default_value="true")
    synchronize_cuda_timing = DeclareLaunchArgument("synchronize_cuda_timing", default_value="true")
    log_level = DeclareLaunchArgument("log_level", default_value="warn")

    node = Node(
        package=LaunchConfiguration("model_package"),
        executable=LaunchConfiguration("model_executable"),
        name="fast_foundation_stereo",
        output="screen",
        parameters=[
            {
                "use_sim_time": LaunchConfiguration("use_sim_time"),
                "model_repo": LaunchConfiguration("model_repo"),
                "model_path": LaunchConfiguration("model_path"),
                "venv_path": LaunchConfiguration("venv_path"),
                "add_venv_site_packages": LaunchConfiguration("add_venv_site_packages"),
                "disable_torch_compile_helpers": LaunchConfiguration("disable_torch_compile_helpers"),
                "valid_iters": LaunchConfiguration("valid_iters"),
                "max_disp": LaunchConfiguration("max_disp"),
                "scale": LaunchConfiguration("scale"),
                "pointcloud_stride": LaunchConfiguration("pointcloud_stride"),
                "max_depth_m": LaunchConfiguration("max_depth_m"),
                "rectify_inputs": LaunchConfiguration("rectify_inputs"),
                "use_amp": LaunchConfiguration("use_amp"),
                "synchronize_cuda_timing": LaunchConfiguration("synchronize_cuda_timing"),
            }
        ],
        remappings=[
            (LaunchConfiguration("model_left_image_topic"), LaunchConfiguration("left_image_topic")),
            (LaunchConfiguration("model_right_image_topic"), LaunchConfiguration("right_image_topic")),
            (LaunchConfiguration("model_left_camera_info_topic"), LaunchConfiguration("left_camera_info_topic")),
            (LaunchConfiguration("model_right_camera_info_topic"), LaunchConfiguration("right_camera_info_topic")),
            (LaunchConfiguration("model_depth_topic"), LaunchConfiguration("depth_topic")),
            (LaunchConfiguration("model_disparity_topic"), LaunchConfiguration("disparity_topic")),
            (LaunchConfiguration("model_pointcloud_topic"), LaunchConfiguration("pointcloud_topic")),
        ],
        arguments=["--ros-args", "--log-level", LaunchConfiguration("log_level")],
    )

    return LaunchDescription(
        [
            use_sim_time,
            model_package,
            model_executable,
            left_image_topic,
            right_image_topic,
            left_camera_info_topic,
            right_camera_info_topic,
            depth_topic,
            disparity_topic,
            pointcloud_topic,
            model_left_image_topic,
            model_right_image_topic,
            model_left_camera_info_topic,
            model_right_camera_info_topic,
            model_depth_topic,
            model_disparity_topic,
            model_pointcloud_topic,
            model_repo,
            model_path,
            venv_path,
            add_venv_site_packages,
            disable_torch_compile_helpers,
            valid_iters,
            max_disp,
            scale,
            pointcloud_stride,
            max_depth_m,
            rectify_inputs,
            use_amp,
            synchronize_cuda_timing,
            log_level,
            node,
        ]
    )
