from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare
import os
import yaml
from ament_index_python.packages import get_package_share_directory


def load_yaml(package_name, file_path):
    """Load a yaml file from package share directory"""
    package_path = get_package_share_directory(package_name)
    absolute_file_path = os.path.join(package_path, file_path)
    
    try:
        with open(absolute_file_path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error loading {absolute_file_path}: {e}")
        return {}


def generate_launch_description():
    # Get package directories
    maurice_arm_share = get_package_share_directory('maurice_arm')
    maurice_sim_share = get_package_share_directory('maurice_sim')
    
    # Get the path to the arm config file (ROS 2 param format)
    arm_config_file = PathJoinSubstitution([
        FindPackageShare('maurice_arm'),
        'config',
        'arm_config.yaml'
    ])
    
    # Load URDF file
    urdf_file = os.path.join(maurice_sim_share, 'urdf', 'maurice.urdf')
    with open(urdf_file, 'r') as f:
        robot_description = f.read()
    
    # Load SRDF file
    srdf_file = os.path.join(maurice_sim_share, 'urdf', 'arm.srdf')
    with open(srdf_file, 'r') as f:
        robot_description_semantic = f.read()
    
    # Load MoveIt config (MoveIt format, will be added as dict)
    moveit_config = load_yaml('maurice_arm', 'config/moveit.yaml')
    
    # Create the arm node (C++ - includes arm + head servo 7)
    maurice_arm_node = Node(
        package='maurice_arm',
        executable='arm',
        name='maurice_arm',
        parameters=[
            arm_config_file,
            {
                'robot_description': robot_description,
                'robot_description_semantic': robot_description_semantic,
            },
            moveit_config,
        ],
        output='screen'
    )

    return LaunchDescription([
        maurice_arm_node
    ])

