#!/usr/bin/env python3
"""
Launch file for the stereo camera calibration node.

This launches an interactive calibration tool that:
1. Subscribes to the stereo camera topic
2. Allows user to capture images by pressing Enter
3. Detects ChArUco board corners in both cameras
4. Performs stereo calibration after collecting enough images
5. Optionally saves the calibration to replace the existing one

Usage:
    ros2 launch maurice_cam stereo_calibrator.launch.py

    # With custom ChArUco board parameters
    ros2 launch maurice_cam stereo_calibrator.launch.py squares_x:=8 squares_y:=11 square_size:=0.016 marker_size:=0.012
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    # Declare launch arguments
    stereo_topic_arg = DeclareLaunchArgument(
        "stereo_topic",
        default_value="/mars/main_camera/stereo",
        description="Input stereo image topic (side-by-side left+right)",
    )

    stereo_width_arg = DeclareLaunchArgument(
        "stereo_width", default_value="1280", description="Total stereo frame width (left + right combined)"
    )

    stereo_height_arg = DeclareLaunchArgument("stereo_height", default_value="480", description="Stereo frame height")

    data_directory_arg = DeclareLaunchArgument(
        "data_directory",
        default_value="/home/jetson1/innate-os/data",
        description="Data directory containing calibration_config_* folder",
    )

    # ChArUco board parameters (default: 8x11 board with 16mm squares, 12mm markers)
    squares_x_arg = DeclareLaunchArgument(
        "squares_x", default_value="8", description="Number of squares in X direction (width) on ChArUco board"
    )

    squares_y_arg = DeclareLaunchArgument(
        "squares_y", default_value="11", description="Number of squares in Y direction (height) on ChArUco board"
    )

    square_size_arg = DeclareLaunchArgument(
        "square_size", default_value="0.016", description="Size of each square in meters (16mm = 0.016)"
    )

    marker_size_arg = DeclareLaunchArgument(
        "marker_size", default_value="0.012", description="Size of ArUco markers in meters (12mm = 0.012)"
    )

    # Calibration parameters
    num_images_arg = DeclareLaunchArgument(
        "num_images", default_value="30", description="Number of images to capture for calibration"
    )

    min_corners_arg = DeclareLaunchArgument(
        "min_corners", default_value="20", description="Minimum number of corners required to accept an image"
    )

    use_legacy_pattern_arg = DeclareLaunchArgument(
        "use_legacy_pattern",
        default_value="true",
        description="Enable legacy pattern support for calib.io boards (OpenCV 4.6.0+)",
    )

    # Create the calibrator node
    calibrator_node = Node(
        package="maurice_cam",
        executable="stereo_calibrator",
        name="stereo_calibrator",
        output="screen",
        emulate_tty=True,
        parameters=[
            {
                "stereo_topic": LaunchConfiguration("stereo_topic"),
                "stereo_width": LaunchConfiguration("stereo_width"),
                "stereo_height": LaunchConfiguration("stereo_height"),
                "data_directory": LaunchConfiguration("data_directory"),
                "squares_x": LaunchConfiguration("squares_x"),
                "squares_y": LaunchConfiguration("squares_y"),
                "square_size": LaunchConfiguration("square_size"),
                "marker_size": LaunchConfiguration("marker_size"),
                "num_images": LaunchConfiguration("num_images"),
                "min_corners": LaunchConfiguration("min_corners"),
                "use_legacy_pattern": LaunchConfiguration("use_legacy_pattern"),
            }
        ],
    )

    return LaunchDescription(
        [
            # Launch arguments
            stereo_topic_arg,
            stereo_width_arg,
            stereo_height_arg,
            data_directory_arg,
            squares_x_arg,
            squares_y_arg,
            square_size_arg,
            marker_size_arg,
            num_images_arg,
            min_corners_arg,
            use_legacy_pattern_arg,
            # Node
            calibrator_node,
        ]
    )
