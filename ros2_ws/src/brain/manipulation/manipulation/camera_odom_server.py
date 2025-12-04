#!/usr/bin/env python3
"""
Camera Odom Server - WebSocket server for remote control of camera_odom_recorder.

This server allows a remote client to:
1. Start/stop recordings
2. Get recorder status
3. Download completed recordings
4. Clean up recordings after transfer

Port-forward this to your PC and connect via the unified pipeline viewer.
"""

import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import ReentrantCallbackGroup
from std_srvs.srv import Trigger

import asyncio
import websockets
import json
import threading
import os
import base64
import zlib
import shutil
import time

# Import shared configuration
from manipulation.camera_odom_config import (
    DEFAULT_RECORDING_DIR,
    DEFAULT_WEBSOCKET_PORT,
    SERVICE_START_RECORDING,
    SERVICE_STOP_RECORDING,
    SERVICE_GET_STATUS,
    get_recording_dir,
    get_session_path,
    get_h5_path,
    get_metadata_path,
    list_available_sessions,
    load_session_metadata,
)


class CameraOdomServer(Node):
    def __init__(self):
        super().__init__('camera_odom_server')
        
        # Parameters (using shared defaults)
        self.declare_parameter('websocket_port', DEFAULT_WEBSOCKET_PORT)
        self.declare_parameter('recording_data_dir', DEFAULT_RECORDING_DIR)
        
        self.ws_port = self.get_parameter('websocket_port').value
        self.recording_data_dir = get_recording_dir(
            self.get_parameter('recording_data_dir').value
        )
        
        # Ensure directory exists
        os.makedirs(self.recording_data_dir, exist_ok=True)
        
        # State
        self.connected_clients = set()
        self.is_recording = False
        self.current_session = None
        
        # Lock to prevent concurrent start/stop operations
        self.operation_lock = threading.Lock()
        self.operation_in_progress = False
        
        # Create a reentrant callback group for service clients
        # This allows service calls from any thread
        self.service_callback_group = ReentrantCallbackGroup()
        
        # Service clients for camera_odom_recorder (using shared service names)
        self.start_recording_client = self.create_client(
            Trigger, SERVICE_START_RECORDING,
            callback_group=self.service_callback_group
        )
        self.stop_recording_client = self.create_client(
            Trigger, SERVICE_STOP_RECORDING,
            callback_group=self.service_callback_group
        )
        self.get_status_client = self.create_client(
            Trigger, SERVICE_GET_STATUS,
            callback_group=self.service_callback_group
        )
        
        # WebSocket loop reference
        self.ws_loop = None
        
        # Start websocket server in background thread
        self.ws_thread = threading.Thread(target=self.run_websocket_server, daemon=True)
        self.ws_thread.start()
        
        # Status broadcast timer (5 Hz)
        self.status_timer = self.create_timer(0.2, self.broadcast_status)
        
        self.get_logger().info(f'Camera Odom Server started on WebSocket port {self.ws_port}')
        self.get_logger().info('Waiting for camera_odom_recorder services...')
    
    def call_service_sync(self, client, timeout=30.0):
        """Call a Trigger service synchronously (thread-safe, no spin_once)."""
        if not client.wait_for_service(timeout_sec=2.0):
            self.get_logger().error(f'Service {client.srv_name} not available')
            return None
        
        request = Trigger.Request()
        future = client.call_async(request)
        
        # Wait for future without calling spin_once (main thread handles spinning)
        # This is safe to call from executor threads
        start_time = time.time()
        while not future.done():
            if time.time() - start_time > timeout:
                self.get_logger().error('Service call timed out')
                return None
            time.sleep(0.1)  # Just sleep and wait - main spin loop processes callbacks
        
        return future.result()
    
    def get_recorder_status(self, timeout: float = 5.0) -> dict:
        """Get current recorder status."""
        try:
            result = self.call_service_sync(self.get_status_client, timeout=timeout)
            if result and result.success:
                status = json.loads(result.message)
                self.is_recording = status.get('state') == 'RECORDING'
                return status
        except Exception as e:
            self.get_logger().warn(f'Failed to get status: {e}')
        
        return {
            'state': 'UNKNOWN',
            'session_name': '',
            'frame_count': 0,
            'all_topics_received': False
        }
    
    def broadcast_status(self):
        """Broadcast recorder status to all connected clients."""
        if not self.connected_clients or not self.ws_loop:
            return
        
        # Don't poll status while an operation is in progress (avoid blocking)
        if self.operation_in_progress:
            return
        
        status = self.get_recorder_status()
        message = json.dumps({
            'type': 'status',
            'recording': status.get('state') == 'RECORDING',
            'session_name': status.get('session_name', ''),
            'frame_count': status.get('frame_count', 0),
            'topics_ready': status.get('all_topics_received', False),
            'recorder_available': status.get('state') != 'UNKNOWN',
            'operation_in_progress': self.operation_in_progress
        })
        
        try:
            asyncio.run_coroutine_threadsafe(
                self._broadcast_message(message),
                self.ws_loop
            )
        except Exception:
            pass
    
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
        
        # Send initial status
        status = self.get_recorder_status()
        await websocket.send(json.dumps({
            'type': 'status',
            'recording': status.get('state') == 'RECORDING',
            'session_name': status.get('session_name', ''),
            'frame_count': status.get('frame_count', 0),
            'topics_ready': status.get('all_topics_received', False),
            'recorder_available': status.get('state') != 'UNKNOWN',
            'operation_in_progress': self.operation_in_progress
        }))
        
        # Send list of available sessions
        await self.send_session_list(websocket)
        
        try:
            async for message in websocket:
                await self.handle_message(websocket, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.connected_clients.discard(websocket)
            self.get_logger().info(f'Client disconnected: {client_addr}')
    
    async def send_session_list(self, websocket):
        """Send list of available recording sessions."""
        sessions = list_available_sessions(self.recording_data_dir)
        await websocket.send(json.dumps({
            'type': 'session_list',
            'sessions': sessions
        }))
    
    async def handle_message(self, websocket, message):
        """Process incoming client messages."""
        try:
            data = json.loads(message)
            msg_type = data.get('type')
            
            if msg_type == 'start_recording':
                await self.handle_start_recording(websocket)
            
            elif msg_type == 'stop_recording':
                await self.handle_stop_recording(websocket)
            
            elif msg_type == 'get_status':
                status = self.get_recorder_status()
                await websocket.send(json.dumps({
                    'type': 'status',
                    'recording': status.get('state') == 'RECORDING',
                    'session_name': status.get('session_name', ''),
                    'frame_count': status.get('frame_count', 0),
                    'topics_ready': status.get('all_topics_received', False),
                    'recorder_available': status.get('state') != 'UNKNOWN',
                    'operation_in_progress': self.operation_in_progress
                }))
            
            elif msg_type == 'list_sessions':
                await self.send_session_list(websocket)
            
            elif msg_type == 'download_session':
                session_name = data.get('session_name')
                await self.handle_download_session(websocket, session_name)
            
            elif msg_type == 'delete_session':
                session_name = data.get('session_name')
                await self.handle_delete_session(websocket, session_name)
            
            elif msg_type == 'download_all':
                await self.handle_download_all(websocket)
                
        except json.JSONDecodeError:
            self.get_logger().error(f'Invalid JSON: {message}')
        except Exception as e:
            self.get_logger().error(f'Error handling message: {e}')
            import traceback
            traceback.print_exc()
    
    async def handle_start_recording(self, websocket):
        """Start a new recording session with proper state handling."""
        # Check if an operation is already in progress
        with self.operation_lock:
            if self.operation_in_progress:
                await websocket.send(json.dumps({
                    'type': 'recording_started',
                    'success': False,
                    'message': 'Another operation is in progress. Please wait.'
                }))
                return
            self.operation_in_progress = True
        
        try:
            self.get_logger().info('Starting recording...')
            
            # First, check current status (use longer timeout during operations)
            await websocket.send(json.dumps({
                'type': 'info',
                'message': 'Checking recorder status...'
            }))
            
            status = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.get_recorder_status(timeout=10.0)
            )
            
            # If already recording, stop the existing recording first
            if status.get('state') == 'RECORDING':
                self.get_logger().info('Existing recording found, stopping it first...')
                await websocket.send(json.dumps({
                    'type': 'info',
                    'message': 'Stopping existing recording first...'
                }))
                
                stop_result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.call_service_sync(self.stop_recording_client, timeout=30.0)
                )
                
                if stop_result and stop_result.success:
                    self.get_logger().info(f'Stopped existing recording: {stop_result.message}')
                    await websocket.send(json.dumps({
                        'type': 'info',
                        'message': f'Stopped existing recording. Starting new one...'
                    }))
                else:
                    error_msg = stop_result.message if stop_result else 'Failed to stop'
                    self.get_logger().error(f'Failed to stop existing recording: {error_msg}')
                    await websocket.send(json.dumps({
                        'type': 'recording_started',
                        'success': False,
                        'message': f'Failed to stop existing recording: {error_msg}'
                    }))
                    return
            
            await websocket.send(json.dumps({
                'type': 'info',
                'message': 'Starting recording (calibrating + initializing)...'
            }))
            
            # Call the service (this takes time: ~5s calibration + 5s odom wait + buffer)
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.call_service_sync(self.start_recording_client, timeout=45.0)
            )
            
            if result and result.success:
                self.is_recording = True
                session_name = result.message.split(': ')[-1] if result.message else ''
                self.current_session = session_name
                
                await websocket.send(json.dumps({
                    'type': 'recording_started',
                    'success': True,
                    'session_name': session_name,
                    'message': result.message
                }))
                self.get_logger().info(f'Recording started: {session_name}')
            else:
                error_msg = result.message if result else 'Service unavailable'
                await websocket.send(json.dumps({
                    'type': 'recording_started',
                    'success': False,
                    'message': f'Failed to start: {error_msg}'
                }))
                self.get_logger().error(f'Failed to start recording: {error_msg}')
        
        finally:
            with self.operation_lock:
                self.operation_in_progress = False
    
    async def handle_stop_recording(self, websocket):
        """Stop the current recording session with proper state handling."""
        # Check if an operation is already in progress
        with self.operation_lock:
            if self.operation_in_progress:
                await websocket.send(json.dumps({
                    'type': 'recording_stopped',
                    'success': False,
                    'message': 'Another operation is in progress. Please wait.'
                }))
                return
            self.operation_in_progress = True
        
        try:
            self.get_logger().info('Stopping recording...')
            
            # First check if actually recording (use longer timeout during operations)
            status = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.get_recorder_status(timeout=10.0)
            )
            
            if status.get('state') != 'RECORDING':
                await websocket.send(json.dumps({
                    'type': 'recording_stopped',
                    'success': False,
                    'message': 'No active recording to stop.'
                }))
                self.get_logger().warn('Stop requested but no active recording')
                return
            
            await websocket.send(json.dumps({
                'type': 'info',
                'message': 'Stopping recording and saving...'
            }))
            
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.call_service_sync(self.stop_recording_client, timeout=30.0)
            )
            
            if result and result.success:
                self.is_recording = False
                
                await websocket.send(json.dumps({
                    'type': 'recording_stopped',
                    'success': True,
                    'message': result.message
                }))
                
                # Send updated session list
                await self.send_session_list(websocket)
                
                self.get_logger().info(f'Recording stopped: {result.message}')
            else:
                error_msg = result.message if result else 'Service unavailable'
                await websocket.send(json.dumps({
                    'type': 'recording_stopped',
                    'success': False,
                    'message': f'Failed to stop: {error_msg}'
                }))
                self.get_logger().error(f'Failed to stop recording: {error_msg}')
        
        finally:
            with self.operation_lock:
                self.operation_in_progress = False
    
    async def handle_download_session(self, websocket, session_name: str):
        """Download a recording session to the client."""
        if not session_name:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': 'No session name provided'
            }))
            return
        
        session_path = get_session_path(self.recording_data_dir, session_name)
        h5_path = get_h5_path(session_path)
        
        if not os.path.exists(h5_path):
            await websocket.send(json.dumps({
                'type': 'error',
                'message': f'Session not found: {session_name}'
            }))
            return
        
        self.get_logger().info(f'Downloading session: {session_name}')
        
        await websocket.send(json.dumps({
            'type': 'info',
            'message': f'Preparing download: {session_name}...'
        }))
        
        # Read metadata using shared helper
        metadata = load_session_metadata(session_path)
        
        # Read and compress H5 file
        with open(h5_path, 'rb') as f:
            h5_data = f.read()
        
        h5_compressed = zlib.compress(h5_data, level=6)
        h5_base64 = base64.b64encode(h5_compressed).decode('ascii')
        
        file_size_mb = len(h5_data) / (1024 * 1024)
        compressed_size_mb = len(h5_compressed) / (1024 * 1024)
        
        self.get_logger().info(
            f'Sending {session_name}: {file_size_mb:.2f}MB -> {compressed_size_mb:.2f}MB'
        )
        
        await websocket.send(json.dumps({
            'type': 'session_download',
            'session_name': session_name,
            'metadata': metadata,
            'h5_data': h5_base64,
            'file_size': len(h5_data),
            'compressed_size': len(h5_compressed)
        }))
    
    async def handle_delete_session(self, websocket, session_name: str):
        """Delete a recording session from disk."""
        if not session_name:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': 'No session name provided'
            }))
            return
        
        session_path = get_session_path(self.recording_data_dir, session_name)
        
        if not os.path.exists(session_path):
            await websocket.send(json.dumps({
                'type': 'error',
                'message': f'Session not found: {session_name}'
            }))
            return
        
        try:
            shutil.rmtree(session_path)
            self.get_logger().info(f'Deleted session: {session_name}')
            
            await websocket.send(json.dumps({
                'type': 'session_deleted',
                'success': True,
                'session_name': session_name
            }))
            
            # Send updated session list
            await self.send_session_list(websocket)
            
        except Exception as e:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': f'Failed to delete: {str(e)}'
            }))
    
    async def handle_download_all(self, websocket):
        """Download all sessions and optionally delete them."""
        sessions = list_available_sessions(self.recording_data_dir)
        
        if not sessions:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': 'No sessions available'
            }))
            return
        
        await websocket.send(json.dumps({
            'type': 'info',
            'message': f'Downloading {len(sessions)} sessions...'
        }))
        
        for session_info in sessions:
            await self.handle_download_session(websocket, session_info['name'])
        
        await websocket.send(json.dumps({
            'type': 'download_all_complete',
            'count': len(sessions)
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
                max_size=200 * 1024 * 1024  # 200MB max message size
            ):
                await asyncio.Future()  # Run forever
        
        self.ws_loop.run_until_complete(serve())


def main(args=None):
    rclpy.init(args=args)
    node = CameraOdomServer()
    
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

