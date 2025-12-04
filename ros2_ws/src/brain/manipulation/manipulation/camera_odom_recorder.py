#!/usr/bin/env python3
"""
Camera + Odometry + Head Position Recorder Node

Records compressed camera frames, odometry, and head position data
directly to HDF5 files using streaming (no in-memory buffering).
"""

import time
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from std_srvs.srv import Trigger
from sensor_msgs.msg import CompressedImage, CameraInfo
from nav_msgs.msg import Odometry
from std_msgs.msg import String
import numpy as np
import h5py
import os
import json

# Import shared configuration
from manipulation.camera_odom_config import (
    DEFAULT_RECORDING_DIR,
    DEFAULT_SESSION_PREFIX,
    DEFAULT_DATA_FREQUENCY,
    DEFAULT_CHUNK_SIZE,
    H5_FILENAME,
    METADATA_FILENAME,
    get_recording_dir,
    get_h5_path,
    get_metadata_path,
)


class HDF5StreamWriter:
    """
    Handles streaming writes to an HDF5 file with resizable datasets.
    Data is written directly to disk, avoiding memory accumulation.
    """
    
    def __init__(self, filepath: str, camera_names: list, chunk_size: int = 100,
                 record_head_position: bool = False):
        """
        Initialize the HDF5 stream writer.
        
        Args:
            filepath: Path to the HDF5 file
            camera_names: List of camera names (sanitized topic names)
            chunk_size: Chunk size for HDF5 datasets (affects I/O performance)
            record_head_position: Whether to record head position data
        """
        self.filepath = filepath
        self.camera_names = camera_names
        self.chunk_size = chunk_size
        self.record_head_position = record_head_position
        self.h5file = None
        self.frame_count = 0
        
        # Dataset references
        self.image_datasets = {}  # cam_name -> dataset
        self.image_ts_datasets = {}  # cam_name -> timestamp dataset
        self.odom_datasets = {}  # field_name -> dataset
        self.odom_ts_dataset = None
        self.head_position_dataset = None
        self.head_position_ts_dataset = None
    
    def open(self):
        """Open the HDF5 file and create initial structure."""
        self.h5file = h5py.File(self.filepath, 'w')
        
        # Create groups
        self.h5file.create_group('/metadata')
        self.h5file.create_group('/camera_info')
        self.h5file.create_group('/images')
        self.h5file.create_group('/timestamps')
        self.h5file.create_group('/timestamps/images')
        self.h5file.create_group('/odometry')
        
        # Create variable-length dtype for compressed images
        vlen_dtype = h5py.vlen_dtype(np.dtype('uint8'))
        
        # Create resizable datasets for each camera
        for cam_name in self.camera_names:
            cam_grp = self.h5file.create_group(f'/images/{cam_name}')
            
            # Variable-length dataset for compressed image bytes
            self.image_datasets[cam_name] = cam_grp.create_dataset(
                'data',
                shape=(0,),
                maxshape=(None,),
                dtype=vlen_dtype,
                chunks=(self.chunk_size,)
            )
            self.image_datasets[cam_name].attrs['format'] = 'jpeg'
            
            # Timestamps for images
            self.image_ts_datasets[cam_name] = self.h5file.create_dataset(
                f'/timestamps/images/{cam_name}',
                shape=(0,),
                maxshape=(None,),
                dtype=np.float64,
                chunks=(self.chunk_size,)
            )
        
        # Create resizable datasets for odometry
        odom_fields = {
            'position': (3,),
            'orientation': (4,),
            'linear_velocity': (3,),
            'angular_velocity': (3,),
            'pose_covariance': (36,),
            'twist_covariance': (36,)
        }
        
        for field_name, field_shape in odom_fields.items():
            self.odom_datasets[field_name] = self.h5file.create_dataset(
                f'/odometry/{field_name}',
                shape=(0,) + field_shape,
                maxshape=(None,) + field_shape,
                dtype=np.float64,
                chunks=(self.chunk_size,) + field_shape
            )
        
        # Odometry timestamps
        self.odom_ts_dataset = self.h5file.create_dataset(
            '/timestamps/odometry',
            shape=(0,),
            maxshape=(None,),
            dtype=np.float64,
            chunks=(self.chunk_size,)
        )
        
        # Head position dataset (if enabled)
        if self.record_head_position:
            self.h5file.create_group('/head')
            self.head_position_dataset = self.h5file.create_dataset(
                '/head/position',
                shape=(0,),
                maxshape=(None,),
                dtype=np.float64,
                chunks=(self.chunk_size,)
            )
            self.head_position_ts_dataset = self.h5file.create_dataset(
                '/timestamps/head',
                shape=(0,),
                maxshape=(None,),
                dtype=np.float64,
                chunks=(self.chunk_size,)
            )
        
        self.frame_count = 0
    
    def write_frame(self, images: dict, image_timestamps: dict, 
                    odom_data: dict, odom_timestamp: float,
                    head_position: float = None, head_timestamp: float = None):
        """
        Write a single frame of data directly to disk.
        
        Args:
            images: Dict of cam_name -> compressed image bytes (numpy array)
            image_timestamps: Dict of cam_name -> timestamp (float)
            odom_data: Dict with odometry fields
            odom_timestamp: Odometry timestamp (float)
            head_position: Head position angle (float, optional)
            head_timestamp: Head position timestamp (float, optional)
        """
        if self.h5file is None:
            raise RuntimeError("HDF5 file not open. Call open() first.")
        
        idx = self.frame_count
        
        # Write image data for each camera
        for cam_name in self.camera_names:
            if cam_name in images:
                img_data = images[cam_name]
                
                # Resize dataset to accommodate new frame
                self.image_datasets[cam_name].resize((idx + 1,))
                self.image_datasets[cam_name][idx] = img_data
                
                # Write timestamp
                self.image_ts_datasets[cam_name].resize((idx + 1,))
                self.image_ts_datasets[cam_name][idx] = image_timestamps.get(cam_name, 0.0)
        
        # Write odometry data
        for field_name, dataset in self.odom_datasets.items():
            current_shape = dataset.shape
            dataset.resize((idx + 1,) + current_shape[1:])
            dataset[idx] = odom_data[field_name]
        
        # Write odometry timestamp
        self.odom_ts_dataset.resize((idx + 1,))
        self.odom_ts_dataset[idx] = odom_timestamp
        
        # Write head position if available
        if self.record_head_position and head_position is not None:
            self.head_position_dataset.resize((idx + 1,))
            self.head_position_dataset[idx] = head_position
            self.head_position_ts_dataset.resize((idx + 1,))
            self.head_position_ts_dataset[idx] = head_timestamp if head_timestamp else odom_timestamp
        
        self.frame_count += 1
        
        # Flush periodically to ensure data is written to disk
        if self.frame_count % self.chunk_size == 0:
            self.h5file.flush()
    
    def write_camera_info(self, camera_info_data: dict):
        """
        Write camera intrinsics/extrinsics.
        
        Args:
            camera_info_data: Dict of topic -> CameraInfo message
        """
        if self.h5file is None or not camera_info_data:
            return
        
        cam_info_grp = self.h5file['/camera_info']
        
        for topic, cam_info in camera_info_data.items():
            cam_name = topic.replace('/', '_').strip('_')
            
            if cam_name in cam_info_grp:
                continue  # Already written
            
            cam_grp = cam_info_grp.create_group(cam_name)
            
            # Intrinsics
            cam_grp.attrs['width'] = cam_info.width
            cam_grp.attrs['height'] = cam_info.height
            cam_grp.attrs['distortion_model'] = cam_info.distortion_model
            cam_grp.create_dataset('K', data=np.array(cam_info.k).reshape(3, 3))
            cam_grp.create_dataset('D', data=np.array(cam_info.d))
            cam_grp.create_dataset('R', data=np.array(cam_info.r).reshape(3, 3))
            cam_grp.create_dataset('P', data=np.array(cam_info.p).reshape(3, 4))
            
            # Frame info
            cam_grp.attrs['frame_id'] = cam_info.header.frame_id
    
    def write_metadata(self, metadata: dict):
        """Write session metadata."""
        if self.h5file is None:
            return
        
        metadata_grp = self.h5file['/metadata']
        for key, value in metadata.items():
            if isinstance(value, (str, int, float)):
                metadata_grp.attrs[key] = value
    
    def close(self):
        """Close the HDF5 file."""
        if self.h5file is not None:
            self.h5file.flush()
            self.h5file.close()
            self.h5file = None
    
    def get_frame_count(self) -> int:
        """Get the number of frames written."""
        return self.frame_count


