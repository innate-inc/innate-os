from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os

def generate_launch_description():
    # Get the package share directories
    pkg_dir = get_package_share_directory('maurice_bringup')
    sim_pkg_dir = get_package_share_directory('maurice_sim')
    
    # Read URDF file for robot_state_publisher
    urdf_file = os.path.join(sim_pkg_dir, 'urdf', 'maurice.urdf')
    with open(urdf_file, 'r') as f:
        robot_description = f.read()
    
    # Robot state publisher node (publishes TF tree from joint_states + URDF)
    robot_state_publisher_node = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        name='robot_state_publisher',
        output='screen',
        parameters=[{'robot_description': robot_description}]
    )
    
    # Include the bringup_core launch file
    bringup_core_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_dir, 'launch', 'bringup_core.launch.py')
        )
    )
    
    # Include the lidar launch file
    lidar_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_dir, 'launch', 'lidar.launch.py')
        )
    )
    
    return LaunchDescription([
        robot_state_publisher_node,
        bringup_core_launch,
        lidar_launch
    ])
