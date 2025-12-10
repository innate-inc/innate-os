#!/usr/bin/env python3

import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    # Get the innate-os root directory
    innate_os_root = os.environ.get('INNATE_OS_ROOT', os.path.expanduser('~/innate-os'))
    
    # Declare launch arguments
    svg_arg = DeclareLaunchArgument(
        'svg',
        default_value=os.path.join(innate_os_root, 'square.svg'),
        description='Path to SVG file to draw'
    )
    
    control_mode_arg = DeclareLaunchArgument(
        'control_mode',
        default_value='direct',
        choices=['trajectory', 'direct'],
        description='Control mode: trajectory (smooth) or direct (with force control)'
    )
    
    scale_arg = DeclareLaunchArgument(
        'scale',
        default_value='0.01',
        description='Scale factor for SVG coordinates'
    )
    
    skip_calibration_arg = DeclareLaunchArgument(
        'skip_calibration',
        default_value='false',
        description='Skip calibration and use default coordinate mapping'
    )
    
    calibration_file_arg = DeclareLaunchArgument(
        'calibration_file',
        default_value=os.path.join(innate_os_root, '.whiteboard_calibration.json'),
        description='Path to JSON file to save/load calibration corners'
    )
    
    pen_tip_offset_arg = DeclareLaunchArgument(
        'pen_tip_offset',
        default_value='0.0,0.0,0.0',
        description='Pen tip offset from ee_link in end effector frame (x,y,z in meters). '
                    'For pen pointing down, typically (0, 0, -pen_length). Default: 0,0,0'
    )
    
    def create_node(context, *args, **kwargs):
        node_args = [
            '--svg', LaunchConfiguration('svg').perform(context),
            '--control-mode', LaunchConfiguration('control_mode').perform(context),
            '--scale', LaunchConfiguration('scale').perform(context),
        ]
        
        skip_cal = LaunchConfiguration('skip_calibration').perform(context)
        if skip_cal and skip_cal.lower() == 'true':
            node_args.append('--skip-calibration')
        
        cal_file = LaunchConfiguration('calibration_file').perform(context)
        if cal_file:
            node_args.extend(['--calibration-file', cal_file])
        
        pen_offset = LaunchConfiguration('pen_tip_offset').perform(context)
        if pen_offset:
            node_args.extend(['--pen-tip-offset', pen_offset])
        
        return [Node(
            package='manipulation',
            executable='whiteboard_draw.py',
            name='whiteboard_draw',
            output='screen',
            arguments=node_args,
            emulate_tty=True,
        )]
    
    whiteboard_draw_node = OpaqueFunction(function=create_node)
    
    return LaunchDescription([
        svg_arg,
        control_mode_arg,
        scale_arg,
        skip_calibration_arg,
        calibration_file_arg,
        pen_tip_offset_arg,
        whiteboard_draw_node,
    ])
