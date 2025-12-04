#!/usr/bin/env python3
"""
Spline Path Server - ROS2 node that serves map data and executes spline paths
with integrated camera/odom recording.

This server:
1. Subscribes to /map (OccupancyGrid) and robot pose
2. Serves map/pose data to remote clients via WebSocket
3. Receives spline paths from clients and executes them via Nav2 FollowPath action
4. Integrates with camera_odom_recorder for synchronized data collection
5. Transfers recorded data back to the client
6. Allows map selection via mode_manager integration

Run on robot, port forward to PC for remote viewer access.
"""

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import QoSProfile, QoSDurabilityPolicy, QoSReliabilityPolicy

import asyncio
import websockets
import json
import threading
import numpy as np
import base64
import zlib
import os
import time
import glob

from nav_msgs.msg import OccupancyGrid, Odometry, Path
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
from std_msgs.msg import String
from nav2_msgs.action import FollowPath
from std_srvs.srv import Trigger
from brain_messages.srv import ChangeMap
from action_msgs.msg import GoalStatus

import tf2_ros


class SplinePathServer(Node):
    def __init__(self):
        super().__init__('spline_path_server')
        
        # Parameters
        self.declare_parameter('websocket_port', 8770)
        self.declare_parameter('use_amcl_pose', True)
        self.declare_parameter('map_topic', '/map')
        self.declare_parameter('odom_topic', '/odom')
        self.declare_parameter('amcl_pose_topic', '/amcl_pose')
        self.declare_parameter('recording_data_dir', '~/innate-os/camera_odom_recordings')
        
        self.ws_port = self.get_parameter('websocket_port').value
        self.use_amcl_pose = self.get_parameter('use_amcl_pose').value
        self.map_topic = self.get_parameter('map_topic').value
        self.odom_topic = self.get_parameter('odom_topic').value
        self.amcl_pose_topic = self.get_parameter('amcl_pose_topic').value
        self.recording_data_dir = os.path.expanduser(
            self.get_parameter('recording_data_dir').value
        )
        
        # State
        self.last_map = None
        self.last_map_compressed = None
        self.last_map_metadata = None
        self.robot_pose = None
        self.odom_pose = None
        self.connected_clients = set()
        self.is_executing = False
        self.current_goal_handle = None
        self.path_execution_complete = asyncio.Event()
        self.path_execution_status = None
        
        # Map selection state
        self.available_maps = []
        self.current_map_name = ""
        
        # Current task context
        self.current_task_label = ""
        self.current_session_name = ""
        
        # TF2 for transforms
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        
        # QoS for map (transient local for latched behavior)
        map_qos = QoSProfile(
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE,
            depth=1
        )
        
        # Subscribers
        self.map_sub = self.create_subscription(
            OccupancyGrid,
            self.map_topic,
            self.map_callback,
            map_qos
        )
        
        self.odom_sub = self.create_subscription(
            Odometry,
            self.odom_topic,
            self.odom_callback,
            10
        )
        
        if self.use_amcl_pose:
            self.amcl_sub = self.create_subscription(
                PoseWithCovarianceStamped,
                self.amcl_pose_topic,
                self.amcl_callback,
                10
            )
        
        # Subscribe to available maps and current map from mode_manager
        self.available_maps_sub = self.create_subscription(
            String,
            '/nav/available_maps',
            self.available_maps_callback,
            10
        )
        
        self.current_map_sub = self.create_subscription(
            String,
            '/nav/current_map',
            self.current_map_callback,
            10
        )
        
        # FollowPath action client
        self.follow_path_client = ActionClient(self, FollowPath, '/follow_path')
        
        # Create a reentrant callback group for service clients
        # This allows service calls from any thread without blocking
        self.service_callback_group = ReentrantCallbackGroup()
        
        # Camera recorder service clients
        self.start_recording_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/start_recording',
            callback_group=self.service_callback_group
        )
        self.stop_recording_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/stop_recording',
            callback_group=self.service_callback_group
        )
        self.get_status_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/get_status',
            callback_group=self.service_callback_group
        )
        
        # Map change service client
        self.change_map_client = self.create_client(
            ChangeMap, '/nav/change_navigation_map',
            callback_group=self.service_callback_group
        )
        
        # Timer to broadcast robot pose to clients
        self.pose_timer = self.create_timer(0.1, self.broadcast_pose)  # 10 Hz
        
        # Timer to update robot pose from TF (more reliable than /amcl_pose)
        self.tf_pose_timer = self.create_timer(0.1, self.update_pose_from_tf)  # 10 Hz
        
        # Initialize websocket loop reference (will be set by the thread)
        self.ws_loop = None
        
        # Start websocket server in background thread
        self.ws_thread = threading.Thread(target=self.run_websocket_server, daemon=True)
        self.ws_thread.start()
        
        self.get_logger().info(f'Spline Path Server started on WebSocket port {self.ws_port}')
        self.get_logger().info('Port forward this port to your PC to use the viewer')
        self.get_logger().info('Integrated with camera_odom_recorder for data collection')
        self.get_logger().info('Map selection available via mode_manager')

    def available_maps_callback(self, msg: String):
        """Handle available maps update from mode_manager."""
        try:
            data = json.loads(msg.data)
            self.available_maps = data.get('available_maps', [])
            self.get_logger().debug(f'Available maps: {self.available_maps}')
            
            # Broadcast to clients
            asyncio.run_coroutine_threadsafe(
                self.broadcast_map_list(),
                self.ws_loop
            )
        except json.JSONDecodeError:
            self.get_logger().warn(f'Failed to parse available_maps: {msg.data}')

    def current_map_callback(self, msg: String):
        """Handle current map update from mode_manager."""
        if msg.data != self.current_map_name:
            self.current_map_name = msg.data
            self.get_logger().info(f'Current map: {self.current_map_name}')
            
            # Broadcast to clients
            asyncio.run_coroutine_threadsafe(
                self.broadcast_map_list(),
                self.ws_loop
            )

    async def broadcast_map_list(self):
        """Send available maps and current map to all connected clients."""
        if self.connected_clients:
            message = json.dumps({
                'type': 'map_list',
                'available_maps': self.available_maps,
                'current_map': self.current_map_name
            })
            
            disconnected = set()
            for ws in self.connected_clients:
                try:
                    await ws.send(message)
                except websockets.exceptions.ConnectionClosed:
                    disconnected.add(ws)
            
            self.connected_clients -= disconnected

    def map_callback(self, msg: OccupancyGrid):
        """Store and compress map data for efficient transmission."""
        self.last_map = msg
        
        # Extract metadata
        self.last_map_metadata = {
            'width': msg.info.width,
            'height': msg.info.height,
            'resolution': msg.info.resolution,
            'origin_x': msg.info.origin.position.x,
            'origin_y': msg.info.origin.position.y,
            'origin_theta': 0.0,
            'map_name': self.current_map_name,
        }
        
        # Convert map data to numpy array and compress
        map_data = np.array(msg.data, dtype=np.int8)
        
        # Compress with zlib
        compressed = zlib.compress(map_data.tobytes(), level=6)
        self.last_map_compressed = base64.b64encode(compressed).decode('ascii')
        
        self.get_logger().info(
            f'Received map: {msg.info.width}x{msg.info.height}, '
            f'compressed size: {len(self.last_map_compressed)} bytes'
        )
        
        # Broadcast map to all connected clients
        asyncio.run_coroutine_threadsafe(
            self.broadcast_map(),
            self.ws_loop
        )

    def odom_callback(self, msg: Odometry):
        """Update robot pose from odometry (fallback if no AMCL)."""
        # Always update odom pose as fallback
        self.odom_pose = {
            'x': msg.pose.pose.position.x,
            'y': msg.pose.pose.position.y,
            'theta': self.quaternion_to_yaw(msg.pose.pose.orientation),
            'frame': 'odom'
        }
        # Use odom if not using AMCL or if AMCL hasn't published yet
        if not self.use_amcl_pose or self.robot_pose is None:
            self.robot_pose = self.odom_pose

    def amcl_callback(self, msg: PoseWithCovarianceStamped):
        """Update robot pose from AMCL localization."""
        self.robot_pose = {
            'x': msg.pose.pose.position.x,
            'y': msg.pose.pose.position.y,
            'theta': self.quaternion_to_yaw(msg.pose.pose.orientation),
            'frame': 'map'
        }

    def update_pose_from_tf(self):
        """Update robot pose by looking up TF from map to base_link."""
        try:
            # Look up transform from map to base_link
            transform = self.tf_buffer.lookup_transform(
                'map',
                'base_link',
                rclpy.time.Time(),
                timeout=rclpy.duration.Duration(seconds=0.05)
            )
            
            # Extract position and orientation
            t = transform.transform.translation
            r = transform.transform.rotation
            
            new_pose = {
                'x': t.x,
                'y': t.y,
                'theta': self.quaternion_to_yaw(r),
                'frame': 'map'
            }
            
            # Log first successful TF lookup
            if self.robot_pose is None:
                self.get_logger().info(f'Got robot pose from TF: ({t.x:.2f}, {t.y:.2f})')
            
            self.robot_pose = new_pose
            
        except Exception as e:
            # TF not available yet, use odom pose if available
            if self.odom_pose:
                self.robot_pose = self.odom_pose

    def quaternion_to_yaw(self, q):
        """Convert quaternion to yaw angle."""
        import math
        siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
        cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
        return math.atan2(siny_cosp, cosy_cosp)

    def yaw_to_quaternion(self, yaw):
        """Convert yaw angle to quaternion (z, w components)."""
        import math
        return {
            'x': 0.0,
            'y': 0.0,
            'z': math.sin(yaw / 2.0),
            'w': math.cos(yaw / 2.0)
        }

    async def broadcast_map(self):
        """Send map to all connected clients."""
        if self.last_map_compressed and self.connected_clients:
            message = json.dumps({
                'type': 'map',
                'metadata': self.last_map_metadata,
                'data': self.last_map_compressed
            })
            
            disconnected = set()
            for ws in self.connected_clients:
                try:
                    await ws.send(message)
                except websockets.exceptions.ConnectionClosed:
                    disconnected.add(ws)
            
            self.connected_clients -= disconnected

    def broadcast_pose(self):
        """Timer callback to broadcast robot pose."""
        if self.robot_pose and self.connected_clients and self.ws_loop:
            message = json.dumps({
                'type': 'pose',
                'pose': self.robot_pose,
                'is_executing': self.is_executing
            })
            
            try:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_message(message),
                    self.ws_loop
                )
            except Exception as e:
                self.get_logger().warn(f'Failed to broadcast pose: {e}')

    async def _broadcast_message(self, message):
        """Send message to all connected clients."""
        disconnected = set()
        for ws in self.connected_clients:
            try:
                await ws.send(message)
            except websockets.exceptions.ConnectionClosed:
                disconnected.add(ws)
        
        self.connected_clients -= disconnected

    async def handle_client(self, websocket):
        """Handle a connected WebSocket client."""
        self.connected_clients.add(websocket)
        client_addr = websocket.remote_address
        self.get_logger().info(f'Client connected: {client_addr}')
        
        try:
            # Send available maps list
            if self.available_maps:
                await websocket.send(json.dumps({
                    'type': 'map_list',
                    'available_maps': self.available_maps,
                    'current_map': self.current_map_name
                }))
            
            # Send current map if available
            if self.last_map_compressed:
                await websocket.send(json.dumps({
                    'type': 'map',
                    'metadata': self.last_map_metadata,
                    'data': self.last_map_compressed
                }))
            
            # Handle incoming messages
            async for message in websocket:
                await self.handle_message(websocket, message)
                
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.connected_clients.discard(websocket)
            self.get_logger().info(f'Client disconnected: {client_addr}')

    async def handle_message(self, websocket, message):
        """Process incoming client messages."""
        try:
            data = json.loads(message)
            msg_type = data.get('type')
            
            if msg_type == 'execute_path':
                # Execute spline path with recording
                waypoints = data.get('waypoints', [])
                task_label = data.get('task_label', 'unlabeled')
                await self.execute_path_with_recording(websocket, waypoints, task_label)
                
            elif msg_type == 'stop':
                # Cancel current path execution
                await self.stop_execution(websocket)
                
            elif msg_type == 'request_map':
                # Client requesting map refresh
                if self.last_map_compressed:
                    await websocket.send(json.dumps({
                        'type': 'map',
                        'metadata': self.last_map_metadata,
                        'data': self.last_map_compressed
                    }))
                    
            elif msg_type == 'change_map':
                # Client requesting map change
                map_name = data.get('map_name', '')
                await self.change_map(websocket, map_name)
                
            elif msg_type == 'request_map_list':
                # Client requesting map list refresh
                await websocket.send(json.dumps({
                    'type': 'map_list',
                    'available_maps': self.available_maps,
                    'current_map': self.current_map_name
                }))
                    
        except json.JSONDecodeError:
            self.get_logger().error(f'Invalid JSON message: {message}')
        except Exception as e:
            self.get_logger().error(f'Error handling message: {e}')
            import traceback
            traceback.print_exc()

    async def change_map(self, websocket, map_name):
        """Change the current map via mode_manager service."""
        if not map_name:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': 'No map name provided'
            }))
            return
        
        if self.is_executing:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': 'Cannot change map while executing a path'
            }))
            return
        
        self.get_logger().info(f'Changing map to: {map_name}')
        
        await websocket.send(json.dumps({
            'type': 'status',
            'message': f'Changing map to {map_name}...',
            'is_executing': False
        }))
        
        # Clear old map data so we know when new one arrives
        old_map_name = self.current_map_name
        self.last_map = None
        self.last_map_compressed = None
        self.last_map_metadata = None
        
        try:
            # Call change map service
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._call_change_map_service(map_name)
            )
            
            if result and result.success:
                await websocket.send(json.dumps({
                    'type': 'status',
                    'message': f'Map changed, waiting for new map data...',
                    'is_executing': False
                }))
                
                # Wait for the new map to arrive (poll for up to 15 seconds)
                new_map_received = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._wait_for_new_map(map_name, timeout=15.0)
                )
                
                if new_map_received and self.last_map_compressed:
                    self.get_logger().info(f'New map loaded, sending to client')
                    # Explicitly send the new map to this client
                    await websocket.send(json.dumps({
                        'type': 'map',
                        'metadata': self.last_map_metadata,
                        'data': self.last_map_compressed
                    }))
                    await websocket.send(json.dumps({
                        'type': 'status',
                        'message': f'Successfully loaded map: {map_name}',
                        'is_executing': False
                    }))
                else:
                    self.get_logger().warn(f'Timeout waiting for new map data')
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'message': 'Map changed but new map data not received. Try refreshing.'
                    }))
            else:
                error_msg = result.message if result else 'Service unavailable'
                await websocket.send(json.dumps({
                    'type': 'error',
                    'message': f'Failed to change map: {error_msg}'
                }))
                
        except Exception as e:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': f'Error changing map: {str(e)}'
            }))

    def _call_change_map_service(self, map_name, timeout=30.0):
        """Call the change map service synchronously (thread-safe, no spin_once)."""
        if not self.change_map_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().error('Change map service not available')
            return None
        
        request = ChangeMap.Request()
        request.map_name = map_name
        
        future = self.change_map_client.call_async(request)
        
        # Wait for future without calling spin_once (main thread handles spinning)
        start_time = time.time()
        while not future.done():
            if time.time() - start_time > timeout:
                self.get_logger().error('Change map service call timed out')
                return None
            time.sleep(0.1)  # Just sleep - MultiThreadedExecutor processes callbacks
        
        return future.result()

    def _wait_for_new_map(self, expected_map_name, timeout=15.0):
        """Wait for a new map to be received after a map change (thread-safe)."""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            # Check if we received the new map
            # (MultiThreadedExecutor processes map_callback in main thread)
            if self.last_map_compressed is not None:
                # Verify it's the expected map (if current_map_name updated)
                if self.current_map_name == expected_map_name:
                    self.get_logger().info(f'New map received: {self.current_map_name}')
                    return True
            
            time.sleep(0.2)  # Just sleep - callbacks processed by executor
        
        return False

    def call_service_sync(self, client, timeout=10.0):
        """Call a Trigger service synchronously (thread-safe, no spin_once)."""
        if not client.wait_for_service(timeout_sec=2.0):
            self.get_logger().error(f'Service {client.srv_name} not available')
            return None
        
        request = Trigger.Request()
        future = client.call_async(request)
        
        # Wait for future without calling spin_once (main thread handles spinning)
        start_time = time.time()
        while not future.done():
            if time.time() - start_time > timeout:
                self.get_logger().error('Service call timed out')
                return None
            time.sleep(0.1)  # Just sleep - MultiThreadedExecutor processes callbacks
        
        return future.result()

    async def execute_path_with_recording(self, websocket, waypoints, task_label):
        """Execute a path with synchronized camera/odom recording."""
        if not waypoints:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': 'No waypoints provided'
            }))
            return
        
        if self.is_executing:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': 'Already executing a path'
            }))
            return
        
        self.current_task_label = task_label
        self.is_executing = True
        
        await websocket.send(json.dumps({
            'type': 'status',
            'message': f'Starting trajectory with task: {task_label}',
            'is_executing': True,
            'phase': 'starting'
        }))
        
        try:
            # Step 1: Start recording
            self.get_logger().info('Starting camera recording...')
            await websocket.send(json.dumps({
                'type': 'status',
                'message': 'Starting camera recording...',
                'is_executing': True,
                'phase': 'recording_start'
            }))
            
            # Call start recording service in thread
            start_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.call_service_sync(self.start_recording_client, timeout=35.0)
            )
            
            if not start_result or not start_result.success:
                error_msg = start_result.message if start_result else 'Service unavailable'
                raise Exception(f'Failed to start recording: {error_msg}')
            
            # Extract session name from response
            self.current_session_name = start_result.message.split(': ')[-1] if start_result.message else ''
            self.get_logger().info(f'Recording started: {self.current_session_name}')
            
            # Step 2: Wait 0.2 seconds
            await asyncio.sleep(0.2)
            
            # Step 3: Execute path
            await websocket.send(json.dumps({
                'type': 'status',
                'message': f'Executing trajectory ({len(waypoints)} waypoints)...',
                'is_executing': True,
                'phase': 'path_execution'
            }))
            
            # Wait for action server
            if not self.follow_path_client.wait_for_server(timeout_sec=2.0):
                raise Exception('FollowPath action server not available')
            
            # Build nav_msgs/Path
            path_msg = Path()
            path_msg.header.frame_id = 'map'
            path_msg.header.stamp = self.get_clock().now().to_msg()
            
            for wp in waypoints:
                pose = PoseStamped()
                pose.header.frame_id = 'map'
                pose.header.stamp = path_msg.header.stamp
                pose.pose.position.x = float(wp['x'])
                pose.pose.position.y = float(wp['y'])
                pose.pose.position.z = 0.0
                
                yaw = float(wp.get('theta', 0.0))
                q = self.yaw_to_quaternion(yaw)
                pose.pose.orientation.x = q['x']
                pose.pose.orientation.y = q['y']
                pose.pose.orientation.z = q['z']
                pose.pose.orientation.w = q['w']
                
                path_msg.poses.append(pose)
            
            # Create and send goal
            goal = FollowPath.Goal()
            goal.path = path_msg
            goal.controller_id = 'FollowPath'
            goal.goal_checker_id = 'goal_checker'
            
            self.get_logger().info(f'Executing path with {len(waypoints)} waypoints')
            
            # Reset completion event
            self.path_execution_complete = asyncio.Event()
            self.path_execution_status = None
            
            # Send goal
            goal_future = self.follow_path_client.send_goal_async(goal)
            
            # Wait for goal acceptance
            goal_handle = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._wait_for_goal_acceptance(goal_future)
            )
            
            if not goal_handle or not goal_handle.accepted:
                raise Exception('Path goal rejected by controller')
            
            self.current_goal_handle = goal_handle
            self.get_logger().info('Path goal accepted, waiting for completion...')
            
            # Wait for path execution to complete
            result_future = goal_handle.get_result_async()
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._wait_for_result(result_future)
            )
            
            self.current_goal_handle = None
            
            if result.status != GoalStatus.STATUS_SUCCEEDED:
                status_names = {
                    1: 'ACCEPTED', 2: 'EXECUTING', 3: 'CANCELING',
                    4: 'SUCCEEDED', 5: 'CANCELED', 6: 'ABORTED'
                }
                status_name = status_names.get(result.status, f'UNKNOWN({result.status})')
                self.get_logger().warn(f'Path execution ended with status: {status_name}')
                
                if result.status == 6:  # ABORTED
                    self.get_logger().error(
                        'Path was ABORTED. Common causes:\n'
                        '  - Robot not localized (set initial pose in RViz)\n'
                        '  - No TF from map to base_link\n'
                        '  - Path goes through obstacles\n'
                        '  - Path too far from robot current position'
                    )
            else:
                self.get_logger().info('Path execution completed successfully')
            
            # Step 4: Wait 0.2 seconds
            await asyncio.sleep(0.2)
            
            # Step 5: Stop recording
            await websocket.send(json.dumps({
                'type': 'status',
                'message': 'Stopping recording and saving data...',
                'is_executing': True,
                'phase': 'recording_stop'
            }))
            
            stop_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.call_service_sync(self.stop_recording_client)
            )
            
            if not stop_result or not stop_result.success:
                error_msg = stop_result.message if stop_result else 'Service unavailable'
                raise Exception(f'Failed to stop recording: {error_msg}')
            
            self.get_logger().info(f'Recording stopped: {stop_result.message}')
            
            # Step 6: Read and transfer recording to client
            await websocket.send(json.dumps({
                'type': 'status',
                'message': 'Transferring recording to client...',
                'is_executing': True,
                'phase': 'transfer'
            }))
            
            await self.transfer_recording(websocket, task_label)
            
            await websocket.send(json.dumps({
                'type': 'status',
                'message': 'Trajectory complete! Recording transferred.',
                'is_executing': False,
                'phase': 'complete'
            }))
            
        except Exception as e:
            self.get_logger().error(f'Error during path execution: {e}')
            import traceback
            traceback.print_exc()
            
            # Try to stop recording if it was started
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.call_service_sync(self.stop_recording_client)
                )
            except:
                pass
            
            await websocket.send(json.dumps({
                'type': 'error',
                'message': str(e),
                'is_executing': False
            }))
        
        finally:
            self.is_executing = False
            self.current_goal_handle = None

    def _wait_for_goal_acceptance(self, future, timeout=10.0):
        """Wait for goal to be accepted."""
        start_time = time.time()
        while not future.done():
            if time.time() - start_time > timeout:
                return None
            time.sleep(0.05)
            rclpy.spin_once(self, timeout_sec=0.01)
        return future.result()

    def _wait_for_result(self, future, timeout=300.0):
        """Wait for action result."""
        start_time = time.time()
        while not future.done():
            if time.time() - start_time > timeout:
                return None
            time.sleep(0.1)
            rclpy.spin_once(self, timeout_sec=0.01)
        return future.result()

    async def transfer_recording(self, websocket, task_label):
        """Read recording files and transfer to client."""
        # Find the latest session directory
        session_dirs = sorted(glob.glob(os.path.join(self.recording_data_dir, '*')))
        
        if not session_dirs:
            raise Exception('No recording sessions found')
        
        latest_session_dir = session_dirs[-1]
        session_name = os.path.basename(latest_session_dir)
        
        h5_path = os.path.join(latest_session_dir, 'recording.h5')
        metadata_path = os.path.join(latest_session_dir, 'metadata.json')
        
        if not os.path.exists(h5_path):
            raise Exception(f'Recording file not found: {h5_path}')
        
        self.get_logger().info(f'Transferring recording from {latest_session_dir}')
        
        # Read and update metadata
        metadata = {}
        if os.path.exists(metadata_path):
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
        
        # Add task label and other fields
        metadata['task_label'] = task_label
        metadata['action_label'] = 'spline_trajectory'
        metadata['source_session'] = session_name
        metadata['source_frames'] = f"0-{metadata.get('num_frames', 0)}"
        metadata['map_name'] = self.current_map_name
        
        # Read HDF5 file and encode as base64
        with open(h5_path, 'rb') as f:
            h5_data = f.read()
        
        # Compress and encode
        h5_compressed = zlib.compress(h5_data, level=6)
        h5_base64 = base64.b64encode(h5_compressed).decode('ascii')
        
        file_size_mb = len(h5_data) / (1024 * 1024)
        compressed_size_mb = len(h5_compressed) / (1024 * 1024)
        
        self.get_logger().info(
            f'Sending recording: {file_size_mb:.2f}MB -> {compressed_size_mb:.2f}MB compressed'
        )
        
        # Send to client
        await websocket.send(json.dumps({
            'type': 'recording',
            'session_name': session_name,
            'metadata': metadata,
            'h5_data': h5_base64,
            'file_size': len(h5_data),
            'compressed_size': len(h5_compressed)
        }))

    async def stop_execution(self, websocket):
        """Cancel current path execution."""
        if self.current_goal_handle:
            self.get_logger().info('Canceling path execution')
            self.current_goal_handle.cancel_goal_async()
            
            # Also stop recording
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.call_service_sync(self.stop_recording_client)
                )
            except:
                pass
            
            self.is_executing = False
            
            await websocket.send(json.dumps({
                'type': 'status',
                'message': 'Path execution canceled',
                'is_executing': False
            }))
        else:
            await websocket.send(json.dumps({
                'type': 'status',
                'message': 'No path currently executing',
                'is_executing': False
            }))

    def run_websocket_server(self):
        """Run the WebSocket server in a background thread."""
        self.ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.ws_loop)
        
        async def serve():
            async with websockets.serve(
                self.handle_client,
                '0.0.0.0',
                self.ws_port,
                max_size=100 * 1024 * 1024  # 100MB max message size for recordings
            ):
                await asyncio.Future()  # Run forever
        
        self.ws_loop.run_until_complete(serve())


def main(args=None):
    rclpy.init(args=args)
    node = SplinePathServer()
    
    # Use MultiThreadedExecutor to allow service callbacks to be processed
    # while other threads are waiting for service responses
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:
            pass  # Ignore if already shutdown


if __name__ == '__main__':
    main()
