from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    # Node to launch the LiDAR driver
    lidar_node = Node(
        package='ld08_driver',
        executable='ld08_driver',
        name='ld08_driver',
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
        lidar_node,
        static_tf_node,
    ])