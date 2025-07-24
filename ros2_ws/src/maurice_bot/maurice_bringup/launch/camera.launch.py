#!/usr/bin/env python3
import os
import math

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import OpaqueFunction, DeclareLaunchArgument, IncludeLaunchDescription
from launch.substitutions import LaunchConfiguration
from launch.launch_description_sources import PythonLaunchDescriptionSource
import launch_ros.actions

def euler_to_quat(roll, pitch, yaw):
    """
    Convert Euler angles (in radians) to a quaternion.
    Returns (qx, qy, qz, qw)
    """
    qx = math.sin(roll/2) * math.cos(pitch/2) * math.cos(yaw/2) - math.cos(roll/2) * math.sin(pitch/2) * math.sin(yaw/2)
    qy = math.cos(roll/2) * math.sin(pitch/2) * math.cos(yaw/2) + math.sin(roll/2) * math.cos(pitch/2) * math.sin(yaw/2)
    qz = math.cos(roll/2) * math.cos(pitch/2) * math.sin(yaw/2) - math.sin(roll/2) * math.sin(pitch/2) * math.cos(yaw/2)
    qw = math.cos(roll/2) * math.cos(pitch/2) * math.cos(yaw/2) + math.sin(roll/2) * math.sin(pitch/2) * math.sin(yaw/2)
    return (qx, qy, qz, qw)

def generate_launch_description():
    # Package paths
    depthai_descriptions_path = get_package_share_directory('depthai_descriptions') # For URDF
    urdf_launch_dir = os.path.join(depthai_descriptions_path, 'launch')

    # Common LaunchConfigurations
    mxId_lc         = LaunchConfiguration('mxId')
    usb2Mode_lc     = LaunchConfiguration('usb2Mode')
    camera_model_lc = LaunchConfiguration('camera_model')
    tf_prefix_lc    = LaunchConfiguration('tf_prefix')
    
    # Frame related LaunchConfigurations for URDF
    head_camera_link_lc = LaunchConfiguration('head_camera_link')

    # -------------------------------------------------------
    # Declare Launch Arguments
    # -------------------------------------------------------

    # --- TF Frame Arguments ---
    declare_head_camera_link = DeclareLaunchArgument(
        'head_camera_link', default_value='head_camera_link',
        description='Head camera frame published by head transform node ')

    # --- Device and Model Arguments (for URDF and Camera Driver) ---
    declare_mxId = DeclareLaunchArgument(
        'mxId', default_value='', 
        description='MXID of the OAK device. Empty for first available.')
    declare_usb2Mode = DeclareLaunchArgument(
        'usb2Mode', default_value='True', 
        description='Enable USB2 mode for the OAK device.')
    declare_camera_model = DeclareLaunchArgument(
        'camera_model', default_value='OAK-D', 
        description='The model of the OAK camera (e.g., OAK-D, OAK-D-LITE). Used for URDF.')
    declare_tf_prefix = DeclareLaunchArgument(
        'tf_prefix', default_value='oak', 
        description='Namespace for TF frames (e.g., oak/rgb_camera_optical_frame).')

    # --- Custom Camera Driver Specific Arguments ---
    declare_color_resolution = DeclareLaunchArgument(
        'color_resolution', default_value='800p',
        description='RGB camera resolution for the driver. Supported: 800p, 720p.')
    declare_fps = DeclareLaunchArgument(
        'fps', default_value='30.0',
        description='RGB camera FPS.')
    declare_use_video = DeclareLaunchArgument(
        'use_video', default_value='True',
        description='Enable main video stream.')
    declare_compression_format = DeclareLaunchArgument(
        'compression_format', default_value='jpeg',
        description='Compression format for the video stream (jpeg or png).')
    declare_jpeg_quality = DeclareLaunchArgument(
        'jpeg_quality', default_value='90',
        description='JPEG compression quality (0-100).')

    # -------------------------------------------------------
    # URDF Launch (Publishes Robot Description and Robot State Publisher)
    # -------------------------------------------------------
    urdf_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(urdf_launch_dir, 'urdf_launch.py')
        ),
        launch_arguments={
            'tf_prefix': tf_prefix_lc,
            'camera_model': camera_model_lc,
            'base_frame': head_camera_link_lc, # URDF base frame is directly the head_camera_link
            'parent_frame': head_camera_link_lc, # Parent is also head_camera_link (no offset)
            'cam_pos_x': '0.0', 'cam_pos_y': '0.0', 'cam_pos_z': '0.0',
            'cam_roll': '0.0', 'cam_pitch': '0.0', 'cam_yaw': '0.0'
        }.items()
    )

    # -------------------------------------------------------
    # Camera Driver Node
    # -------------------------------------------------------
    camera_driver_node = launch_ros.actions.Node(
        package='maurice_bringup',
        executable='camera_driver',
        name='camera_driver',
        output='screen',
        parameters=[{
            'tf_prefix': tf_prefix_lc,
            'camera_model': camera_model_lc,
            'color_resolution': LaunchConfiguration('color_resolution'),
            'fps': LaunchConfiguration('fps'),
            'use_video': LaunchConfiguration('use_video'),
            'compression_format': LaunchConfiguration('compression_format'),
            'jpeg_quality': LaunchConfiguration('jpeg_quality'),
            'mxId': mxId_lc,
            'usb2Mode': usb2Mode_lc,
        }]
    )

    # -------------------------------------------------------
    # Assemble the Launch Description
    # -------------------------------------------------------
    ld = LaunchDescription()

    # Add declared arguments to the LaunchDescription
    ld.add_action(declare_head_camera_link)
    ld.add_action(declare_mxId)
    ld.add_action(declare_usb2Mode)
    ld.add_action(declare_camera_model)
    ld.add_action(declare_tf_prefix)
    ld.add_action(declare_color_resolution)
    ld.add_action(declare_fps)
    ld.add_action(declare_use_video)
    ld.add_action(declare_compression_format)
    ld.add_action(declare_jpeg_quality)

    # Add nodes and other launch actions
    ld.add_action(urdf_launch)
    ld.add_action(camera_driver_node)

    return ld

if __name__ == '__main__':
    generate_launch_description()
