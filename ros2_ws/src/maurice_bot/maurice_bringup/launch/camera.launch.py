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
    
    # Frame related LaunchConfigurations for TF and URDF
    base_link_lc   = LaunchConfiguration('base_link')
    camera_link_lc = LaunchConfiguration('camera_link')
    oak_d_base_lc  = LaunchConfiguration('oak_d_base')

    # -------------------------------------------------------
    # Declare Launch Arguments
    # -------------------------------------------------------

    # --- TF Frame Arguments ---
    declare_base_link = DeclareLaunchArgument(
        'base_link', default_value='base_link',
        description='Robot base frame for the static transform.')
    declare_camera_link = DeclareLaunchArgument(
        'camera_link', default_value='camera_link',
        description='Intermediate frame: child of static transform, parent of URDF oak_d_base.')
    declare_oak_d_base = DeclareLaunchArgument(
        'oak_d_base', default_value='oak_d_base',
        description='Base frame of the camera, as defined in URDF (child of camera_link).')

    # --- Static Transform Publisher Arguments (base_link to camera_link pose) ---
    declare_measured_cam_pos_x = DeclareLaunchArgument('measured_cam_pos_x', default_value='0.01039', description='Measured camera X offset from base_link.')
    declare_measured_cam_pos_y = DeclareLaunchArgument('measured_cam_pos_y', default_value='0.00708', description='Measured camera Y offset from base_link.')
    declare_measured_cam_pos_z = DeclareLaunchArgument('measured_cam_pos_z', default_value='0.1976', description='Measured camera Z offset from base_link.')
    declare_measured_cam_roll  = DeclareLaunchArgument('measured_cam_roll', default_value='0.0', description='Measured camera roll offset from base_link (radians).')
    declare_measured_cam_pitch = DeclareLaunchArgument('measured_cam_pitch', default_value='0.17453', description='Measured camera pitch offset from base_link (radians).') # Default 10 deg
    declare_measured_cam_yaw   = DeclareLaunchArgument('measured_cam_yaw', default_value='0.0', description='Measured camera yaw offset from base_link (radians).')

    # --- Device and Model Arguments (for URDF and Camera Driver) ---
    declare_mxId = DeclareLaunchArgument('mxId', default_value='', description='MXID of the OAK device. Empty for first available.')
    declare_usb2Mode = DeclareLaunchArgument('usb2Mode', default_value='Trueus', description='Enable USB2 mode for the OAK device.')
    declare_camera_model = DeclareLaunchArgument('camera_model', default_value='OAK-D', description='The model of the OAK camera (e.g., OAK-D, OAK-D-LITE). Used for URDF.')
    declare_tf_prefix = DeclareLaunchArgument('tf_prefix', default_value='oak', description='Namespace for TF frames (e.g., oak/rgb_camera_optical_frame).')

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
            'base_frame': oak_d_base_lc, # URDF's own base frame, child of camera_link
            'parent_frame': camera_link_lc, # Connects URDF to the rest of the TF tree
            'cam_pos_x': '0.0', 'cam_pos_y': '0.0', 'cam_pos_z': '0.0',
            'cam_roll': '0.0', 'cam_pitch': '0.0', 'cam_yaw': '0.0'
        }.items()
    )

    # -------------------------------------------------------
    # Static Transform Publisher (base_link -> camera_link)
    # -------------------------------------------------------
    def launch_static_tf_func(context, *args, **kwargs):
        x = float(LaunchConfiguration('measured_cam_pos_x').perform(context))
        y = float(LaunchConfiguration('measured_cam_pos_y').perform(context))
        z = float(LaunchConfiguration('measured_cam_pos_z').perform(context))
        roll = float(LaunchConfiguration('measured_cam_roll').perform(context))
        pitch = float(LaunchConfiguration('measured_cam_pitch').perform(context))
        yaw = float(LaunchConfiguration('measured_cam_yaw').perform(context))
        qx, qy, qz, qw = euler_to_quat(roll, pitch, yaw)
        
        return [launch_ros.actions.Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='static_tf_base_to_camera',
            output='screen',
            arguments=[
                str(x), str(y), str(z),
                str(qx), str(qy), str(qz), str(qw),
                base_link_lc.perform(context), camera_link_lc.perform(context)
            ]
        )]
    static_tf_action = OpaqueFunction(function=launch_static_tf_func)

    # -------------------------------------------------------
    # Custom Camera Driver Node
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
    ld.add_action(declare_base_link)
    ld.add_action(declare_camera_link)
    ld.add_action(declare_oak_d_base)
    ld.add_action(declare_measured_cam_pos_x)
    ld.add_action(declare_measured_cam_pos_y)
    ld.add_action(declare_measured_cam_pos_z)
    ld.add_action(declare_measured_cam_roll)
    ld.add_action(declare_measured_cam_pitch)
    ld.add_action(declare_measured_cam_yaw)
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
    ld.add_action(static_tf_action)
    ld.add_action(camera_driver_node)

    return ld

if __name__ == '__main__':
    generate_launch_description()
