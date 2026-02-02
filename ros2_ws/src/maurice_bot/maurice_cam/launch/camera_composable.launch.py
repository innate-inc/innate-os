#!/usr/bin/env python3
"""
Composable node launch file for maurice_cam.

This launch file runs all camera nodes in a single process using ROS 2 composition.
With intra-process communication enabled, image data is passed via shared_ptr
instead of being serialized, eliminating copy overhead between nodes.

Usage:
    ros2 launch maurice_cam camera_composable.launch.py

    # With options:
    ros2 launch maurice_cam camera_composable.launch.py launch_main_camera:=true launch_arm_camera:=true launch_webrtc:=true
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    # Declare launch arguments
    launch_main_camera_arg = DeclareLaunchArgument(
        'launch_main_camera',
        default_value='true',
        description='Launch the main camera driver'
    )
    
    launch_arm_camera_arg = DeclareLaunchArgument(
        'launch_arm_camera',
        default_value='true',
        description='Launch the arm camera driver'
    )
    
    launch_webrtc_arg = DeclareLaunchArgument(
        'launch_webrtc',
        default_value='true',
        description='Launch the WebRTC streamer'
    )
    
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )

    # Camera pipeline config file
    camera_config_arg = DeclareLaunchArgument(
        'camera_config',
        default_value=PathJoinSubstitution([
            FindPackageShare('maurice_cam'),
            'config',
            'stereo_depth_estimator.yaml'
        ]),
        description='Path to camera pipeline config file'
    )

    # Main camera driver node
    main_camera_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::MainCameraDriver',
        name='main_camera_driver',
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    
    # Arm camera driver node
    arm_camera_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::ArmCameraDriver',
        name='arm_camera_driver',
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    
    # WebRTC streamer node
    webrtc_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::WebRTCStreamer',
        name='webrtc_streamer',
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'live_main_camera_topic': '/mars/main_camera/image',
            'live_arm_camera_topic': '/mars/arm/image_raw',
            'replay_main_camera_topic': '/brain/recorder/replay/main_camera/image',
            'replay_arm_camera_topic': '/brain/recorder/replay/arm_camera/image_raw',
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    
    # Stereo depth estimator node
    depth_estimator_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::StereoDepthEstimator',
        name='stereo_depth_estimator',
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Main camera info publisher node (publishes left/right CameraInfo + set_camera_info services)
    main_camera_info_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::MainCameraInfo',
        name='main_camera_info',
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # ==========================================================================
    # stereo_image_proc pipeline (for side-by-side comparison with VPI depth)
    # Outputs in /mars/main_camera/stereo_image_proc namespace
    # ==========================================================================

    # Left camera rectify for color (for point cloud)
    sip_rectify_color_left = ComposableNode(
        package='image_proc',
        plugin='image_proc::RectifyNode',
        name='rectify_color_left',
        namespace='/mars/main_camera/stereo_image_proc',
        remappings=[
            ('image', '/mars/main_camera/left/image_raw'),
            ('camera_info', '/mars/main_camera/left/camera_info'),
            ('image_rect', 'left/image_rect_color'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'image_transport': 'raw',
            'publish_compressed': False,
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Left camera rectify for mono (for disparity)
    sip_rectify_mono_left = ComposableNode(
        package='image_proc',
        plugin='image_proc::RectifyNode',
        name='rectify_mono_left',
        namespace='/mars/main_camera/stereo_image_proc',
        remappings=[
            ('image', '/mars/main_camera/left/image_raw'),
            ('camera_info', '/mars/main_camera/left/camera_info'),
            ('image_rect', 'left/image_rect'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'image_transport': 'raw',
            'publish_compressed': False,
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Left camera rectify for mono (for disparity)
    sip = ComposableNode(
        package='stereo_image_proc',
        plugin='stereo_image_proc',
        name='rectify_mono_left',
        namespace='/mars/main_camera/stereo_image_proc',
        remappings=[
            ('image', '/mars/main_camera/left/image_raw'),
            ('camera_info', '/mars/main_camera/left/camera_info'),
            ('image_rect', 'left/image_rect'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'image_transport': 'raw',
            'publish_compressed': False,
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )



    # Right camera rectify for color (for point cloud)
    sip_rectify_color_right = ComposableNode(
        package='image_proc',
        plugin='image_proc::RectifyNode',
        name='rectify_color_right',
        namespace='/mars/main_camera/stereo_image_proc',
        remappings=[
            ('image', '/mars/main_camera/right/image_raw'),
            ('camera_info', '/mars/main_camera/right/camera_info'),
            ('image_rect', 'right/image_rect_color'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'image_transport': 'raw',
            'publish_compressed': False,
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Right camera rectify for mono (for disparity)
    sip_rectify_mono_right = ComposableNode(
        package='image_proc',
        plugin='image_proc::RectifyNode',
        name='rectify_mono_right',
        namespace='/mars/main_camera/stereo_image_proc',
        remappings=[
            ('image', '/mars/main_camera/right/image_raw'),
            ('camera_info', '/mars/main_camera/right/camera_info'),
            ('image_rect', 'right/image_rect'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'image_transport': 'raw',
            'publish_compressed': False,
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Disparity node (stereo matching)
    sip_disparity_node = ComposableNode(
        package='stereo_image_proc',
        plugin='stereo_image_proc::DisparityNode',
        name='disparity_node',
        namespace='/mars/main_camera/stereo_image_proc',
        remappings=[
            ('left/image_rect', 'left/image_rect'),
            ('left/camera_info', '/mars/main_camera/left/camera_info'),
            ('right/image_rect', 'right/image_rect'),
            ('right/camera_info', '/mars/main_camera/right/camera_info'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'approximate_sync': True,  # Our cameras are synced but timestamps may differ slightly
            'sgbm_mode': 2,  # SGBM_3WAY - good balance of quality and speed
            'min_disparity': 0,
            'disparity_range': 64,
            'correlation_window_size': 15,
            'uniqueness_ratio': 15.0,
            'speckle_size': 100,
            'speckle_range': 4,
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Point cloud node (from disparity + color image)
    sip_pointcloud_node = ComposableNode(
        package='stereo_image_proc',
        plugin='stereo_image_proc::PointCloudNode',
        name='point_cloud_node',
        namespace='/mars/main_camera/stereo_image_proc',
        remappings=[
            ('left/image_rect_color', 'left/image_rect_color'),
            ('left/camera_info', '/mars/main_camera/left/camera_info'),
            ('right/camera_info', '/mars/main_camera/right/camera_info'),
            # disparity is in same namespace, no remap needed
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'approximate_sync': True,
            'use_color': True,
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # CropBox filter for stereo_image_proc point cloud
    sip_pointcloud_filter = ComposableNode(
        package='pcl_ros',
        plugin='pcl_ros::CropBox',
        name='pointcloud_cropbox_filter',
        namespace='/mars/main_camera/stereo_image_proc',
        remappings=[
            ('input', 'points'),
            ('output', 'points_filtered'),
        ],
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Create the composable node container
    # All nodes run in the same process, enabling zero-copy intra-process communication
    container = ComposableNodeContainer(
        name='camera_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container_mt',  # Multi-threaded container for parallel callbacks
        composable_node_descriptions=[
            main_camera_node,
            arm_camera_node,
            webrtc_node,
            depth_estimator_node,
            main_camera_info_node,
            # stereo_image_proc pipeline (for comparison testing)
            sip_rectify_color_left,
            sip_rectify_mono_left,
            sip_rectify_color_right,
            sip_rectify_mono_right,
            sip_disparity_node,
            sip_pointcloud_node,
            sip_pointcloud_filter,
        ],
        output='screen',
        emulate_tty=True,
    )

    return LaunchDescription([
        # Launch arguments
        launch_main_camera_arg,
        launch_arm_camera_arg,
        launch_webrtc_arg,
        use_sim_time_arg,
        # Camera pipeline config
        camera_config_arg,
        # Container with all nodes
        container,
    ])
