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

Run on robot, port forward to PC for remote viewer access.
"""

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
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
from nav2_msgs.action import FollowPath
from std_srvs.srv import Trigger
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
        self.connected_clients = set()
        self.is_executing = False
        self.current_goal_handle = None
        self.path_execution_complete = asyncio.Event()
        self.path_execution_status = None
        
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
        
        # FollowPath action client
        self.follow_path_client = ActionClient(self, FollowPath, '/follow_path')
        
        # Camera recorder service clients
        self.start_recording_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/start_recording'
        )
        self.stop_recording_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/stop_recording'
        )
        self.get_status_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/get_status'
        )
        
        # Timer to broadcast robot pose to clients
        self.pose_timer = self.create_timer(0.1, self.broadcast_pose)  # 10 Hz
        
        # Start websocket server in background thread
        self.ws_thread = threading.Thread(target=self.run_websocket_server, daemon=True)
        self.ws_thread.start()
        
        self.get_logger().info(f'Spline Path Server started on WebSocket port {self.ws_port}')
        self.get_logger().info('Port forward this port to your PC to use the viewer')
        self.get_logger().info('Integrated with camera_odom_recorder for data collection')

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
        if not self.use_amcl_pose:
            self.robot_pose = {
                'x': msg.pose.pose.position.x,
                'y': msg.pose.pose.position.y,
                'theta': self.quaternion_to_yaw(msg.pose.pose.orientation),
                'frame': 'odom'
            }

    def amcl_callback(self, msg: PoseWithCovarianceStamped):
        """Update robot pose from AMCL localization."""
        self.robot_pose = {
            'x': msg.pose.pose.position.x,
            'y': msg.pose.pose.position.y,
            'theta': self.quaternion_to_yaw(msg.pose.pose.orientation),
            'frame': 'map'
        }

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
        if self.robot_pose and self.connected_clients:
            message = json.dumps({
                'type': 'pose',
                'pose': self.robot_pose,
                'is_executing': self.is_executing
            })
            
            asyncio.run_coroutine_threadsafe(
                self._broadcast_message(message),
                self.ws_loop
            )

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
                    
        except json.JSONDecodeError:
            self.get_logger().error(f'Invalid JSON message: {message}')
        except Exception as e:
            self.get_logger().error(f'Error handling message: {e}')
            import traceback
            traceback.print_exc()

    def call_service_sync(self, client, timeout=10.0):
        """Call a Trigger service synchronously."""
        if not client.wait_for_service(timeout_sec=2.0):
            self.get_logger().error(f'Service {client.srv_name} not available')
            return None
        
        request = Trigger.Request()
        future = client.call_async(request)
        
        # Wait for result
        start_time = time.time()
        while not future.done():
            if time.time() - start_time > timeout:
                self.get_logger().error('Service call timed out')
                return None
            time.sleep(0.05)
            rclpy.spin_once(self, timeout_sec=0.01)
        
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
                self.get_logger().warn(f'Path execution ended with status: {result.status}')
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
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
