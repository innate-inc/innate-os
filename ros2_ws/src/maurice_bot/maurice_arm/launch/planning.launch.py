#!/usr/bin/env python3
"""
Planning-only launch file for Maurice Arm.
Starts only the MoveIt planning components:
  1. MoveIt move_group (motion planning server)
"""

import os
import yaml
from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import Command
from launch_ros.parameter_descriptions import ParameterValue
from ament_index_python.packages import get_package_share_directory


def load_yaml(package_name, file_path):
    """Load a yaml file from package share directory."""
    package_path = get_package_share_directory(package_name)
    absolute_file_path = os.path.join(package_path, file_path)
    
    try:
        with open(absolute_file_path, 'r') as file:
            return yaml.safe_load(file)
    except Exception as e:
        print(f"Error loading {absolute_file_path}: {e}")
        return {}


def generate_launch_description():
    # ========== Package Paths ==========
    maurice_arm_share = get_package_share_directory('maurice_arm')
    maurice_sim_share = get_package_share_directory('maurice_sim')
    
    # ========== Configuration Files ==========
    
    # Robot description (URDF)
    urdf_file = os.path.join(maurice_sim_share, 'urdf', 'maurice.urdf')
    with open(urdf_file, 'r') as f:
        robot_description_content = f.read()
    
    robot_description = {
        'robot_description': ParameterValue(
            Command(['cat ', urdf_file]), value_type=str
        )
    }
    
    # Semantic description (SRDF)
    srdf_file = os.path.join(maurice_sim_share, 'urdf', 'arm.srdf')
    robot_description_semantic = {
        'robot_description_semantic': ParameterValue(
            Command(['cat ', srdf_file]), value_type=str
        )
    }
    
    # Load kinematics config
    kin_path = os.path.join(maurice_arm_share, 'config', 'kinematics.yaml')
    kin_full = yaml.safe_load(open(kin_path, 'r'))
    kin_params = kin_full.get('ros__parameters', kin_full)
    
    # Load MoveIt config
    moveit_config = load_yaml('maurice_arm', 'config/moveit.yaml')
    
    # ========== MoveIt Planning Node ==========
    
    # MoveIt move_group node (motion planning server)
    move_group_node = Node(
        package='moveit_ros_move_group',
        executable='move_group',
        name='move_group',
        output='screen',
        parameters=[
            robot_description,
            robot_description_semantic,
            {
                'robot_description_kinematics': 
                    kin_params.get('robot_description_kinematics', {})
            },
            moveit_config,
            {'use_sim_time': False},
            {'publish_planning_scene': True},
            {'publish_geometry_updates': True},
            {'publish_state_updates': True},
            {'publish_transforms_updates': True},
        ],
    )
    
    return LaunchDescription([
        move_group_node,
    ])

