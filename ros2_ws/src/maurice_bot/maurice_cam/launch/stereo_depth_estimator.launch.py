#!/usr/bin/env python3
"""
Launch file for the stereo depth estimator node.

This launch file runs the stereo depth estimator as a standalone node or as a
composable node with intra-process communication enabled.

Uses VPI Block Matching algorithm with:
- Downscaling to calibration resolution for processing
- OpenCV rectification (accurate)
- VPI Block Matching (fast, good quality)
- No post-filtering (cleanest results)
- Upscaling depth back to input resolution

Usage:
    # Standalone node (subscribes to existing stereo topic)
    ros2 launch maurice_cam stereo_depth_estimator.launch.py

    # With custom parameters
    ros2 launch maurice_cam stereo_depth_estimator.launch.py max_disparity:=80 publish_disparity:=true

    # With composable mode for zero-copy IPC (when main camera is in same container)
    ros2 launch maurice_cam stereo_depth_estimator.launch.py use_composable:=true
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node, ComposableNodeContainer
from launch_ros.descriptions import ComposableNode


def generate_launch_description():
    # Declare launch arguments
    use_composable_arg = DeclareLaunchArgument(
        'use_composable',
        default_value='false',
        description='Run as composable node in a container (enables zero-copy IPC)'
    )
    
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )
    
    data_directory_arg = DeclareLaunchArgument(
        'data_directory',
        default_value='/home/jetson1/innate-os/data',
        description='Data directory containing calibration_config_* folder'
    )
    
    stereo_topic_arg = DeclareLaunchArgument(
        'stereo_topic',
        default_value='/mars/main_camera/stereo',
        description='Input stereo image topic (side-by-side left+right)'
    )
    
    depth_topic_arg = DeclareLaunchArgument(
        'depth_topic',
        default_value='/mars/main_camera/depth',
        description='Output depth image topic (16UC1 in millimeters)'
    )
    
    disparity_topic_arg = DeclareLaunchArgument(
        'disparity_topic',
        default_value='/mars/main_camera/disparity',
        description='Output disparity visualization topic (mono8)'
    )
    
    frame_id_arg = DeclareLaunchArgument(
        'frame_id',
        default_value='camera_optical_frame',
        description='TF frame ID for output messages'
    )
    
    max_disparity_arg = DeclareLaunchArgument(
        'max_disparity',
        default_value='80',
        description='Maximum disparity for stereo matching (higher = closer objects, more compute)'
    )
    
    publish_disparity_arg = DeclareLaunchArgument(
        'publish_disparity',
        default_value='true',
        description='Publish disparity visualization image'
    )
    
    rectified_topic_arg = DeclareLaunchArgument(
        'rectified_topic',
        default_value='/mars/main_camera/stereo_rectified',
        description='Output rectified left image topic (mono8, matches depth image)'
    )
    
    publish_rectified_arg = DeclareLaunchArgument(
        'publish_rectified',
        default_value='true',
        description='Publish rectified left image (overlaps with depth output)'
    )
    
    stereo_width_arg = DeclareLaunchArgument(
        'stereo_width',
        default_value='1280',
        description='Expected stereo image width (full stereo frame, left+right combined)'
    )
    
    stereo_height_arg = DeclareLaunchArgument(
        'stereo_height',
        default_value='480',
        description='Expected stereo image height'
    )
    
    process_every_n_frames_arg = DeclareLaunchArgument(
        'process_every_n_frames',
        default_value='5',
        description='Process 1 out of every N frames (1=all, 2=half rate, 5=6Hz from 30Hz, etc.)'
    )
    
    publish_pointcloud_arg = DeclareLaunchArgument(
        'publish_pointcloud',
        default_value='true',
        description='Publish 3D point cloud from depth'
    )
    
    pointcloud_topic_arg = DeclareLaunchArgument(
        'pointcloud_topic',
        default_value='/mars/main_camera/points',
        description='Point cloud output topic'
    )
    
    pointcloud_decimation_arg = DeclareLaunchArgument(
        'pointcloud_decimation',
        default_value='8',
        description='Point cloud decimation (1=full, 2=half, 4=quarter, 8=1/8th resolution)'
    )
    
    # Block Matching parameters (from Python script that worked best)
    bm_window_size_arg = DeclareLaunchArgument(
        'bm_window_size',
        default_value='11',
        description='Block matching window size (odd number, typically 5-15)'
    )
    
    bm_quality_arg = DeclareLaunchArgument(
        'bm_quality',
        default_value='8',
        description='Block matching quality level (0-8, higher = better quality but slower)'
    )
    
    bm_conf_threshold_arg = DeclareLaunchArgument(
        'bm_conf_threshold',
        default_value='16000',
        description='Confidence threshold (0-65535, lower = more details/noise)'
    )

    # Common parameters for both modes
    node_parameters = [{
        'use_sim_time': LaunchConfiguration('use_sim_time'),
        'data_directory': LaunchConfiguration('data_directory'),
        'stereo_topic': LaunchConfiguration('stereo_topic'),
        'depth_topic': LaunchConfiguration('depth_topic'),
        'disparity_topic': LaunchConfiguration('disparity_topic'),
        'rectified_topic': LaunchConfiguration('rectified_topic'),
        'frame_id': LaunchConfiguration('frame_id'),
        'max_disparity': LaunchConfiguration('max_disparity'),
        'publish_disparity': LaunchConfiguration('publish_disparity'),
        'publish_rectified': LaunchConfiguration('publish_rectified'),
        'stereo_width': LaunchConfiguration('stereo_width'),
        'stereo_height': LaunchConfiguration('stereo_height'),
        'process_every_n_frames': LaunchConfiguration('process_every_n_frames'),
        'publish_pointcloud': LaunchConfiguration('publish_pointcloud'),
        'pointcloud_topic': LaunchConfiguration('pointcloud_topic'),
        'pointcloud_decimation': LaunchConfiguration('pointcloud_decimation'),
        'bm_window_size': LaunchConfiguration('bm_window_size'),
        'bm_quality': LaunchConfiguration('bm_quality'),
        'bm_conf_threshold': LaunchConfiguration('bm_conf_threshold'),
    }]

    # Standalone node (default)
    standalone_node = Node(
        package='maurice_cam',
        executable='stereo_depth_estimator',
        name='stereo_depth_estimator',
        parameters=node_parameters,
        output='screen',
        emulate_tty=True,
        condition=UnlessCondition(LaunchConfiguration('use_composable')),
    )

    # Composable node in container (for zero-copy IPC)
    composable_container = ComposableNodeContainer(
        name='depth_estimator_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container_mt',
        composable_node_descriptions=[
            ComposableNode(
                package='maurice_cam',
                plugin='maurice_cam::StereoDepthEstimator',
                name='stereo_depth_estimator',
                parameters=node_parameters,
                extra_arguments=[{'use_intra_process_comms': True}],
            ),
        ],
        output='screen',
        emulate_tty=True,
        condition=IfCondition(LaunchConfiguration('use_composable')),
    )

    return LaunchDescription([
        # Launch arguments
        use_composable_arg,
        use_sim_time_arg,
        data_directory_arg,
        stereo_topic_arg,
        depth_topic_arg,
        disparity_topic_arg,
        rectified_topic_arg,
        publish_rectified_arg,
        frame_id_arg,
        max_disparity_arg,
        publish_disparity_arg,
        stereo_width_arg,
        stereo_height_arg,
        process_every_n_frames_arg,
        publish_pointcloud_arg,
        pointcloud_topic_arg,
        pointcloud_decimation_arg,
        bm_window_size_arg,
        bm_quality_arg,
        bm_conf_threshold_arg,
        # Nodes (one will be active based on use_composable)
        standalone_node,
        composable_container,
    ])