class CameraOdomRecorderNode(Node):
    def __init__(self):
        super().__init__('camera_odom_recorder_node')
        
        # Declare parameters (using shared defaults from camera_odom_config)
        self.declare_parameter('data_directory', DEFAULT_RECORDING_DIR)
        self.declare_parameter('data_frequency', DEFAULT_DATA_FREQUENCY)
        self.declare_parameter('camera_topics', ['/camera/image/compressed'])
        self.declare_parameter('odom_topic', '/odom')
        self.declare_parameter('head_position_topic', '')  # Optional head position topic
        self.declare_parameter('session_name_prefix', DEFAULT_SESSION_PREFIX)
        self.declare_parameter('chunk_size', DEFAULT_CHUNK_SIZE)
        
        # Get parameter values
        self.data_directory = get_recording_dir(
            self.get_parameter('data_directory').value
        )
        self.data_frequency = self.get_parameter('data_frequency').value
        self.camera_topics = self.get_parameter('camera_topics').value
        self.camera_info_topics = []  # Not used - camera_info topics don't exist
        self.odom_topic = self.get_parameter('odom_topic').value
        self.head_position_topic = self.get_parameter('head_position_topic').value or ''
        self.session_name_prefix = self.get_parameter('session_name_prefix').value
        self.chunk_size = self.get_parameter('chunk_size').value
        
        # Create data directory if it doesn't exist
        os.makedirs(self.data_directory, exist_ok=True)
        
        # State management
        self.state = "IDLE"  # IDLE, RECORDING
        self.current_session_name = ""
        self.session_start_time = None
        self.session_dir = None
        
        # HDF5 stream writer (replaces in-memory buffers)
        self.stream_writer: HDF5StreamWriter = None
        
        # Camera info cache (written once per session)
        self.camera_info_cache = {}
        self.camera_info_written = False
        
        # Topic reception tracking
        self.topics_received = {}
        for topic in self.camera_topics:
            self.topics_received[topic] = False
        for topic in self.camera_info_topics:
            self.topics_received[topic] = False
        self.topics_received[self.odom_topic] = False
        if self.head_position_topic:
            self.topics_received[self.head_position_topic] = False
        self.all_topics_received = False
        
        # Latest data for synchronized recording
        self.latest_images = {topic: None for topic in self.camera_topics}
        self.latest_odom = None
        self.latest_head_position = None  # Will store parsed head position
        self.latest_head_timestamp = None
        
        # QoS profiles
        image_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        
        camera_info_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Subscribe to compressed image topics
        for topic in self.camera_topics:
            self.create_subscription(
                CompressedImage,
                topic,
                lambda msg, t=topic: self.image_callback(msg, t),
                image_qos
            )
            self.get_logger().info(f"Subscribing to compressed image topic: {topic}")
        
        # Subscribe to camera info topics (optional)
        for topic in self.camera_info_topics:
            self.create_subscription(
                CameraInfo,
                topic,
                lambda msg, t=topic: self.camera_info_callback(msg, t),
                camera_info_qos
            )
            self.get_logger().info(f"Subscribing to camera info topic: {topic}")
        
        # Subscribe to odometry
        self.create_subscription(
            Odometry,
            self.odom_topic,
            self.odom_callback,
            10
        )
        self.get_logger().info(f"Subscribing to odom topic: {self.odom_topic}")
        
        # Subscribe to head position (optional)
        if self.head_position_topic:
            self.create_subscription(
                String,
                self.head_position_topic,
                self.head_position_callback,
                10
            )
            self.get_logger().info(f"Subscribing to head position topic: {self.head_position_topic}")
        
        # Create calibration service client
        self.calibrate_client = self.create_client(Trigger, '/calibrate')
        self.get_logger().info("Created client for /calibrate service")
        
        # Create services
        self.start_recording_srv = self.create_service(
            Trigger,
            'brain/camera_odom_recorder/start_recording',
            self.handle_start_recording
        )
        self.stop_recording_srv = self.create_service(
            Trigger,
            'brain/camera_odom_recorder/stop_recording',
            self.handle_stop_recording
        )
        self.get_status_srv = self.create_service(
            Trigger,
            'brain/camera_odom_recorder/get_status',
            self.handle_get_status
        )
        
        self.get_logger().info("Hosting services:")
        self.get_logger().info("  brain/camera_odom_recorder/start_recording")
        self.get_logger().info("  brain/camera_odom_recorder/stop_recording")
        self.get_logger().info("  brain/camera_odom_recorder/get_status")
        
        # Status publisher
        self.status_pub = self.create_publisher(String, '/brain/camera_odom_recorder/status', 10)
        
        # Recording timer
        self.timer = self.create_timer(1.0 / self.data_frequency, self.timer_callback)
        
        self.get_logger().info("Camera + Odometry Recorder Node initialized in IDLE state.")
        self.get_logger().info("Using HDF5 disk streaming (no memory buffering).")
    
    def check_all_topics_received(self):
        """Check if all subscribed topics have received at least one message."""
        if not self.all_topics_received and all(self.topics_received.values()):
            self.all_topics_received = True
            self.get_logger().info("All topics have received at least one message.")
    
    def image_callback(self, msg: CompressedImage, topic: str):
        """Handle incoming compressed image messages."""
        self.latest_images[topic] = msg
        if not self.topics_received[topic]:
            self.topics_received[topic] = True
            self.get_logger().info(f"First message received on image topic: {topic}")
            self.check_all_topics_received()
    
    def camera_info_callback(self, msg: CameraInfo, topic: str):
        """Handle incoming camera info messages (intrinsics/extrinsics)."""
        self.camera_info_cache[topic] = msg
        if not self.topics_received[topic]:
            self.topics_received[topic] = True
            self.get_logger().info(f"First message received on camera info topic: {topic}")
            self.check_all_topics_received()
    
    def odom_callback(self, msg: Odometry):
        """Handle incoming odometry messages."""
        self.latest_odom = msg
        if not self.topics_received[self.odom_topic]:
            self.topics_received[self.odom_topic] = True
            self.get_logger().info(f"First message received on odom topic: {self.odom_topic}")
            self.check_all_topics_received()
    
    def head_position_callback(self, msg: String):
        """Handle incoming head position messages (JSON string)."""
        try:
            data = json.loads(msg.data)
            self.latest_head_position = data.get('current_position', 0.0)
            self.latest_head_timestamp = time.time()
        except json.JSONDecodeError:
            self.get_logger().warn(f"Failed to parse head position JSON: {msg.data}")
            return
        
        if not self.topics_received.get(self.head_position_topic, True):
            self.topics_received[self.head_position_topic] = True
            self.get_logger().info(f"First message received on head position topic: {self.head_position_topic}")
            self.check_all_topics_received()
    
    def timer_callback(self):
        """Timer callback to record data at fixed frequency - streams directly to disk."""
        if self.state != "RECORDING" or self.stream_writer is None:
            return
        
        # Check if we have all required data
        for topic in self.camera_topics:
            if self.latest_images[topic] is None:
                self.get_logger().warn(f"Missing image from {topic}, skipping frame.")
                return
        
        if self.latest_odom is None:
            self.get_logger().warn("Missing odometry data, skipping frame.")
            return
        
        # Write camera info once at the start of recording
        if not self.camera_info_written and self.camera_info_cache:
            self.stream_writer.write_camera_info(self.camera_info_cache)
            self.camera_info_written = True
        
        # Prepare image data
        images = {}
        image_timestamps = {}
        for topic in self.camera_topics:
            img_msg = self.latest_images[topic]
            cam_name = topic.replace('/', '_').strip('_')
            
            # Get compressed image bytes as numpy array
            images[cam_name] = np.frombuffer(img_msg.data, dtype=np.uint8)
            
            # Get timestamp
            img_stamp = img_msg.header.stamp
            image_timestamps[cam_name] = img_stamp.sec + img_stamp.nanosec * 1e-9
        
        # Prepare odometry data
        odom = self.latest_odom
        odom_data = {
            'position': np.array([
                odom.pose.pose.position.x,
                odom.pose.pose.position.y,
                odom.pose.pose.position.z
            ]),
            'orientation': np.array([
                odom.pose.pose.orientation.x,
                odom.pose.pose.orientation.y,
                odom.pose.pose.orientation.z,
                odom.pose.pose.orientation.w
            ]),
            'linear_velocity': np.array([
                odom.twist.twist.linear.x,
                odom.twist.twist.linear.y,
                odom.twist.twist.linear.z
            ]),
            'angular_velocity': np.array([
                odom.twist.twist.angular.x,
                odom.twist.twist.angular.y,
                odom.twist.twist.angular.z
            ]),
            'pose_covariance': np.array(odom.pose.covariance),
            'twist_covariance': np.array(odom.twist.covariance)
        }
        
        # Get odom timestamp
        odom_stamp = odom.header.stamp
        odom_timestamp = odom_stamp.sec + odom_stamp.nanosec * 1e-9
        
        # Stream directly to disk
        try:
            self.stream_writer.write_frame(
                images, image_timestamps, odom_data, odom_timestamp,
                head_position=self.latest_head_position,
                head_timestamp=self.latest_head_timestamp
            )
        except Exception as e:
            self.get_logger().error(f"Error writing frame to disk: {e}")
            return
        
        frame_count = self.stream_writer.get_frame_count()
        
        # Log progress every second
        if frame_count % self.data_frequency == 0:
            elapsed = time.time() - self.session_start_time
            self.get_logger().info(
                f"Recording... {frame_count} frames ({elapsed:.1f}s) - streaming to disk"
            )
    
    def _call_calibrate_service(self):
        """Call the /calibrate service before starting recording."""
        if not self.calibrate_client.wait_for_service(timeout_sec=2.0):
            self.get_logger().warn("/calibrate service not available, skipping calibration")
            return False
        
        self.get_logger().info("Calling /calibrate service...")
        request = Trigger.Request()
        future = self.calibrate_client.call_async(request)
        
        # Wait for result with timeout
        start_time = time.time()
        while not future.done():
            if time.time() - start_time > 5.0:
                self.get_logger().warn("Calibration service call timed out")
                return False
            time.sleep(0.1)
        
        try:
            result = future.result()
            if result.success:
                self.get_logger().info(f"Calibration successful: {result.message}")
                return True
            else:
                self.get_logger().warn(f"Calibration failed: {result.message}")
                return False
        except Exception as e:
            self.get_logger().error(f"Calibration service error: {e}")
            return False
    
    def handle_start_recording(self, request, response):
        """Start a new recording session with disk streaming."""
        if self.state == "RECORDING":
            response.success = False
            response.message = "Already recording. Stop current session first."
            return response
        
        # Call calibrate service before starting
        self._call_calibrate_service()
        
        # Wait for odometry to reinitialize after calibration
        self.get_logger().info("Waiting 5 seconds for odometry to reinitialize...")
        time.sleep(5.0)
        self.get_logger().info("Odometry wait complete, starting recording...")
        
        # Generate session name with timestamp
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        self.current_session_name = f"{self.session_name_prefix}_{timestamp}"
        
        # Create session directory
        self.session_dir = os.path.join(self.data_directory, self.current_session_name)
        os.makedirs(self.session_dir, exist_ok=True)
        
        # Get camera names from topics
        camera_names = [topic.replace('/', '_').strip('_') for topic in self.camera_topics]
        
        # Initialize HDF5 stream writer
        h5_path = get_h5_path(self.session_dir)
        self.stream_writer = HDF5StreamWriter(
            h5_path, camera_names, self.chunk_size,
            record_head_position=bool(self.head_position_topic)
        )
        
        try:
            self.stream_writer.open()
        except Exception as e:
            self.get_logger().error(f"Failed to open HDF5 file: {e}")
            response.success = False
            response.message = f"Failed to open HDF5 file: {str(e)}"
            return response
        
        self.camera_info_written = False
        self.session_start_time = time.time()
        self.state = "RECORDING"
        
        self.get_logger().info(f"=== RECORDING STARTED (STREAMING TO DISK) ===")
        self.get_logger().info(f"Session: {self.current_session_name}")
        self.get_logger().info(f"Recording at {self.data_frequency} Hz")
        self.get_logger().info(f"Output: {h5_path}")
        self.publish_status("recording")
        
        response.success = True
        response.message = f"Recording started: {self.current_session_name}"
        return response
    
    def handle_stop_recording(self, request, response):
        """Stop recording and finalize the file."""
        if self.state != "RECORDING":
            response.success = False
            response.message = "Not currently recording."
            return response
        
        self.state = "IDLE"
        duration = time.time() - self.session_start_time
        frame_count = self.stream_writer.get_frame_count() if self.stream_writer else 0
        
        # Finalize HDF5 file
        try:
            if self.stream_writer:
                # Write final metadata
                self.stream_writer.write_metadata({
                    'session_name': self.current_session_name,
                    'data_frequency': self.data_frequency,
                    'start_time': time.strftime(
                        '%Y-%m-%dT%H:%M:%S', time.localtime(self.session_start_time)
                    ),
                    'duration_sec': duration,
                    'num_frames': frame_count
                })
                self.stream_writer.close()
            
            # Also save a JSON metadata file for easy inspection
            self._save_json_metadata(duration, frame_count)
            
            h5_path = get_h5_path(self.session_dir)
            self.get_logger().info(f"=== RECORDING SAVED ===")
            self.get_logger().info(f"Session: {self.current_session_name}")
            self.get_logger().info(f"Duration: {duration:.1f}s, Frames: {frame_count}")
            self.get_logger().info(f"Saved to: {h5_path}")
            self.publish_status("saved")
            
            response.success = True
            response.message = f"Recording saved: {h5_path} ({frame_count} frames, {duration:.1f}s)"
            
        except Exception as e:
            self.get_logger().error(f"Error finalizing recording: {e}")
            self.publish_status("error")
            response.success = False
            response.message = f"Error finalizing recording: {str(e)}"
        
        finally:
            self.stream_writer = None
        
        return response
    
    def handle_get_status(self, request, response):
        """Get current recorder status."""
        frame_count = 0
        if self.stream_writer:
            frame_count = self.stream_writer.get_frame_count()
        
        status_info = {
            "state": self.state,
            "session_name": self.current_session_name,
            "frame_count": frame_count,
            "all_topics_received": self.all_topics_received,
            "topics_status": self.topics_received,
            "streaming_mode": "disk"
        }
        response.success = True
        response.message = json.dumps(status_info)
        return response
    
    def _save_json_metadata(self, duration: float, frame_count: int):
        """Save JSON metadata file for easy inspection."""
        metadata = {
            'session_name': self.current_session_name,
            'data_frequency': self.data_frequency,
            'start_time': time.strftime(
                '%Y-%m-%dT%H:%M:%S', time.localtime(self.session_start_time)
            ),
            'duration_sec': duration,
            'num_frames': frame_count,
            'camera_topics': self.camera_topics,
            'camera_info_topics': self.camera_info_topics,
            'odom_topic': self.odom_topic,
            'head_position_topic': self.head_position_topic,
            'streaming_mode': 'disk'
        }
        
        # Add camera info to metadata
        if self.camera_info_cache:
            metadata['camera_info'] = {}
            for topic, cam_info in self.camera_info_cache.items():
                cam_name = topic.replace('/', '_').strip('_')
                metadata['camera_info'][cam_name] = {
                    'width': cam_info.width,
                    'height': cam_info.height,
                    'distortion_model': cam_info.distortion_model,
                    'K': list(cam_info.k),
                    'D': list(cam_info.d),
                    'R': list(cam_info.r),
                    'P': list(cam_info.p),
                    'frame_id': cam_info.header.frame_id
                }
        
        metadata_path = get_metadata_path(self.session_dir)
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
    
    def publish_status(self, status: str):
        """Publish current status."""
        frame_count = 0
        if self.stream_writer:
            frame_count = self.stream_writer.get_frame_count()
        
        msg = String()
        msg.data = json.dumps({
            'state': self.state,
            'status': status,
            'session_name': self.current_session_name,
            'frame_count': frame_count
        })
        self.status_pub.publish(msg)
    
    def destroy_node(self):
        """Clean up resources on shutdown."""
        # Ensure HDF5 file is closed on shutdown
        if self.stream_writer:
            self.get_logger().warn("Closing HDF5 file due to node shutdown...")
            try:
                self.stream_writer.close()
            except Exception as e:
                self.get_logger().error(f"Error closing HDF5 file: {e}")
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = CameraOdomRecorderNode()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Camera + Odometry Recorder Node shutting down.")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
