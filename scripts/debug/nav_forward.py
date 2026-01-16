#!/usr/bin/env python3
"""
Debug the navigate to position action
Navigate forward/backward from current position using /navigate_to_pose action.
Usage: nav_forward.py <meters> [behavior_tree]
  meters: positive = forward, negative = backward
"""

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from nav2_msgs.action import NavigateToPose
from geometry_msgs.msg import PoseStamped
from tf2_ros import Buffer, TransformListener
import math
import sys


class NavForward(Node):
    def __init__(self):
        super().__init__('nav_forward')
        
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.action_client = ActionClient(self, NavigateToPose, '/navigate_to_pose')
        
    def get_pose_ahead(self, distance, use_map_frame=False):
        """Calculate pose `distance` meters ahead of current position."""
        source_frame = 'map' if use_map_frame else 'odom'
        target_frame = 'base_link'
        
        # Wait for transform
        self.get_logger().info(f'Waiting for transform {source_frame} -> {target_frame}...')
        while True:
            rclpy.spin_once(self, timeout_sec=0.1)
            try:
                transform = self.tf_buffer.lookup_transform(source_frame, target_frame, rclpy.time.Time())
                break
            except Exception:
                continue
        
        pos = transform.transform.translation
        ori = transform.transform.rotation
        
        # Calculate yaw from quaternion
        siny_cosp = 2 * (ori.w * ori.z + ori.x * ori.y)
        cosy_cosp = 1 - 2 * (ori.y * ori.y + ori.z * ori.z)
        yaw = math.atan2(siny_cosp, cosy_cosp)
        
        # Calculate position ahead
        new_x = pos.x + math.cos(yaw) * distance
        new_y = pos.y + math.sin(yaw) * distance
        
        self.get_logger().info(f'Current: x={pos.x:.3f}, y={pos.y:.3f}, yaw={math.degrees(yaw):.1f}°')
        self.get_logger().info(f'Target:  x={new_x:.3f}, y={new_y:.3f}')
        
        return new_x, new_y, ori
        
    def navigate(self, distance, behavior_tree='mapfree'):
        """Send navigation goal."""
        # Determine frame based on behavior tree
        use_map_frame = behavior_tree == 'navigation'
        frame_id = 'map' if use_map_frame else 'odom'
        
        x, y, ori = self.get_pose_ahead(distance, use_map_frame=use_map_frame)
        
        # Wait for action server
        self.get_logger().info('Waiting for navigate_to_pose action server...')
        if not self.action_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error('Action server not available!')
            return False
            
        # Create goal
        goal = NavigateToPose.Goal()
        goal.pose = PoseStamped()
        goal.pose.header.frame_id = frame_id
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = x
        goal.pose.pose.position.y = y
        goal.pose.pose.position.z = 0.0
        goal.pose.pose.orientation.x = ori.x
        goal.pose.pose.orientation.y = ori.y
        goal.pose.pose.orientation.z = ori.z
        goal.pose.pose.orientation.w = ori.w
        goal.behavior_tree = behavior_tree
        
        self.get_logger().info(f'Sending goal with behavior_tree={behavior_tree}')
        
        # Send goal
        future = self.action_client.send_goal_async(
            goal, 
            feedback_callback=self.feedback_cb
        )
        rclpy.spin_until_future_complete(self, future)
        
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().error('Goal rejected!')
            return False
            
        self.get_logger().info('Goal accepted, waiting for result...')
        
        # Wait for result
        result_future = goal_handle.get_result_async()
        rclpy.spin_until_future_complete(self, result_future)
        
        result = result_future.result()
        self.get_logger().info(f'Navigation complete with status: {result.status}')
        return result.status == 4  # SUCCEEDED
        
    def feedback_cb(self, feedback_msg):
        feedback = feedback_msg.feedback
        pos = feedback.current_pose.pose.position
        self.get_logger().info(f'Current position: x={pos.x:.3f}, y={pos.y:.3f}')


def main():
    rclpy.init()
    
    if len(sys.argv) < 2:
        print("Usage: nav_forward.py <meters> [behavior_tree]")
        print("  meters: positive = forward, negative = backward")
        sys.exit(1)
    
    distance = float(sys.argv[1])
    behavior_tree = sys.argv[2] if len(sys.argv) > 2 else 'mapfree'
    
    node = NavForward()
    try:
        node.navigate(distance, behavior_tree=behavior_tree)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
