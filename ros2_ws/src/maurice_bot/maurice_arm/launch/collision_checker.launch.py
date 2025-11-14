#!/usr/bin/env python3

from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    """
    Launch the collision checker node for Maurice arm.
    
    This node performs high-speed collision checking using simplified collision geometries:
    - base_link: Box
    - link2: Cylinder
    - link3: Cylinder
    - link61: Box (gripper finger)
    - link62: Box (gripper finger)
    
    Checks for:
    - Self-collisions (8 pairs checked, ignoring adjacent links)
    - Ground collisions (all links except base_link)
    
    Publishes:
    - /mars/arm/collision (Bool): Collision status
    - /collision_shapes (MarkerArray): Visualization in RViz
    """
    
    collision_checker_node = Node(
        package='maurice_arm',
        executable='collision_checker',
        name='collision_checker',
        output='screen',
        parameters=[{
            'publish_markers': True,  # Enable visualization markers
        }],
        emulate_tty=True,
    )
    
    return LaunchDescription([
        collision_checker_node
    ])

