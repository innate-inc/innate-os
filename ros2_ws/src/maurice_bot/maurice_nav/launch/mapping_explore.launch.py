#!/usr/bin/env python3

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch.events import matches_action
from launch.actions import DeclareLaunchArgument, EmitEvent, LogInfo, RegisterEventHandler
from launch.substitutions import AndSubstitution, LaunchConfiguration, NotSubstitution
from launch.conditions import IfCondition
from launch_ros.actions import LifecycleNode
from launch_ros.event_handlers import OnStateTransition
from launch_ros.events.lifecycle import ChangeState
from lifecycle_msgs.msg import Transition

def generate_launch_description():
    # Get the package share directory for your package
    package_name = 'maurice_nav'
    share_dir = get_package_share_directory(package_name)

    use_lifecycle_manager = LaunchConfiguration("use_lifecycle_manager")
    autostart = LaunchConfiguration('autostart')
    use_sim_time = LaunchConfiguration('use_sim_time')

    slam_params_file = os.path.join(
        get_package_share_directory("maurice_nav"),
        'config',
        'mapping_params_init.yaml'
    )

     # Declare launch arguments.
    declare_autostart_cmd = DeclareLaunchArgument(
        'autostart',
        default_value='true',
        description='Automatically startup the slam_toolbox. Ignored when use_lifecycle_manager is true.'
    )

    declare_use_lifecycle_manager = DeclareLaunchArgument(
        'use_lifecycle_manager',
        default_value='false',
        description='Enable bond connection during node activation'
    )

    declare_use_sim_time_argument = DeclareLaunchArgument(
        'use_sim_time',
        default_value='true',
        description='Use simulation/Gazebo clock'
    )

    # Define the paths to your YAML files
    planner_params_file = os.path.join(share_dir, 'config', 'planner.yaml')
    controller_params_file = os.path.join(share_dir, 'config', 'controller.yaml')
    costmap_params_file = os.path.join(share_dir, 'config', 'costmap.yaml')
    bt_navigator_params_file = os.path.join(share_dir, 'config', 'bt_navigator.yaml')
    behavior_params_file = os.path.join(share_dir, 'config', 'behavior.yaml')
    smoother_params_file = os.path.join(share_dir, 'config', 'velocity_smoother.yaml')

    # Use the map file - construct path from environment variable or HOME
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'maurice-prod'))
    default_map_path = os.path.join(maurice_root, 'maps', 'home.yaml')
    
    # Declare launch arguments so that these paths can be overridden if needed
    map_arg = DeclareLaunchArgument(
        'map',
        default_value=default_map_path,
        description='Full path to the map file to load'
    )
    # Create the map server node
    map_server_node = Node(
        package='nav2_map_server',
        executable='map_server',
        name='map_server',
        output='screen',
        parameters=[{'yaml_filename': LaunchConfiguration('map')}]
    )
    
    # Create the planner node
    planner_node = Node(
        package='nav2_planner',
        executable='planner_server',
        name='planner_server',
        output='screen',
        parameters=[planner_params_file, costmap_params_file]
    )

    # Create the controller node
    controller_node = Node(
        package='nav2_controller',
        executable='controller_server',
        name='controller_server',
        output='screen',
        parameters=[controller_params_file, costmap_params_file],
        remappings=[('cmd_vel', 'cmd_vel_raw')]
    )
    
    # Create the velocity smoother node
    velocity_smoother_node = Node(
        package='nav2_velocity_smoother',
        executable='velocity_smoother',
        name='velocity_smoother',
        output='screen',
        parameters=[smoother_params_file],
        remappings=[('cmd_vel', '/cmd_vel_raw'),
                    ('cmd_vel_smoothed', '/cmd_vel')]
    )

    # Configure the asynchronous slam_toolbox node using the parameter file.
    start_async_slam_toolbox_node = LifecycleNode(
        parameters=[
            slam_params_file,
            {
                'use_lifecycle_manager': use_lifecycle_manager,
                'use_sim_time': use_sim_time
            }
        ],
        package='slam_toolbox',
        executable='async_slam_toolbox_node',
        name='slam_toolbox',
        namespace='',  # Added required namespace parameter.
        output='screen'
    )

     # Emit event to configure the node if autostart is enabled.
    configure_event = EmitEvent(
        event=ChangeState(
            lifecycle_node_matcher=matches_action(start_async_slam_toolbox_node),
            transition_id=Transition.TRANSITION_CONFIGURE
        ),
        condition=IfCondition(AndSubstitution(autostart, NotSubstitution(use_lifecycle_manager)))
    )

    # Register event handler to transition from configuring to activating.
    activate_event = RegisterEventHandler(
        OnStateTransition(
            target_lifecycle_node=start_async_slam_toolbox_node,
            start_state="configuring",
            goal_state="inactive",
            entities=[
                LogInfo(msg="[LifecycleLaunch] Slam_toolbox node is activating."),
                EmitEvent(event=ChangeState(
                    lifecycle_node_matcher=matches_action(start_async_slam_toolbox_node),
                    transition_id=Transition.TRANSITION_ACTIVATE
                ))
            ]
        ),
        condition=IfCondition(AndSubstitution(autostart, NotSubstitution(use_lifecycle_manager)))
    )

    # Create the lifecycle manager node to manage all nodes
    lifecycle_manager_node = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager',
        output='screen',
        parameters=[{
            'autostart': True,
            'node_names': [
                'map_server', 
                'planner_server', 
                'controller_server', 
                'bt_navigator', 
                'behavior_server',
                'velocity_smoother'
                ]
        }]
    )

    # Create the BT Navigator node
    # Override BT XML paths with package-relative paths
    nav_to_pose_bt_xml = os.path.join(share_dir, 'config', 'nav_to_pose.xml')
    nav_through_poses_bt_xml = os.path.join(share_dir, 'config', 'nav_through_poses.xml')
    
    bt_navigator_node = Node(
        package='nav2_bt_navigator',
        executable='bt_navigator',
        name='bt_navigator',
        output='screen',
        parameters=[
            bt_navigator_params_file,
            {'default_nav_to_pose_bt_xml': nav_to_pose_bt_xml},
            {'default_nav_through_poses_bt_xml': nav_through_poses_bt_xml}
        ]
    )
    
    # Create the behavior server node
    behavior_server_node = Node(
        package='nav2_behaviors',
        executable='behavior_server',
        name='behavior_server',
        output='screen',
        parameters=[behavior_params_file]
    )
    

    ld = LaunchDescription([
        map_arg,
        map_server_node,
        planner_node,
        controller_node,
        velocity_smoother_node,
        bt_navigator_node,
        behavior_server_node,
        lifecycle_manager_node
    ])

    ld.add_action(declare_autostart_cmd)
    ld.add_action(declare_use_lifecycle_manager)
    ld.add_action(declare_use_sim_time_argument)
    ld.add_action(start_async_slam_toolbox_node)
    ld.add_action(configure_event)
    ld.add_action(activate_event)

    return ld
