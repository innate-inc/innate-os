from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import LaunchConfiguration
from launch.actions import DeclareLaunchArgument

def generate_launch_description():
    # Declare launch arguments
    serial_port = LaunchConfiguration('serial_port', default='/dev/ttyUSB0')
    frame_id = LaunchConfiguration('frame_id', default='base_scan')
    
    # Node to launch the RPLidar A1 driver
    lidar_node = Node(
        package='rplidar_ros',
        executable='rplidar_composition',
        name='rplidar_node',
        parameters=[{
            'serial_port': serial_port,
            'frame_id': frame_id,
            'scan_mode': 'Standard',
            'angle_compensate': True,
        }],
        output='screen'
    )

    # Node to publish a static transform from base_link to laser_frame.
    # Translations are in meters, converted from millimeters:
    # X = -76.4mm = -0.0764m, Y = 0mm = 0.0m, Z = 171.65mm = 0.17165m
    static_tf_node = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='lidar_tf',
        arguments=[
            "-0.0764", "0.0", "0.17165",  # Translation: X, Y, Z (in meters)
            "0", "0", "0",                # Rotation: roll, pitch, yaw (in radians)
            "base_link", "base_scan"      # Parent and child frames
        ]
    )

    return LaunchDescription([
        DeclareLaunchArgument('serial_port', default_value=serial_port,
                              description='Serial port for the RPLidar'),
        DeclareLaunchArgument('frame_id', default_value=frame_id,
                              description='Frame ID for the laser scan data'),
        lidar_node,
        static_tf_node,
    ])