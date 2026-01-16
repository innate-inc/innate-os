#!/usr/bin/env python3
"""
NavigateToPoseRouter

This ROS 2 node acts as a router/proxy for the NavigateToPose action.
It listens on /navigate_to_pose and forwards all requests to /internal_navigate_to_pose.
This allows for interception, logging, or future middleware functionality.
"""

import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, ActionClient, GoalResponse, CancelResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.qos import QoSProfile, QoSDurabilityPolicy, QoSReliabilityPolicy

from nav2_msgs.action import NavigateToPose
from std_msgs.msg import String


class NavigateToPoseRouter(Node):
    def __init__(self):
        super().__init__('navigate_to_pose_router')
        
        # Use separate mutually exclusive callback groups so server can call client
        self._server_callback_group = MutuallyExclusiveCallbackGroup()
        self._client_callback_group = MutuallyExclusiveCallbackGroup()
        
        # Track active goals for cancel forwarding
        self._goal_handle_map = {}  # Maps server goal handle to client goal handle
        
        # Track current navigation mode
        self._current_mode = 'mapfree'  # Default mode
        
        # QoS profile for persistent/latched topic
        latched_qos = QoSProfile(
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE
        )
        
        # Subscribe to current navigation mode
        self._mode_sub = self.create_subscription(
            String,
            '/nav/current_mode',
            self._mode_callback,
            latched_qos
        )
        
        # Publisher for current navigation frame
        self._current_frame_pub = self.create_publisher(
            String,
            '/nav/current_frame',
            latched_qos
        )
        
        # Publish initial frame at startup
        initial_frame = String()
        initial_frame.data = 'mapfree'
        self._current_frame_pub.publish(initial_frame)
        self.get_logger().info('Published initial current_frame: mapfree')
        
        # Create action client to forward requests to internal action
        self._action_client = ActionClient(
            self,
            NavigateToPose,
            '/internal_navigate_to_pose',
            callback_group=self._client_callback_group
        )
        
        # Create action server to receive requests
        self._action_server = ActionServer(
            self,
            NavigateToPose,
            '/navigate_to_pose',
            execute_callback=self._execute_callback,
            goal_callback=self._goal_callback,
            cancel_callback=self._cancel_callback,
            callback_group=self._server_callback_group
        )
        
        self.get_logger().info('NavigateToPoseRouter initialized')
        self.get_logger().info('  Listening on: /navigate_to_pose')
        self.get_logger().info('  Forwarding to: /internal_navigate_to_pose')

    def _mode_callback(self, msg):
        """Track the current navigation mode."""
        self._current_mode = msg.data
        self.get_logger().debug(f'Current mode updated: {self._current_mode}')

    def _goal_callback(self, goal_request):
        """Accept or reject incoming goal requests."""
        self.get_logger().info('Received navigate_to_pose goal request')
        return GoalResponse.ACCEPT

    def _cancel_callback(self, goal_handle):
        """Handle cancel requests by forwarding to internal action."""
        self.get_logger().info('Received cancel request')
        
        # Try to cancel the corresponding internal goal
        if goal_handle in self._goal_handle_map:
            client_goal_handle = self._goal_handle_map[goal_handle]
            if client_goal_handle is not None:
                self.get_logger().info('Forwarding cancel to internal action')
                client_goal_handle.cancel_goal_async()
        
        return CancelResponse.ACCEPT

    async def _execute_callback(self, goal_handle):
        """Execute callback that forwards the goal to internal action."""
        self.get_logger().info('Executing navigate_to_pose goal...')
        
        # Wait for the internal action server to be available
        if not self._action_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error('Internal navigate_to_pose action server not available!')
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result
        
        # Create the goal to forward
        goal_msg = NavigateToPose.Goal()
        goal_msg.pose = goal_handle.request.pose
        # Leave behavior_tree empty, publish frame to /nav/current_frame instead
        
        # Determine the frame to use
        requested_frame = goal_handle.request.behavior_tree
        if not requested_frame:
            # If no frame specified, use the current mode
            requested_frame = self._current_mode
        
        # If in mapping mode, reject the navigation request
        if requested_frame == 'mapping':
            self.get_logger().warn('Navigation rejected: currently in mapping mode')
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result
        
        # Publish the frame to the current_frame topic
        frame_msg = String()
        frame_msg.data = requested_frame
        self._current_frame_pub.publish(frame_msg)
        self.get_logger().info(f'Published current_frame: {requested_frame}')
        
        self.get_logger().info(
            f'Forwarding goal to /internal_navigate_to_pose: '
            f'position=({goal_msg.pose.pose.position.x:.2f}, '
            f'{goal_msg.pose.pose.position.y:.2f}, '
            f'{goal_msg.pose.pose.position.z:.2f})'
        )
        
        # Send goal to internal action server
        send_goal_future = self._action_client.send_goal_async(
            goal_msg,
            feedback_callback=lambda feedback: self._feedback_callback(goal_handle, feedback)
        )
        
        # Wait for goal acceptance
        client_goal_handle = await send_goal_future
        
        if not client_goal_handle.accepted:
            self.get_logger().warn('Internal goal was rejected')
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result
        
        self.get_logger().info('Internal goal accepted')
        
        # Store mapping for cancel handling
        self._goal_handle_map[goal_handle] = client_goal_handle
        
        # Wait for the result
        try:
            result_future = client_goal_handle.get_result_async()
            result_response = await result_future
            
            # Forward the result status
            if result_response.status == 4:  # SUCCEEDED
                self.get_logger().info('Internal goal succeeded')
                goal_handle.succeed()
            elif result_response.status == 5:  # CANCELED
                self.get_logger().info('Internal goal was canceled')
                goal_handle.canceled()
            else:  # ABORTED or other failure
                self.get_logger().warn(f'Internal goal failed with status: {result_response.status}')
                goal_handle.abort()
            
            return result_response.result
            
        except Exception as e:
            self.get_logger().error(f'Error waiting for internal result: {e}')
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result
        finally:
            # Clean up goal handle mapping
            if goal_handle in self._goal_handle_map:
                del self._goal_handle_map[goal_handle]

    def _feedback_callback(self, server_goal_handle, feedback_msg):
        """Forward feedback from internal action to the original client."""
        # feedback_msg is a NavigateToPose.Impl.FeedbackMessage
        self.get_logger().debug('Forwarding feedback')
        server_goal_handle.publish_feedback(feedback_msg.feedback)
