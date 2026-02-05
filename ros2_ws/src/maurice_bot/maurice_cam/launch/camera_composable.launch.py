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
from launch.actions import DeclareLaunchArgument, TimerAction
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import ComposableNodeContainer, LoadComposableNodes
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

    # ==========================================================================
    # Bi3D Freespace Segmentation launch arguments
    # ==========================================================================
    bi3d_featnet_engine_arg = DeclareLaunchArgument(
        'bi3d_featnet_engine',
        default_value='/home/jetson1/innate-os/ISAAC_ROS_WS/isaac_ros_assets/models/bi3d/bi3d_featnet.engine',
        description='Path to Bi3D Featnet TensorRT engine'
    )
    bi3d_segnet_engine_arg = DeclareLaunchArgument(
        'bi3d_segnet_engine',
        default_value='/home/jetson1/innate-os/ISAAC_ROS_WS/isaac_ros_assets/models/bi3d/bi3d_segnet.engine',
        description='Path to Bi3D Segnet TensorRT engine'
    )
    bi3d_max_disparity_arg = DeclareLaunchArgument(
        'bi3d_max_disparity',
        default_value='64',
        description='Maximum disparity values for Bi3D inference'
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

    # ==========================================================================
    # Isaac ROS stereo_image_proc pipeline (GPU-accelerated SGM via NITROS)
    # Outputs in /mars/main_camera/isaac_image_proc namespace
    # ==========================================================================
    
    ISAAC_NS = '/mars/main_camera/isaac_image_proc'
    
    # ==========================================================================
    # Rectify + Scale pipeline for ESS (preserve aspect ratio)
    # 640x480 -> 480x360 (rectify with resize) -> 480x288 (center crop)
    # RectifyNode has built-in resize, reducing NITROS hops
    # ==========================================================================
    
    # Isaac ROS Rectify+Resize Left: 640x480 -> 480x360 (uniform 0.75x scale)
    isaac_rectify_left = ComposableNode(
        package='isaac_ros_image_proc',
        plugin='nvidia::isaac_ros::image_proc::RectifyNode',
        name='rectify_left',
        namespace=ISAAC_NS,
        remappings=[
            ('image_raw', '/mars/main_camera/left/image_raw'),
            ('camera_info', '/mars/main_camera/left/camera_info'),
            ('image_rect', 'left/image_rect_color'),
            ('camera_info_rect', 'left/camera_info_rect'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'output_width': 480,   # Resize during rectify (uniform 0.75x)
            'output_height': 360,  # Preserves 4:3 aspect ratio
            'type_negotiation_duration_s': 5,  # Longer NITROS negotiation timeout
        }],
    )
    
    # Isaac ROS Rectify+Resize Right: 640x480 -> 480x360 (uniform 0.75x scale)
    isaac_rectify_right = ComposableNode(
        package='isaac_ros_image_proc',
        plugin='nvidia::isaac_ros::image_proc::RectifyNode',
        name='rectify_right',
        namespace=ISAAC_NS,
        remappings=[
            ('image_raw', '/mars/main_camera/right/image_raw'),
            ('camera_info', '/mars/main_camera/right/camera_info'),
            ('image_rect', 'right/image_rect_color'),
            ('camera_info_rect', 'right/camera_info_rect'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'output_width': 480,
            'output_height': 360,
            'type_negotiation_duration_s': 5,
        }],
    )
    
    # Crop left: 480x360 -> 480x288 (bottom crop, removes 72px from top)
    isaac_crop_left = ComposableNode(
        package='isaac_ros_image_proc',
        plugin='nvidia::isaac_ros::image_proc::CropNode',
        name='crop_left',
        namespace=ISAAC_NS,
        remappings=[
            ('image', 'left/image_rect_color'),
            ('camera_info', 'left/camera_info_rect'),
            ('crop/image', 'left/image_cropped'),
            ('crop/camera_info', 'left/camera_info_cropped'),
        ],
        parameters=[{
            'input_width': 480,
            'input_height': 360,
            'crop_width': 480,
            'crop_height': 288,
            'crop_mode': 'CENTER',  # Keep bottom of frame (removes 72px from top)
            'type_negotiation_duration_s': 5,
        }],
    )
    
    # Crop right: 480x360 -> 480x288 (bottom crop, removes 72px from top)
    isaac_crop_right = ComposableNode(
        package='isaac_ros_image_proc',
        plugin='nvidia::isaac_ros::image_proc::CropNode',
        name='crop_right',
        namespace=ISAAC_NS,
        remappings=[
            ('image', 'right/image_rect_color'),
            ('camera_info', 'right/camera_info_rect'),
            ('crop/image', 'right/image_cropped'),
            ('crop/camera_info', 'right/camera_info_cropped'),
        ],
        parameters=[{
            'input_width': 480,
            'input_height': 360,
            'crop_width': 480,
            'crop_height': 288,
            'crop_mode': 'CENTER',  # Keep bottom of frame (removes 72px from top)
            'type_negotiation_duration_s': 5,
        }],
    )

    # Isaac ROS Format Converter Left (RGB8 -> MONO8 for disparity)
    # NOTE: image_width/height control GPU buffer allocation (~576MB at 1920x1200)
    isaac_format_left = ComposableNode(
        package='isaac_ros_image_proc',
        plugin='nvidia::isaac_ros::image_proc::ImageFormatConverterNode',
        name='format_converter_left',
        namespace=ISAAC_NS,
        remappings=[
            ('image_raw', 'left/image_rect_color'),
            ('image', 'left/image_rect'),
        ],
        parameters=[{
            'encoding_desired': 'mono8',
            'image_width': 480,   # Match rectified image width (default: 1920)
            'image_height': 360,  # Match rectified image height (default: 1200)
            'type_negotiation_duration_s': 5,
        }],
    )

    # Isaac ROS Format Converter Right (RGB8 -> MONO8 for disparity)
    isaac_format_right = ComposableNode(
        package='isaac_ros_image_proc',
        plugin='nvidia::isaac_ros::image_proc::ImageFormatConverterNode',
        name='format_converter_right',
        namespace=ISAAC_NS,
        remappings=[
            ('image_raw', 'right/image_rect_color'),
            ('image', 'right/image_rect'),
        ],
        parameters=[{
            'encoding_desired': 'mono8',
            'image_width': 480,   # Match rectified image width (default: 1920)
            'image_height': 360,  # Match rectified image height (default: 1200)
            'type_negotiation_duration_s': 5,
        }],
    )
    
    # Isaac ROS Disparity Node (GPU SGM via VPI)
    # Uses CUDA backend by default, can use ORIN for hardware acceleration
    # NOTE: SGM expects BGR8/RGB8 images (nitros_image_bgr8) - VPI handles grayscale conversion internally
    # WARNING: Isaac ROS 3.2 DisparityNode has hardcoded ~840MB GPU buffer pool (no output_width/height params)
    # Consider using ESS instead, or upgrade to Isaac ROS 4.x for configurable buffers
    isaac_disparity = ComposableNode(
        package='isaac_ros_stereo_image_proc',
        plugin='nvidia::isaac_ros::stereo_image_proc::DisparityNode',
        name='disparity_node',
        namespace=ISAAC_NS,
        remappings=[
            ('left/image_rect', 'left/image_rect_color'),   # Use color images directly (bgr8)
            ('left/camera_info', 'left/camera_info_rect'),
            ('right/image_rect', 'right/image_rect_color'), # Use color images directly (bgr8)
            ('right/camera_info', 'right/camera_info_rect'),
            ('disparity', 'disparity_unfiltered'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'backend': 'CUDA',  # Options: CUDA, XAVIER, ORIN (JETSON in newer API)
            'max_disparity': 128.0,  # Must be 128 or 256 for ORIN/JETSON backend
            'window_size': 7,
            'num_passes': 2,
            'p1': 8,  # Penalty for disparity changes of +/- 1
            'p2': 120,  # Penalty for disparity changes > 1
            'p2_alpha': 1,
            'quality': 1,
            'confidence_threshold': 60000,
            'type_negotiation_duration_s': 5,
        }],
    )

    # ==========================================================================
    # ESS DNN Stereo Disparity (uses cropped 480x288 images)
    # Input: 480x288 from crop nodes (proper aspect ratio preserved)
    # Output: 480x288 disparity (no internal resize needed)
    # ==========================================================================

    # Isaac ROS ESS DNN Disparity Node (Deep Learning based)
    # Uses cropped 480x288 images - proper aspect ratio, no distortion
    # Outputs to disparity_unfiltered, then median filter produces disparity
    isaac_dnn_disparity = ComposableNode(
        package='isaac_ros_ess',
        plugin='nvidia::isaac_ros::dnn_stereo_depth::ESSDisparityNode',
        name='ess_disparity_node',
        namespace=ISAAC_NS,
        remappings=[
            ('left/image_rect', 'left/image_cropped'),
            ('left/camera_info', 'left/camera_info_cropped'),
            ('right/image_rect', 'right/image_cropped'),
            ('right/camera_info', 'right/camera_info_cropped'),
            ('disparity', 'disparity_unfiltered'),
        ],
        parameters=[{
            'engine_file_path': "/home/jetson1/innate-os/ISAAC_ROS_WS/isaac_ros_assets/models/dnn_stereo_disparity/dnn_stereo_disparity_v4.1.0_onnx/light_ess.engine",
            'image_type': 'BGR_U8',  # Match camera output format (bgr8)
            'threshold': 0.10,  # Confidence threshold (0.0 = fully dense)
            'type_negotiation_duration_s': 5,
        }],
    )

    # Disparity Median Filter - applies OpenCV median filter to smooth disparity
    disparity_median_filter = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::DisparityMedianFilter',
        name='disparity_median_filter',
        namespace=ISAAC_NS,
        remappings=[
            ('disparity_unfiltered', 'disparity_unfiltered'),
            ('disparity', 'disparity_median'),  # Output to intermediate topic
        ],
        parameters=[
            LaunchConfiguration('camera_config'),
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Disparity Edge-Preserving Filter - Domain Transform based spatial filtering
    # Smooths disparity while preserving depth discontinuities (object edges)
    disparity_edge_preserving_filter = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::DisparityEdgePreservingFilter',
        name='disparity_edge_preserving_filter',
        namespace=ISAAC_NS,
        remappings=[
            ('disparity_unfiltered', 'disparity_unfiltered'),  # Input from median filter
            ('disparity', 'disparity'),                    # Final output
        ],
        parameters=[
            LaunchConfiguration('camera_config'),
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Isaac ROS Disparity to Depth (using ESS disparity at 480x288)
    # WARNING: Isaac ROS 3.2 DisparityToDepthNode has hardcoded ~880MB GPU buffer pool
    # (block_size=9.2MB x 100 blocks, no output_width/height params available)
    isaac_disparity_to_depth = ComposableNode(
        package='isaac_ros_stereo_image_proc',
        plugin='nvidia::isaac_ros::stereo_image_proc::DisparityToDepthNode',
        name='disparity_to_depth',
        namespace=ISAAC_NS,
        remappings=[
            ('disparity', 'disparity'),
            ('depth', 'depth'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'type_negotiation_duration_s': 5,
        }],
    )
    
    # Isaac ROS Point Cloud from disparity + rectified color (480x288 after crop)
    # Uses cropped left image which matches ESS disparity resolution
    # IMPORTANT: output_width/output_height control GPU buffer allocation!
    # Default is 1920x1200 which allocates ~1.3GB GPU memory.
    # Set to match your actual resolution to reduce memory usage significantly.
    isaac_pointcloud = ComposableNode(
        package='isaac_ros_stereo_image_proc',
        plugin='nvidia::isaac_ros::stereo_image_proc::PointCloudNode',
        name='point_cloud_node',
        namespace=ISAAC_NS,
        remappings=[
            ('left/image_rect_color', 'left/image_cropped'),  # From crop node (480x288)
            ('left/camera_info', 'left/camera_info_cropped'),       # From crop node
            ('right/camera_info', 'right/camera_info_cropped'),     # From crop node
            ('disparity', 'disparity'),
            ('points2', 'points'),
        ],
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'use_color': True,
            'unit_scaling': 1.0,
            'output_width': 480,    # Match your cropped image width (default: 1920)
            'output_height': 288,   # Match your cropped image height (default: 1200)
            'type_negotiation_duration_s': 5,
        }],
    )

    # PassThrough filter - transforms point cloud to base_link frame
    isaac_voxel_filter = ComposableNode(
        package='pcl_ros',
        plugin='pcl_ros::VoxelGrid',
        name='pointcloud_voxel_filter',
        namespace=ISAAC_NS,
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

    # ==========================================================================
    # Bi3D Freespace Segmentation (uses cropped stereo images)
    # Outputs occupancy grid for navigation - better at detecting low obstacles
    # ==========================================================================
    
    # Bi3D Node - neural network stereo matching for freespace detection
    # Uses cropped 480x288 images from crop nodes
    isaac_bi3d = ComposableNode(
        package='isaac_ros_bi3d',
        plugin='nvidia::isaac_ros::bi3d::Bi3DNode',
        name='bi3d_node',
        namespace=ISAAC_NS,
        remappings=[
            ('left_image_bi3d', 'left/image_cropped'),
            ('right_image_bi3d', 'right/image_cropped'),
            ('left_camera_info_bi3d', 'left/camera_info_cropped'),
            ('right_camera_info_bi3d', 'right/camera_info_cropped'),
            ('bi3d_node/bi3d_output', 'bi3d_mask'),
        ],
        parameters=[{
            'featnet_engine_file_path': LaunchConfiguration('bi3d_featnet_engine'),
            'segnet_engine_file_path': LaunchConfiguration('bi3d_segnet_engine'),
            'max_disparity_values': LaunchConfiguration('bi3d_max_disparity'),
            'image_width': 480,
            'image_height': 288,
            'type_negotiation_duration_s': 5,
        }],
        extra_arguments=[{'use_intra_process_comms': False}],
    )
    
    # Freespace Segmentation Node - converts Bi3D mask to occupancy grid
    # Focal length scaled: 273.58 * 0.75 = 205.2 px (after 640->480 resize)
    isaac_freespace = ComposableNode(
        package='isaac_ros_bi3d_freespace',
        plugin='nvidia::isaac_ros::bi3d_freespace::FreespaceSegmentationNode',
        name='freespace_segmentation_node',
        namespace=ISAAC_NS,
        remappings=[
            ('bi3d_mask', 'bi3d_mask'),
            ('freespace_segmentation/occupancy_grid', 'freespace_occupancy_grid'),
        ],
        parameters=[{
            'base_link_frame': 'base_link',
            'camera_frame': 'fixed_camera_optical_frame',
            'f_x': 205.2,  # Scaled focal length (273.58 * 0.75)
            'f_y': 205.2,
            'grid_height': 200,   # 2m x 2m grid at 1cm resolution
            'grid_width': 200,
            'grid_resolution': 0.01,  # 1cm per cell
            'type_negotiation_duration_s': 5,
        }],
        extra_arguments=[{'use_intra_process_comms': False}],
    )

    # ==========================================================================
    # Container setup - separate containers for CPU profiling or single for perf
    # ==========================================================================
    
    # All nodes to include in the container(s)
    all_nodes = [
        main_camera_node,
        arm_camera_node,
        # webrtc_node,
        # depth_estimator_node,
        main_camera_info_node,
        # stereo_image_proc pipeline (OpenCV CPU)
        # sip_rectify_color_left,
        # sip_rectify_mono_left,
        # sip_rectify_color_right,
        # sip_rectify_mono_right,
        # sip_disparity_node,
        # sip_pointcloud_node,
        # sip_pointcloud_filter,
        # Isaac ROS pipeline (GPU NITROS)
        # Rectify+Resize: 640x480 -> 480x360 (uniform 0.75x scale during rectify)
        isaac_rectify_left,
        isaac_rectify_right,
        # Crop: 480x360 -> 480x288 (center crop, preserves aspect ratio)
        isaac_crop_left,
        isaac_crop_right,
        # Format converters NOT needed - SGM expects bgr8 (nitros_image_bgr8)
        # isaac_format_left,
        # isaac_format_right,
        # isaac_disparity,      # SGM - uses bgr8 directly from rectify

        isaac_dnn_disparity, #loaded separately with 3s delay
        # isaac_disparity_to_depth,
        # isaac_pointcloud,
        # isaac_passthrough_filter,
        # isaac_pc_filter,
        # isaac_sor_filter,
        # isaac_radius_filter,
        # isaac_voxel_filter,
        
                        # isaac_dnn_disparity,
                        # disparity_median_filter,
                        # disparity_edge_preserving_filter,  # Domain Transform edge-aware filter
                        isaac_pointcloud,
                        # isaac_passthrough_filter,
                        # isaac_pc_filter,
                        # isaac_sor_filter,
                        # isaac_radius_filter,
 
        # Bi3D Freespace Segmentation (better at detecting low obstacles like socks)
        # isaac_bi3d,
        # isaac_freespace,
    ]

    # Toggle: True = separate containers per node (for CPU profiling)
    #         False = single container (for zero-copy intra-process perf)
    SEPARATE_CONTAINERS = False

    if SEPARATE_CONTAINERS:
        # Create one container per node for individual CPU monitoring
        containers = [
            ComposableNodeContainer(
                name=f'{node.node_name[0].text}_container',
                namespace='',
                package='rclcpp_components',
                executable='component_container_mt',
                composable_node_descriptions=[node],
                output='screen',
                emulate_tty=True,
            )
            for node in all_nodes
        ]
    else:
        # Single container with all nodes (zero-copy intra-process comms)
        containers = [
            ComposableNodeContainer(
                name='camera_container',
                namespace='',
                package='rclcpp_components',
                executable='component_container_mt',
                composable_node_descriptions=all_nodes,
                output='screen',
                emulate_tty=True,
            )
        ]

    # # Load ESS node with 10 second delay
    # load_ess_delayed = TimerAction(
    #     period=10.0,
    #     actions=[
    #         LoadComposableNodes(
    #             target_container='camera_container',
    #             composable_node_descriptions=[
    #                ],
    #         )
    #     ]
    # )

    return LaunchDescription([
        # Launch arguments
        launch_main_camera_arg,
        launch_arm_camera_arg,
        launch_webrtc_arg,
        use_sim_time_arg,
        # Camera pipeline config
        camera_config_arg,
        # Bi3D Freespace args
        bi3d_featnet_engine_arg,
        bi3d_segnet_engine_arg,
        bi3d_max_disparity_arg,
        # Container(s)
        *containers,
        # Load ESS after 3 seconds
        # load_ess_delayed,
    ])
