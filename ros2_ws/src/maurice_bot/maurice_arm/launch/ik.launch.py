#!/usr/bin/env python3
import os
import yaml

from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import Command, FindExecutable
from launch_ros.substitutions import FindPackageShare
from launch_ros.parameter_descriptions import ParameterValue

def generate_launch_description():
    # ———————— 1) URDF & SRDF ————————
    sim_pkg = FindPackageShare('maurice_sim').find('maurice_sim')
    urdf_path = os.path.join(sim_pkg, 'urdf', 'maurice.urdf')
    srdf_path = os.path.join(sim_pkg, 'urdf', 'arm.srdf')

    robot_description = {
        'robot_description': ParameterValue(
            Command(['cat ', urdf_path]), value_type=str
        )
    }
    robot_description_semantic = {
        'robot_description_semantic': ParameterValue(
            Command(['cat ', srdf_path]), value_type=str
        )
    }

    # ———————— 2) Load & parse kinematics.yaml ————————
    arm_pkg = FindPackageShare('maurice_arm').find('maurice_arm')
    kin_path = os.path.join(arm_pkg, 'config', 'kinematics.yaml')
    kin_full = yaml.safe_load(open(kin_path, 'r'))

    # If you left a ros__parameters: wrapper in your src file, unwrap it here;
    # otherwise kin_full already is the map you want.
    kin_params = kin_full.get('ros__parameters', kin_full)

    # ———————— 3) Launch MoveIt! move_group ————————
    return LaunchDescription([
        Node(
            package='moveit_ros_move_group',
            executable='move_group',
            name='move_group',
            output='screen',
            parameters=[
                robot_description,
                robot_description_semantic,

                # <-- here's the magic: pass the "robot_description_kinematics" map
                {
                  'robot_description_kinematics':
                    kin_params['robot_description_kinematics']
                },

                {'use_sim_time': False},
            ],
        ),

        # KDL-based IK node
        Node(
            package='maurice_arm',
            executable='ik.py',
            name='kdl_ik_from_file',
            output='screen',
        ),
    ])
