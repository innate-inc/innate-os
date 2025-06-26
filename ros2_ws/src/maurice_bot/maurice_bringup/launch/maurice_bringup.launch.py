from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from ament_index_python.packages import get_package_share_directory
import os

def generate_launch_description():
    # Get the package share directory
    pkg_dir = get_package_share_directory('maurice_bringup')
    head_pkg_dir = get_package_share_directory('maurice_head')
    
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
    camera_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_dir, 'launch', 'camera.launch.py')
        )
    )
    head_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(head_pkg_dir, 'launch', 'head.launch.py')
        )
    )
    
    return LaunchDescription([
        bringup_core_launch,
        lidar_launch,
        camera_launch,
        head_launch
    ])
