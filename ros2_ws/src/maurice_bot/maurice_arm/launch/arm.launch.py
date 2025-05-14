from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    # Get the path to the config file
    config_file = PathJoinSubstitution([
        FindPackageShare('maurice_arm'),
        'config',
        'arm_config.yaml'
    ])

    # Create the node
    maurice_arm_node = Node(
        package='maurice_arm',
        executable='arm.py',
        name='arm',
        parameters=[config_file],
        output='screen'
    )

    # Create the camera node
    camera_node = Node(
        package='v4l2_camera',
        executable='v4l2_camera_node',
        name='v4l2_camera',
        parameters=[{
            'video_device': '/dev/video0',
            'width': 640,
            'height': 480,
            'fps': 30
        }],
        output='screen'
    )

    # Create the arm utils node
    arm_utils_node = Node(
        package='maurice_arm',
        executable='arm_utils.py',
        name='arm_utils',
        output='screen'
    )

    return LaunchDescription([
        maurice_arm_node,
        camera_node,
        arm_utils_node
    ])
