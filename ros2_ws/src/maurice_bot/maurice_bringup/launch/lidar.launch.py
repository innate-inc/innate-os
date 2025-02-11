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
    # Translations are in meters: X = -0.07055, Y = 0.0, Z = 0.16018.
    # No rotation is applied (roll, pitch, yaw are all 0).
    static_tf_node = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='lidar_tf',  # Updated node name here
        arguments=[
            "-0.07055", "0.0", "0.16018",  # Translation: X, Y, Z (in meters)
            "0", "0", "0",                # Rotation: roll, pitch, yaw (in radians)
            "base_link", "laser_frame"     # Parent and child frames
        ]
    )

    return LaunchDescription([
        lidar_node,
        static_tf_node,
    ])