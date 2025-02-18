#!/usr/bin/env python3
import os
import math

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import OpaqueFunction, DeclareLaunchArgument, IncludeLaunchDescription
from launch.substitutions import LaunchConfiguration
from launch.launch_description_sources import PythonLaunchDescriptionSource
import launch_ros.actions
import launch_ros.descriptions

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
    # Get package directories
    depthai_examples_path = get_package_share_directory('depthai_examples')
    depthai_descriptions_path = get_package_share_directory('depthai_descriptions')
    urdf_launch_dir = os.path.join(depthai_descriptions_path, 'launch')
    default_resource_folder = os.path.join(depthai_examples_path, 'resources')

    # -------------------------------------------------------
    # Launch Arguments for Device and Frame Configuration
    # -------------------------------------------------------
    # Device/driver parameters
    mxId         = LaunchConfiguration('mxId')
    usb2Mode     = LaunchConfiguration('usb2Mode')
    poeMode      = LaunchConfiguration('poeMode')
    camera_model = LaunchConfiguration('camera_model')
    tf_prefix    = LaunchConfiguration('tf_prefix')
    imuMode      = LaunchConfiguration('imuMode')

    # Frame names for the TF chain:
    # - base_link: robot base
    # - camera_link: will receive the measured offset from base_link (via static publisher)
    # - oak_d_base: defined in the URDF as the camera’s mounting frame relative to camera_link (zero offset)
    declare_base_link = DeclareLaunchArgument(
        'base_link', default_value='base_link',
        description='Robot base frame.')
    declare_camera_link = DeclareLaunchArgument(
        'camera_link', default_value='camera_link',
        description='Intermediate frame: measured offset from base_link is published here.')
    declare_oak_d_base = DeclareLaunchArgument(
        'oak_d_base', default_value='oak_d_base',
        description='Final sensor frame as used by DepthAI nodes (attached to camera_link with zero offset via URDF).')

    # -------------------------------------------------------
    # Measured Offset Arguments (for static transform publisher)
    # -------------------------------------------------------
    declare_measured_cam_pos_x = DeclareLaunchArgument(
        'measured_cam_pos_x', default_value='0.0197',
        description='Measured camera X offset from base_link in meters.')
    declare_measured_cam_pos_y = DeclareLaunchArgument(
        'measured_cam_pos_y', default_value='0.0',
        description='Measured camera Y offset from base_link in meters.')
    declare_measured_cam_pos_z = DeclareLaunchArgument(
        'measured_cam_pos_z', default_value='0.19663',
        description='Measured camera Z offset from base_link in meters.')
    declare_measured_cam_roll = DeclareLaunchArgument(
        'measured_cam_roll', default_value='0.0',
        description='Measured camera roll offset from base_link in radians.')
    declare_measured_cam_pitch = DeclareLaunchArgument(
        'measured_cam_pitch', default_value='0.17453',
        description='Measured camera pitch offset from base_link in radians (10°).')
    declare_measured_cam_yaw = DeclareLaunchArgument(
        'measured_cam_yaw', default_value='0.0',
        description='Measured camera yaw offset from base_link in radians.')

    # -------------------------------------------------------
    # Other Device/Driver Arguments
    # -------------------------------------------------------
    declare_mxId = DeclareLaunchArgument(
        'mxId', default_value='x',
        description='Select device by mxId.')
    declare_usb2Mode = DeclareLaunchArgument(
        'usb2Mode', default_value='False',
        description='Set to true to use USB2 mode.')
    declare_poeMode = DeclareLaunchArgument(
        'poeMode', default_value='False',
        description='Set to true if using a POE model.')
    declare_camera_model = DeclareLaunchArgument(
        'camera_model', default_value='OAK-D',
        description='Camera model (e.g., OAK-D).')
    declare_tf_prefix = DeclareLaunchArgument(
        'tf_prefix', default_value='oak',
        description='TF prefix for the camera frames.')
    declare_imuMode = DeclareLaunchArgument(
        'imuMode', default_value='1',
        description='IMU mode setting.')

    # -------------------------------------------------------
    # New Resolution Arguments for Depth and RGB
    # -------------------------------------------------------
    declare_mono_resolution = DeclareLaunchArgument(
        'monoResolution', default_value='800p',
        description='Mono (stereo) camera resolution for depth (e.g., 800p, 720p, 480p, 400p).')
    declare_rgb_resolution = DeclareLaunchArgument(
        'rgbResolution', default_value='1080p',
        description='RGB camera resolution (e.g., 800p, 1080p, 4K, 12MP, 13MP).')

    # -------------------------------------------------------
    # Resource Folder Argument for NN Blob
    # -------------------------------------------------------
    declare_resource_base_folder = DeclareLaunchArgument(
        'resourceBaseFolder', default_value=default_resource_folder,
        description='Path to the folder containing NN Blob.')

    # -------------------------------------------------------
    # New Scaling Arguments for RGB Camera Resolution
    # -------------------------------------------------------
    declare_rgb_scale_numerator = DeclareLaunchArgument(
        'rgbScaleNumerator', default_value='1',
        description='Scaling numerator for RGB camera resolution. Default is 1 (no scaling).')
    declare_rgb_scale_denominator = DeclareLaunchArgument(
        'rgbScaleDinominator', default_value='1',
        description='Scaling denominator for RGB camera resolution. Default is 1 (no scaling).')

    # -------------------------------------------------------
    # URDF Launch: Attach oak_d_base to camera_link.
    # -------------------------------------------------------
    urdf_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(urdf_launch_dir, 'urdf_launch.py')
        ),
        launch_arguments={
            'tf_prefix': tf_prefix,
            'camera_model': camera_model,
            # In the URDF, attach oak_d_base to camera_link with zero offset.
            'base_frame': LaunchConfiguration('oak_d_base'),
            'parent_frame': LaunchConfiguration('camera_link'),
            'cam_pos_x': '0.0',
            'cam_pos_y': '0.0',
            'cam_pos_z': '0.0',
            'cam_roll': '0.0',
            'cam_pitch': '0.0',
            'cam_yaw': '0.0'
        }.items()
    )

    # -------------------------------------------------------
    # Static Transform Publisher:
    # -------------------------------------------------------
    def launch_static_tf(context, *args, **kwargs):
        x = float(LaunchConfiguration('measured_cam_pos_x').perform(context))
        y = float(LaunchConfiguration('measured_cam_pos_y').perform(context))
        z = float(LaunchConfiguration('measured_cam_pos_z').perform(context))
        roll = float(LaunchConfiguration('measured_cam_roll').perform(context))
        pitch = float(LaunchConfiguration('measured_cam_pitch').perform(context))
        yaw = float(LaunchConfiguration('measured_cam_yaw').perform(context))
        base_link_val = LaunchConfiguration('base_link').perform(context)
        camera_link_val = LaunchConfiguration('camera_link').perform(context)
        qx, qy, qz, qw = euler_to_quat(roll, pitch, yaw)
        static_tf_node = launch_ros.actions.Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='static_tf_base_to_camera',
            output='screen',
            arguments=[
                str(x), str(y), str(z),
                str(qx), str(qy), str(qz), str(qw),
                base_link_val, camera_link_val
            ]
        )
        return [static_tf_node]

    static_tf_action = OpaqueFunction(function=launch_static_tf)

    # -------------------------------------------------------
    # DepthAI Camera Driver Node (stereo_inertial_node)
    # -------------------------------------------------------
    stereo_node = launch_ros.actions.Node(
        package='depthai_examples',
        executable='stereo_inertial_node',
        name='stereo_inertial_node',
        output='screen',
        parameters=[{
            'mxId': mxId,
            'usb2Mode': usb2Mode,
            'poeMode': poeMode,
            'tf_prefix': tf_prefix,
            'camera_model': camera_model,
            # Use oak_d_base as the frame for sensor data.
            'base_frame': LaunchConfiguration('oak_d_base'),
            'parent_frame': LaunchConfiguration('oak_d_base'),
            'imuMode': imuMode,
            'monoResolution': LaunchConfiguration('monoResolution'),
            'rgbResolution': LaunchConfiguration('rgbResolution'),
            'resourceBaseFolder': LaunchConfiguration('resourceBaseFolder'),
            'rgbScaleNumerator': LaunchConfiguration('rgbScaleNumerator'),
            'rgbScaleDinominator': LaunchConfiguration('rgbScaleDinominator')
        }]
    )

    # -------------------------------------------------------
    # Composable Node for Converting Depth to Metric (Meters)
    # -------------------------------------------------------
    depth_metric_converter = launch_ros.descriptions.ComposableNode(
        package='depth_image_proc',
        plugin='depth_image_proc::ConvertMetricNode',
        name='convert_metric_node',
        remappings=[('image_raw', 'stereo/depth'),
                    ('camera_info', 'stereo/camera_info'),
                    ('image', 'stereo/converted_depth')]
    )

    metric_container = launch_ros.actions.ComposableNodeContainer(
        name='metric_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container',
        composable_node_descriptions=[depth_metric_converter],
        output='screen'
    )

    # -------------------------------------------------------
    # Assemble the Launch Description
    # -------------------------------------------------------
    ld = LaunchDescription()

    # Add all launch argument declarations
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
    ld.add_action(declare_poeMode)
    ld.add_action(declare_camera_model)
    ld.add_action(declare_tf_prefix)
    ld.add_action(declare_imuMode)
    ld.add_action(declare_mono_resolution)
    ld.add_action(declare_rgb_resolution)
    ld.add_action(declare_resource_base_folder)
    ld.add_action(declare_rgb_scale_numerator)
    ld.add_action(declare_rgb_scale_denominator)

    # Add nodes and included launch files
    ld.add_action(urdf_launch)
    ld.add_action(static_tf_action)
    ld.add_action(stereo_node)
    ld.add_action(metric_container)

    return ld

if __name__ == '__main__':
    generate_launch_description()
