#!/usr/bin/env python3
import time
import rclpy
from rclpy.node import Node
from std_srvs.srv import Trigger
from brain_messages.srv import ManipulationTask
from manipulation.DataUtils import EpisodeData, TaskManager

# Import message types for sensor data
from sensor_msgs.msg import Image, JointState, CompressedImage
from std_msgs.msg import Float64MultiArray, Empty
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from cv_bridge import CvBridge  # For image conversion
import cv2
import numpy as np
import h5py
# Import RecorderStatus message
from brain_messages.msg import RecorderStatus, ReplayStatus
import os
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
import json # Added for JSON manipulation

# Service imports
from brain_messages.srv import GetTaskMetadataList, UpdateTaskMetadata, GetTaskMetadata, LoadEpisode

class RecorderNode(Node):
    def __init__(self):
        super().__init__('recorder_node')
        
        # Load parameters (can be set via a YAML file or launch file)
        self.declare_parameter('data_directory', '/path/to/data')
        self.declare_parameter('data_frequency', 10)
        self.declare_parameter('image_topics', ['/camera/image_raw', '/camera/image_processed'])
        self.declare_parameter('arm_state_topic', '/arm/state')
        self.declare_parameter('leader_command_topic', '/leader/command')
        self.declare_parameter('velocity_topic', '/cmd_vel')
        self.declare_parameter('odom_topic', '/odom')
        self.declare_parameter('image_size', [640, 480])
        self.declare_parameter('head_ai_position_topic', '/mars/head/set_ai_position')
        
        # Get parameter values
        data_directory = os.path.expanduser(self.get_parameter('data_directory').value)
        self.data_frequency = self.get_parameter('data_frequency').value
        self.image_topics = self.get_parameter('image_topics').value
        self.arm_state_topic = self.get_parameter('arm_state_topic').value
        self.leader_command_topic = self.get_parameter('leader_command_topic').value
        self.velocity_topic = self.get_parameter('velocity_topic').value
        self.odom_topic = self.get_parameter('odom_topic').value
        self.image_size = self.get_parameter('image_size').value
        self.head_ai_position_topic = self.get_parameter('head_ai_position_topic').value
        # Initialize TaskManager and internal state
        self.task_manager = TaskManager(data_directory)
        self.current_episode = None  # Holds an EpisodeData instance when an episode is active
        self.state = "IDLE"          # Possible states: "IDLE", "TASK_ACTIVE", "EPISODE_ACTIVE", "EPISODE_STOPPED"
        self.episode_start_time = None
        
        # Internal variables to track current task and episode number
        self.current_task_name = ""
        self.episode_count = 0
        
        # Initialize CvBridge for image conversion
        self.bridge = CvBridge()
        
        # Latest sensor data variables
        # Use a dictionary to store the latest image for each topic.
        self.latest_images = {topic: None for topic in self.image_topics}
        self.latest_arm_state = None
        self.latest_leader_command = None
        self.latest_cmd_vel = None
        self.latest_odom = None
        
        # Tracking message reception for each topic.
        # Initialize booleans for image topics and the other topics.
        self.topics_received = {}
        for topic in self.image_topics:
            self.topics_received[topic] = False
        self.topics_received[self.arm_state_topic] = False
        self.topics_received[self.leader_command_topic] = False
        self.topics_received[self.velocity_topic] = False

        # Overall flag: True when every topic has received at least one message.
        self.all_topics_received = False
        
        # Create subscribers for sensor topics.
        # Subscribe to each image topic with QoS profile
        image_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )

        for topic in self.image_topics:
            self.create_subscription(
                Image, 
                topic, 
                lambda msg, t=topic: self.image_callback(msg, t), 
                image_qos
            )
        self.create_subscription(JointState, self.arm_state_topic, self.arm_state_callback, 10)
        self.create_subscription(Float64MultiArray, self.leader_command_topic, self.leader_command_callback, 10)
        self.create_subscription(Twist, self.velocity_topic, self.cmd_vel_callback, 10)
        self.create_subscription(Odometry, self.odom_topic, self.odom_callback, 10)
        
        # Log the topics it is subscribing to
        self.get_logger().info(f"Subscribing to image topics: {self.image_topics}")
        self.get_logger().info(f"Subscribing to arm state topic: {self.arm_state_topic}")
        self.get_logger().info(f"Subscribing to leader command topic: {self.leader_command_topic}")
        self.get_logger().info(f"Subscribing to velocity topic: {self.velocity_topic}")
        self.get_logger().info(f"Subscribing to odom topic: {self.odom_topic}")
        
        # Create publishers
        self.head_ai_position_pub = self.create_publisher(Empty, self.head_ai_position_topic, 10)
        
        # Create service servers with updated names prefixed with "recorder/"
        self.new_physical_primitive_srv = self.create_service(ManipulationTask, 'brain/recorder/new_physical_primitive', self.handle_new_physical_primitive)
        self.new_episode_srv = self.create_service(Trigger, 'brain/recorder/new_episode', self.handle_new_episode)
        self.save_episode_srv = self.create_service(Trigger, 'brain/recorder/save_episode', self.handle_save_episode)
        self.cancel_episode_srv = self.create_service(Trigger, 'brain/recorder/cancel_episode', self.handle_cancel_episode)
        self.stop_episode_srv = self.create_service(Trigger, 'brain/recorder/stop_episode', self.handle_stop_episode)
        self.end_task_srv = self.create_service(Trigger, 'brain/recorder/end_task', self.handle_end_task)
        self.get_task_metadata_list_srv = self.create_service(GetTaskMetadataList, 'brain/recorder/get_task_metadata_list', self.handle_get_task_metadata_list)
        self.update_task_metadata_srv = self.create_service(UpdateTaskMetadata, 'brain/recorder/update_task_metadata', self.handle_update_task_metadata)
        self.get_task_metadata_srv = self.create_service(GetTaskMetadata, 'brain/recorder/get_task_metadata', self.handle_get_task_metadata)
        
        # Log the services it is hosting
        self.get_logger().info("Hosting services:")
        self.get_logger().info("  brain/recorder/new_physical_primitive")
        self.get_logger().info("  brain/recorder/new_episode")
        self.get_logger().info("  brain/recorder/save_episode")
        self.get_logger().info("  brain/recorder/cancel_episode")
        self.get_logger().info("  brain/recorder/stop_episode")
        self.get_logger().info("  brain/recorder/end_task")
        self.get_logger().info("  brain/recorder/get_task_metadata_list")
        self.get_logger().info("  brain/recorder/update_task_metadata")
        self.get_logger().info("  brain/recorder/get_task_metadata")
        
        # Create a publisher for recorder status
        self.status_pub = self.create_publisher(RecorderStatus, '/brain/recorder/status', 10)
        
        # Create a timer that will attempt to add sensor data as a new timestep at the specified frequency.
        self.timer = self.create_timer(1.0 / self.data_frequency, self.timer_callback)
        
        # ========== REPLAY FUNCTIONALITY ==========
        # Replay state: "idle", "ready", "playing", "paused", "finished"
        self.replay_state = "idle"
        self.replay_buffer = {}  # Will hold {'main_camera': [...], 'arm_camera': [...]}
        self.replay_frame_index = 0
        self.replay_total_frames = 0
        self.replay_fps = 10.0
        self.replay_task_name = ""
        self.replay_episode_id = ""
        self.replay_timer = None
        
        # Replay publishers (CompressedImage for WebRTC compatibility)
        self.replay_main_pub = self.create_publisher(
            CompressedImage, 
            '/brain/recorder/replay/main_camera/compressed', 
            10
        )
        self.replay_arm_pub = self.create_publisher(
            CompressedImage, 
            '/brain/recorder/replay/arm_camera/compressed', 
            10
        )
        self.replay_status_pub = self.create_publisher(
            ReplayStatus, 
            '/brain/recorder/replay_status', 
            10
        )
        
        # Replay services
        self.load_episode_srv = self.create_service(
            LoadEpisode, 
            'brain/recorder/load_episode', 
            self.handle_load_episode
        )
        self.play_replay_srv = self.create_service(
            Trigger, 
            'brain/recorder/play_replay', 
            self.handle_play_replay
        )
        self.pause_replay_srv = self.create_service(
            Trigger, 
            'brain/recorder/pause_replay', 
            self.handle_pause_replay
        )
        self.stop_replay_srv = self.create_service(
            Trigger, 
            'brain/recorder/stop_replay', 
            self.handle_stop_replay
        )
        
        self.get_logger().info("Replay services initialized:")
        self.get_logger().info("  brain/recorder/load_episode")
        self.get_logger().info("  brain/recorder/play_replay")
        self.get_logger().info("  brain/recorder/pause_replay")
        self.get_logger().info("  brain/recorder/stop_replay")
        
        self.get_logger().info("Recorder Node initialized in IDLE state.")
    
    def check_all_topics_received(self):
        """Check if every subscribed topic has received at least one message."""
        if not self.all_topics_received and all(self.topics_received.values()):
            self.all_topics_received = True
            self.get_logger().info("All topics have received at least one message.")

    # Generic image callback to store images by topic.
    def image_callback(self, msg, topic: str):
        self.latest_images[topic] = msg
        if not self.topics_received[topic]:
            self.topics_received[topic] = True
            self.get_logger().info(f"First message received on image topic: {topic}")
            self.check_all_topics_received()
    
    def arm_state_callback(self, msg):
        self.latest_arm_state = msg
        if not self.topics_received[self.arm_state_topic]:
            self.topics_received[self.arm_state_topic] = True
            self.get_logger().info(f"First message received on arm state topic: {self.arm_state_topic}")
            self.check_all_topics_received()

    def leader_command_callback(self, msg):
        self.latest_leader_command = msg
        if not self.topics_received[self.leader_command_topic]:
            self.topics_received[self.leader_command_topic] = True
            self.get_logger().info(f"First message received on leader command topic: {self.leader_command_topic}")
            self.check_all_topics_received()

    def cmd_vel_callback(self, msg):
        self.latest_cmd_vel = msg
        if not self.topics_received[self.velocity_topic]:
            self.topics_received[self.velocity_topic] = True
            self.get_logger().info(f"First message received on velocity topic: {self.velocity_topic}")
            self.check_all_topics_received()

    def odom_callback(self, msg):
        self.latest_odom = msg

    # Timer callback to record sensor data as a timestep.
    def timer_callback(self):
        # Only record data if an episode is active.
        if self.state != "EPISODE_ACTIVE" or self.current_episode is None:
            return
        
        # Ensure required sensor data are available (images, arm_state, odom)
        for topic in self.image_topics:
            if self.latest_images[topic] is None:
                self.get_logger().warn(f"Incomplete sensor data; missing image from topic {topic}. Skipping timestep.")
                return
        
        if self.latest_arm_state is None:
            self.get_logger().warn("Incomplete sensor data; missing arm state. Skipping timestep.")
            return
        
        if self.latest_odom is None:
            self.get_logger().warn("Incomplete sensor data; missing odom. Skipping timestep.")
            return
        
        # Build action data starting with leader command (use zeros if not available)
        if self.latest_leader_command is not None:
            action_data = list(self.latest_leader_command.data)
        else:
            action_data = [0.0] * 10  # Default to 10 zeros for arm commands
        
        # Add forward speed and yaw rate from cmd_vel (use zeros if not available)
        if self.latest_cmd_vel is not None:
            action_data.extend([self.latest_cmd_vel.linear.x, self.latest_cmd_vel.angular.z])
        else:
            action_data.extend([0.0, 0.0])
        
        # Get joint positions and velocities from the arm state message.
        qpos = list(self.latest_arm_state.position)
        qvel = list(self.latest_arm_state.velocity)
        
        # Get timestamp from arm state message (seconds since epoch)
        arm_stamp = self.latest_arm_state.header.stamp
        arm_timestamp = arm_stamp.sec + arm_stamp.nanosec * 1e-9
        
        # Convert each ROS image message to a NumPy array using cv_bridge.
        # Also capture image timestamps
        images_converted = []
        image_timestamps = []
        for topic in self.image_topics:
            try:
                img_msg = self.latest_images[topic]
                cv_image = self.bridge.imgmsg_to_cv2(img_msg, desired_encoding='passthrough')
                cv_image = cv2.resize(cv_image, (self.image_size[0], self.image_size[1]))
                images_converted.append(cv_image)
                
                # Get timestamp from image message
                img_stamp = img_msg.header.stamp
                image_timestamps.append(img_stamp.sec + img_stamp.nanosec * 1e-9)
            except Exception as e:
                self.get_logger().error(f"Error converting image from topic {topic}: {e}")
                return
        
        try:
            self.current_episode.add_timestep(
                action_data, qpos, qvel, images_converted,
                arm_timestamp=arm_timestamp,
                image_timestamps=image_timestamps
            )
            timestep_count = self.current_episode.get_episode_length()
            # Log progress every 20 timesteps (roughly every second at 20Hz)
            if timestep_count % 20 == 0:
                elapsed = time.time() - self.episode_start_time
                self.get_logger().info(f"Recording... {timestep_count} timesteps ({elapsed:.1f}s)")
        except ValueError as e:
            self.get_logger().error(f"Error adding timestep: {e}")

    def _set_head_ai_position(self):
        """Set the head to AI position for recording."""
        try:
            empty_msg = Empty()
            self.head_ai_position_pub.publish(empty_msg)
            self.get_logger().info("AI position command sent to head for recording setup")
            time.sleep(3.0)  # Wait for head to move to AI position
        except Exception as e:
            self.get_logger().error(f"Error setting AI position: {e}")

    # Service handlers.
    def handle_new_physical_primitive(self, request, response):
        """Handle request to create a new physical primitive (type: learned)."""
        if self.state in ["EPISODE_ACTIVE", "EPISODE_STOPPED"]:
            self.get_logger().warn(f"New physical primitive requested during an {self.state.lower()} episode; canceling current episode.")
            # Publish status update for cancelled episode
            self.publish_status(status="cancelled", episode_number=str(self.episode_count), current_task_name=self.current_task_name)
            if self.current_episode:
                self.current_episode.clear()
            self.current_episode = None
            # self.state will be set to TASK_ACTIVE by the rest of the method.
            # Episode count for the new primitive will effectively start fresh.
        
        # Set head to AI position for optimal camera angle during recording
        self.get_logger().info("Setting head to AI position for new physical primitive setup")
        self._set_head_ai_position()
        
        # Start new task with type set to 'learned'
        self.task_manager.start_new_task(
            request.task_name,
            self.data_frequency,
            primitive_type='learned'  # Physical primitives are of type 'learned'
        )
        if self.task_manager.metadata:
            self.episode_count = self.task_manager.metadata["number_of_episodes"]
        else:
            self.episode_count = 0
        self.current_task_name = request.task_name
        self.state = "TASK_ACTIVE"
        self.get_logger().info(f"New physical primitive '{request.task_name}' (type: learned) started.")
        # Publish status update for new primitive
        self.publish_status(status="active", episode_number="", current_task_name=self.current_task_name)
        response.success = True
        return response

    def handle_new_episode(self, request, response):
        if self.state == "IDLE":
            self.get_logger().error("Cannot start a new episode unless a task is active.")
            self.publish_status(status="failed - no active task", episode_number="", current_task_name="")
            response.success = False
            response.message = "No active task. Please start a task first."
            return response
        elif self.state in ["EPISODE_ACTIVE", "EPISODE_STOPPED"]:
            self.get_logger().error(f"Cannot start a new episode while an episode is {self.state.lower()}.")
            self.publish_status(status=f"failed - episode {self.state.lower()}", episode_number=str(self.episode_count), current_task_name=self.current_task_name)
            response.success = False
            response.message = f"An episode is already {self.state.lower()}. Please save or cancel the current episode first."
            return response
        
        self.current_episode = EpisodeData()
        self.episode_start_time = time.time()
        self.state = "EPISODE_ACTIVE"
        self.episode_count += 1
        episode_str = str(self.episode_count)
        self.get_logger().info(f"=== RECORDING STARTED ===")
        self.get_logger().info(f"Task: {self.current_task_name}, Episode: {episode_str}")
        self.get_logger().info(f"Recording at {self.data_frequency} Hz")
        self.get_logger().info(f"Image topics: {self.image_topics}")
        self.publish_status(status="active", episode_number=episode_str, current_task_name=self.current_task_name)
        response.success = True
        response.message = "Episode started."
        return response

    def handle_save_episode(self, request, response):
        if self.state not in ["EPISODE_ACTIVE", "EPISODE_STOPPED"] or self.current_episode is None:
            self.get_logger().error("No active or stopped episode to save.")
            # Publish appropriate status based on current state
            if self.state == "TASK_ACTIVE":
                self.publish_status(status="failed - no active/stopped episode to save", episode_number="", current_task_name=self.current_task_name)
            else:  # IDLE state
                self.publish_status(status="failed - no active task", episode_number="", current_task_name="")
            response.success = False
            response.message = "No active or stopped episode to save."
            return response

        # Check if episode has any timesteps
        if self.current_episode.get_episode_length() == 0:
            self.get_logger().error("Cannot save empty episode with no timesteps.")
            self.publish_status(status="failed - empty episode", episode_number=str(self.episode_count), current_task_name=self.current_task_name)
            response.success = False
            response.message = "Cannot save empty episode."
            return response

        end_time = time.time()
        duration = end_time - self.episode_start_time
        timesteps = self.current_episode.get_episode_length()
        self.task_manager.add_episode(
            self.current_episode,
            time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.episode_start_time)),
            time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(end_time))
        )
        self.get_logger().info(f"=== EPISODE SAVED ===")
        self.get_logger().info(f"Task: {self.current_task_name}, Episode: {self.episode_count}")
        self.get_logger().info(f"Duration: {duration:.1f}s, Timesteps: {timesteps}")
        # Publish status update for saved episode
        episode_str = str(self.episode_count)
        self.publish_status(status="saved", episode_number=episode_str, current_task_name=self.current_task_name)
        self.current_episode = None
        self.state = "TASK_ACTIVE"
        response.success = True
        response.message = "Episode saved."
        return response

    def handle_cancel_episode(self, request, response):
        if self.state not in ["EPISODE_ACTIVE", "EPISODE_STOPPED"] or self.current_episode is None:
            self.get_logger().error("No active or stopped episode to cancel.")
            # Publish appropriate status based on current state
            if self.state == "TASK_ACTIVE":
                self.publish_status(status="failed - no active/stopped episode to cancel", episode_number="", current_task_name=self.current_task_name)
            else:  # IDLE state
                self.publish_status(status="failed - no active task", episode_number="", current_task_name="")
            response.success = False
            response.message = "No active or stopped episode to cancel."
            return response

        self.current_episode.clear()
        # Only decrement if episode_count is greater than 0
        if self.episode_count > 0:
            self.episode_count -= 1
        self.get_logger().info("Episode canceled; buffered data discarded.")
        # Publish status update for cancelled episode
        episode_str = str(self.episode_count)
        self.publish_status(status="cancelled", episode_number=episode_str, current_task_name=self.current_task_name)
        self.current_episode = None
        self.state = "TASK_ACTIVE"
        response.success = True
        response.message = "Episode canceled."
        return response

    def handle_stop_episode(self, request, response):
        if self.state == "EPISODE_ACTIVE":
            self.state = "EPISODE_STOPPED"
            self.get_logger().info("Episode recording stopped. Waiting for save or cancel command.")
            self.publish_status(status="stopped", episode_number=str(self.episode_count), current_task_name=self.current_task_name)
            response.success = True
            response.message = "Episode recording stopped. Awaiting save or cancel."
        elif self.state == "EPISODE_STOPPED":
            self.get_logger().warn("Stop episode requested, but episode is already stopped.")
            self.publish_status(status="failed - episode already stopped", episode_number=str(self.episode_count), current_task_name=self.current_task_name)
            response.success = False
            response.message = "Episode is already stopped."
        elif self.state == "TASK_ACTIVE":
            self.get_logger().error("Stop episode requested, but no episode is active.")
            self.publish_status(status="failed - no active episode", episode_number="", current_task_name=self.current_task_name) # Assuming episode_count is not relevant if no episode active
            response.success = False
            response.message = "No active episode to stop."
        else:  # IDLE
            self.get_logger().error("Stop episode requested, but no task is active.")
            self.publish_status(status="failed - no active task", episode_number="", current_task_name="")
            response.success = False
            response.message = "No active task or episode to stop."
        return response

    def handle_end_task(self, request, response):
        if self.state in ["EPISODE_ACTIVE", "EPISODE_STOPPED"]:
            self.get_logger().warn(f"Ending task during an {self.state.lower()} episode; canceling current episode first.")
            # Publish status update for cancelled episode
            episode_str = str(self.episode_count)
            self.publish_status(status="cancelled", episode_number=episode_str, current_task_name=self.current_task_name)
            if self.current_episode:
                self.current_episode.clear()
            self.current_episode = None
        self.task_manager.end_task()
        self.state = "IDLE"
        self.get_logger().info("Task ended; recorder state set to IDLE.")
        # Publish status update for ending task
        self.publish_status(status="idle", episode_number="", current_task_name="")
        # Reset task and episode tracking
        self.current_task_name = ""
        self.episode_count = 0
        response.success = True
        response.message = "Task ended."
        return response

    def handle_get_task_metadata_list(self, request, response):
        self.get_logger().info("Received request to get task metadata list.")
        try:
            # This method is hypothetical and needs to be implemented in your TaskManager class
            # It should scan the data_directory and return a list of dictionaries
            # conforming to the structure expected for the JSON output.
            if not hasattr(self.task_manager, 'get_all_tasks_summary'):
                self.get_logger().error("TaskManager does not have 'get_all_tasks_summary' method.")
                response.success = False
                response.message = "Internal server error: TaskManager cannot provide task summaries."
                response.json_metadata = "{}"
                return response

            all_tasks_summary = self.task_manager.get_all_tasks_summary()
            
            if not all_tasks_summary:
                self.get_logger().info("No tasks found by TaskManager.")
                response.success = True # Success, but no data
                response.message = "No tasks recorded yet."
                response.json_metadata = "[]" # Empty JSON array
                return response

            response.json_metadata = json.dumps(all_tasks_summary, indent=2)
            response.success = True
            response.message = "Successfully retrieved task metadata list."
            self.get_logger().info("Successfully prepared task metadata list.")
            
        except Exception as e:
            self.get_logger().error(f"Failed to get task metadata list: {str(e)}")
            response.success = False
            response.message = f"Error retrieving task metadata: {str(e)}"
            response.json_metadata = "{}" # Empty JSON object on error
        return response

    def handle_update_task_metadata(self, request, response):
        self.get_logger().info(f"Received request to update metadata for task directory: {request.task_directory}")
        try:
            if not hasattr(self.task_manager, 'update_task_metadata_by_directory'):
                self.get_logger().error("TaskManager does not have 'update_task_metadata_by_directory' method.")
                response.success = False
                response.message = "Internal server error: TaskManager cannot update task metadata by directory."
                return response

            success, message = self.task_manager.update_task_metadata_by_directory(request.task_directory, request.json_metadata_update)
            response.success = success
            response.message = message
            if success:
                self.get_logger().info(f"Successfully updated metadata for task directory: {request.task_directory}")
            else:
                self.get_logger().error(f"Failed to update metadata for task directory {request.task_directory}: {message}")
            
        except Exception as e:
            self.get_logger().error(f"Exception while updating task metadata for directory {request.task_directory}: {str(e)}")
            response.success = False
            response.message = f"Error updating task metadata: {str(e)}"
        return response

    def handle_get_task_metadata(self, request, response):
        self.get_logger().info(f"Received request to get metadata for task directory: {request.task_directory}")
        try:
            if not hasattr(self.task_manager, 'get_task_metadata_by_directory'):
                self.get_logger().error("TaskManager does not have 'get_task_metadata_by_directory' method.")
                response.success = False
                response.message = "Internal server error: TaskManager cannot provide task metadata by directory."
                response.json_metadata = "{}"
                return response

            success, message, metadata_json = self.task_manager.get_task_metadata_by_directory(request.task_directory)
            response.success = success
            response.message = message
            response.json_metadata = metadata_json

            if success:
                self.get_logger().info(f"Successfully retrieved metadata for task directory: {request.task_directory}")
            else:
                self.get_logger().error(f"Failed to retrieve metadata for task directory {request.task_directory}: {message}")
            
        except Exception as e:
            self.get_logger().error(f"Exception while retrieving task metadata for directory {request.task_directory}: {str(e)}")
            response.success = False
            response.message = f"Error retrieving task metadata: {str(e)}"
            response.json_metadata = "{}"
        return response

    def publish_status(self, status: str, episode_number: str = "", current_task_name: str = ""):
        msg = RecorderStatus()
        msg.current_task_name = current_task_name if current_task_name else self.current_task_name
        msg.episode_number = episode_number
        msg.status = status
        self.status_pub.publish(msg)

    # ========== REPLAY SERVICE HANDLERS ==========
    
    def handle_load_episode(self, request, response):
        """Load an episode from H5 file into memory buffer for replay."""
        self.get_logger().info(f"Loading episode {request.episode_id} from {request.task_directory}")
        
        try:
            # Stop any current replay
            if self.replay_state in ["playing", "paused"]:
                self._stop_replay_timer()
                self.replay_state = "idle"
            
            # Construct path to H5 file
            data_dir = os.path.join(request.task_directory, "data")
            h5_path = os.path.join(data_dir, f"episode_{request.episode_id}.h5")
            
            if not os.path.exists(h5_path):
                response.success = False
                response.message = f"Episode file not found: {h5_path}"
                response.num_frames = 0
                response.fps = 0.0
                response.duration_sec = 0.0
                response.arm_data_json = "{}"
                return response
            
            # Load dataset metadata to get fps
            metadata_path = os.path.join(data_dir, "dataset_metadata.json")
            if os.path.exists(metadata_path):
                with open(metadata_path, 'r') as f:
                    metadata = json.load(f)
                    self.replay_fps = float(metadata.get('data_frequency', 10))
            else:
                self.replay_fps = float(self.data_frequency)
            
            # Load images from H5 file
            with h5py.File(h5_path, 'r') as hf:
                if '/observations/images' not in hf:
                    response.success = False
                    response.message = "No images found in episode file"
                    response.num_frames = 0
                    response.fps = 0.0
                    response.duration_sec = 0.0
                    response.arm_data_json = "{}"
                    return response
                
                images_group = hf['/observations/images']
                camera_names = list(images_group.keys())
                
                if len(camera_names) == 0:
                    response.success = False
                    response.message = "No camera data found in episode"
                    response.num_frames = 0
                    response.fps = 0.0
                    response.duration_sec = 0.0
                    response.arm_data_json = "{}"
                    return response
                
                # Load all camera images into buffer
                self.replay_buffer = {}
                for cam_name in camera_names:
                    # Load entire dataset into memory (numpy array)
                    self.replay_buffer[cam_name] = np.array(images_group[cam_name])
                    self.get_logger().info(f"Loaded {cam_name}: {self.replay_buffer[cam_name].shape}")
                
                # Get frame count from first camera
                first_cam = camera_names[0]
                self.replay_total_frames = len(self.replay_buffer[first_cam])
            
            # Set replay metadata
            self.replay_frame_index = 0
            self.replay_task_name = os.path.basename(request.task_directory)
            self.replay_episode_id = f"episode_{request.episode_id}"
            self.replay_state = "ready"
            
            duration = self.replay_total_frames / self.replay_fps
            
            # Load arm state data and timestamps for frame drop detection
            arm_data = {
                "arm_timestamps": [],
                "image_timestamps": {},  # camera_name -> [timestamps]
                "qpos": [],
                "qvel": []
            }
            with h5py.File(h5_path, 'r') as hf:
                # Load arm timestamps
                if '/timestamps/arm' in hf:
                    arm_ts = np.array(hf['/timestamps/arm'])
                    if len(arm_ts) > 0:
                        arm_data["arm_timestamps"] = (arm_ts - arm_ts[0]).tolist()
                    self.get_logger().info(f"Loaded arm timestamps: {len(arm_ts)} frames")
                elif '/timestamps' in hf and not isinstance(hf['/timestamps'], h5py.Group):
                    # Legacy format: single timestamps array
                    timestamps_data = np.array(hf['/timestamps'])
                    if len(timestamps_data) > 0:
                        arm_data["arm_timestamps"] = (timestamps_data - timestamps_data[0]).tolist()
                    self.get_logger().info(f"Loaded legacy timestamps: {len(timestamps_data)} frames")
                else:
                    # Fallback: compute expected timestamps based on fps
                    arm_data["arm_timestamps"] = [i / self.replay_fps for i in range(self.replay_total_frames)]
                    self.get_logger().info(f"No timestamps in H5, using computed timestamps")
                
                # Load image timestamps for each camera
                if '/timestamps/images' in hf:
                    img_ts_group = hf['/timestamps/images']
                    for cam_name in img_ts_group.keys():
                        img_ts = np.array(img_ts_group[cam_name])
                        if len(img_ts) > 0:
                            arm_data["image_timestamps"][cam_name] = (img_ts - img_ts[0]).tolist()
                        self.get_logger().info(f"Loaded {cam_name} timestamps: {len(img_ts)} frames")
                
                # Load qpos if available
                if '/observations/qpos' in hf:
                    qpos_data = np.array(hf['/observations/qpos'])
                    arm_data["qpos"] = qpos_data.tolist()
                    self.get_logger().info(f"Loaded qpos: {qpos_data.shape}")
                
                # Load qvel if available
                if '/observations/qvel' in hf:
                    qvel_data = np.array(hf['/observations/qvel'])
                    arm_data["qvel"] = qvel_data.tolist()
                    self.get_logger().info(f"Loaded qvel: {qvel_data.shape}")
            
            self.get_logger().info(f"Episode loaded: {self.replay_total_frames} frames, {self.replay_fps} fps, {duration:.1f}s")
            
            # Publish status
            self._publish_replay_status()
            
            response.success = True
            response.message = f"Episode loaded successfully"
            response.num_frames = self.replay_total_frames
            response.fps = self.replay_fps
            response.duration_sec = duration
            response.arm_data_json = json.dumps(arm_data)
            
        except Exception as e:
            self.get_logger().error(f"Error loading episode: {e}")
            response.success = False
            response.message = f"Error loading episode: {str(e)}"
            response.num_frames = 0
            response.fps = 0.0
            response.duration_sec = 0.0
            response.arm_data_json = "{}"
        
        return response
    
    def handle_play_replay(self, request, response):
        """Start or resume replay playback."""
        if self.replay_state == "idle":
            response.success = False
            response.message = "No episode loaded. Call load_episode first."
            return response
        
        if self.replay_state == "playing":
            response.success = False
            response.message = "Replay is already playing."
            return response
        
        if self.replay_state == "finished":
            # Restart from beginning
            self.replay_frame_index = 0
        
        # Start the replay timer
        self._start_replay_timer()
        self.replay_state = "playing"
        
        self.get_logger().info(f"Replay started from frame {self.replay_frame_index}")
        self._publish_replay_status()
        
        response.success = True
        response.message = f"Replay started from frame {self.replay_frame_index}"
        return response
    
    def handle_pause_replay(self, request, response):
        """Pause replay playback."""
        if self.replay_state != "playing":
            response.success = False
            response.message = f"Cannot pause: replay is {self.replay_state}"
            return response
        
        self._stop_replay_timer()
        self.replay_state = "paused"
        
        self.get_logger().info(f"Replay paused at frame {self.replay_frame_index}")
        self._publish_replay_status()
        
        response.success = True
        response.message = f"Replay paused at frame {self.replay_frame_index}"
        return response
    
    def handle_stop_replay(self, request, response):
        """Stop replay and reset to beginning."""
        if self.replay_state == "idle":
            response.success = False
            response.message = "No episode loaded."
            return response
        
        self._stop_replay_timer()
        self.replay_frame_index = 0
        self.replay_state = "ready"
        
        self.get_logger().info("Replay stopped and reset to frame 0")
        self._publish_replay_status()
        
        response.success = True
        response.message = "Replay stopped and reset to frame 0"
        return response
    
    def _start_replay_timer(self):
        """Start the replay timer."""
        if self.replay_timer is not None:
            self.destroy_timer(self.replay_timer)
        self.replay_timer = self.create_timer(1.0 / self.replay_fps, self._replay_timer_callback)
    
    def _stop_replay_timer(self):
        """Stop the replay timer."""
        if self.replay_timer is not None:
            self.destroy_timer(self.replay_timer)
            self.replay_timer = None
    
    def _replay_timer_callback(self):
        """Timer callback to publish replay frames."""
        if self.replay_state != "playing":
            return
        
        if self.replay_frame_index >= self.replay_total_frames:
            # Reached end of episode
            self._stop_replay_timer()
            self.replay_state = "finished"
            self.get_logger().info("Replay finished")
            self._publish_replay_status()
            return
        
        # Get camera names and publish frames
        camera_names = list(self.replay_buffer.keys())
        
        for i, cam_name in enumerate(camera_names):
            # Get the frame (numpy array, likely BGR or RGB)
            frame = self.replay_buffer[cam_name][self.replay_frame_index]
            
            # Encode as JPEG for CompressedImage
            try:
                # Ensure frame is uint8
                if frame.dtype != np.uint8:
                    frame = frame.astype(np.uint8)
                
                # Encode to JPEG
                success, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not success:
                    self.get_logger().warn(f"Failed to encode frame for {cam_name}")
                    continue
                
                # Create CompressedImage message
                msg = CompressedImage()
                msg.header.stamp = self.get_clock().now().to_msg()
                msg.header.frame_id = cam_name
                msg.format = "jpeg"
                msg.data = encoded.tobytes()
                
                # Publish to appropriate topic
                # camera_1 -> main, camera_2 -> arm (based on typical setup)
                if i == 0 or 'main' in cam_name.lower() or cam_name == 'camera_1':
                    self.replay_main_pub.publish(msg)
                else:
                    self.replay_arm_pub.publish(msg)
                    
            except Exception as e:
                self.get_logger().error(f"Error publishing frame for {cam_name}: {e}")
        
        self.replay_frame_index += 1
        
        # Publish status periodically (every 10 frames)
        if self.replay_frame_index % 10 == 0:
            self._publish_replay_status()
    
    def _publish_replay_status(self):
        """Publish current replay status."""
        msg = ReplayStatus()
        msg.state = self.replay_state
        msg.current_frame = self.replay_frame_index
        msg.total_frames = self.replay_total_frames
        msg.current_time_sec = self.replay_frame_index / self.replay_fps if self.replay_fps > 0 else 0.0
        msg.total_time_sec = self.replay_total_frames / self.replay_fps if self.replay_fps > 0 else 0.0
        msg.episode_id = self.replay_episode_id
        msg.task_name = self.replay_task_name
        self.replay_status_pub.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = RecorderNode()
    
    # Create a MultiThreadedExecutor
    executor = rclpy.executors.MultiThreadedExecutor()
    executor.add_node(node)
    
    try:
        executor.spin()
    except KeyboardInterrupt:
        node.get_logger().info("Recorder Node shutting down.")
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
