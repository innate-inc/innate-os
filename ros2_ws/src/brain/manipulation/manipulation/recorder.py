#!/usr/bin/env python3
import time
import rclpy
from rclpy.node import Node
from std_srvs.srv import Trigger
from brain_messages.srv import ManipulationTask
from manipulation.DataUtils import EpisodeData, TaskManager

# Import message types for sensor data
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray
from geometry_msgs.msg import Twist
from cv_bridge import CvBridge  # For image conversion
import cv2
# Import RecorderStatus message
from brain_messages.msg import RecorderStatus
import os
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

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
        self.declare_parameter('image_size', [640, 480])
        
        # Get parameter values
        data_directory = os.path.expanduser(self.get_parameter('data_directory').value)
        self.data_frequency = self.get_parameter('data_frequency').value
        self.image_topics = self.get_parameter('image_topics').value
        self.arm_state_topic = self.get_parameter('arm_state_topic').value
        self.leader_command_topic = self.get_parameter('leader_command_topic').value
        self.velocity_topic = self.get_parameter('velocity_topic').value
        self.image_size = self.get_parameter('image_size').value
        # Initialize TaskManager and internal state
        self.task_manager = TaskManager(data_directory)
        self.current_episode = None  # Holds an EpisodeData instance when an episode is active
        self.state = "IDLE"          # Possible states: "IDLE", "TASK_ACTIVE", "EPISODE_ACTIVE"
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
        
        # Log the topics it is subscribing to
        self.get_logger().info(f"Subscribing to image topics: {self.image_topics}")
        self.get_logger().info(f"Subscribing to arm state topic: {self.arm_state_topic}")
        self.get_logger().info(f"Subscribing to leader command topic: {self.leader_command_topic}")
        self.get_logger().info(f"Subscribing to velocity topic: {self.velocity_topic}")
        
        # Create service servers with updated names prefixed with "recorder/"
        self.new_task_srv = self.create_service(ManipulationTask, 'recorder/new_task', self.handle_new_task)
        self.new_episode_srv = self.create_service(Trigger, 'recorder/new_episode', self.handle_new_episode)
        self.save_episode_srv = self.create_service(Trigger, 'recorder/save_episode', self.handle_save_episode)
        self.cancel_episode_srv = self.create_service(Trigger, 'recorder/cancel_episode', self.handle_cancel_episode)
        self.end_task_srv = self.create_service(Trigger, 'recorder/end_task', self.handle_end_task)
        
        # Log the services it is hosting
        self.get_logger().info("Hosting services:")
        self.get_logger().info("  recorder/new_task")
        self.get_logger().info("  recorder/new_episode")
        self.get_logger().info("  recorder/save_episode")
        self.get_logger().info("  recorder/cancel_episode")
        self.get_logger().info("  recorder/end_task")
        
        # Create a publisher for recorder status
        self.status_pub = self.create_publisher(RecorderStatus, 'recorder/status', 10)
        
        # Create a timer that will attempt to add sensor data as a new timestep at the specified frequency.
        self.timer = self.create_timer(1.0 / self.data_frequency, self.timer_callback)
        
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

    # Timer callback to record sensor data as a timestep.
    def timer_callback(self):
        # Only record data if an episode is active.
        if self.state != "EPISODE_ACTIVE" or self.current_episode is None:
            return
        
        # Ensure all sensor data are available.
        for topic in self.image_topics:
            if self.latest_images[topic] is None:
                self.get_logger().warn(f"Incomplete sensor data; missing image from topic {topic}. Skipping timestep.")
                return
        
        if self.latest_arm_state is None or self.latest_leader_command is None or self.latest_cmd_vel is None:
            self.get_logger().warn("Incomplete sensor data; skipping timestep.")
            return
        
        # Build action data starting with leader command
        action_data = list(self.latest_leader_command.data)  # leader_command is a list of floats
        # Add only forward speed and yaw rate from cmd_vel
        twist = self.latest_cmd_vel
        action_data.extend([twist.linear.x, twist.angular.z])
        
        # Get joint positions and velocities from the arm state message.
        qpos = list(self.latest_arm_state.position)
        qvel = list(self.latest_arm_state.velocity)
        
        # Convert each ROS image message to a NumPy array using cv_bridge.
        images_converted = []
        for topic in self.image_topics:
            try:
                cv_image = self.bridge.imgmsg_to_cv2(self.latest_images[topic], desired_encoding='passthrough')
                cv_image = cv2.resize(cv_image, (self.image_size[0], self.image_size[1]))
                images_converted.append(cv_image)
            except Exception as e:
                self.get_logger().error(f"Error converting image from topic {topic}: {e}")
                return
        
        try:
            self.current_episode.add_timestep(action_data, qpos, qvel, images_converted)
            self.get_logger().debug("Added timestep to current episode.")
        except ValueError as e:
            self.get_logger().error(f"Error adding timestep: {e}")

    # Service handlers.
    def handle_new_task(self, request, response):
        if self.state == "EPISODE_ACTIVE":
            self.get_logger().warn("New task requested during an active episode; canceling current episode.")
            # Publish status update for cancelled episode
            self.publish_status(status="failed - episode active", episode_number=str(self.episode_count))
            self.current_episode = None
            self.state = "TASK_ACTIVE"
        self.task_manager.start_new_task(request.task_name, request.task_description, request.mobile_task, self.data_frequency)
        self.current_task_name = request.task_name
        self.state = "TASK_ACTIVE"
        self.get_logger().info(f"New task '{request.task_name}' started.")
        # Publish status update for new task
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
        elif self.state == "EPISODE_ACTIVE":
            self.get_logger().error("Cannot start a new episode while another episode is active.")
            self.publish_status(status="failed - episode active", episode_number=str(self.episode_count), current_task_name=self.current_task_name)
            response.success = False
            response.message = "An episode is already active. Please save or cancel the current episode first."
            return response
        
        self.current_episode = EpisodeData()
        self.episode_start_time = time.time()
        self.state = "EPISODE_ACTIVE"
        self.episode_count += 1
        episode_str = str(self.episode_count)
        self.get_logger().info("Episode started; now recording data.")
        self.publish_status(status="active", episode_number=episode_str, current_task_name=self.current_task_name)
        response.success = True
        response.message = "Episode started."
        return response

    def handle_save_episode(self, request, response):
        if self.state != "EPISODE_ACTIVE" or self.current_episode is None:
            self.get_logger().error("No active episode to save.")
            # Publish appropriate status based on current state
            if self.state == "TASK_ACTIVE":
                self.publish_status(status="failed - no active episode", episode_number="", current_task_name=self.current_task_name)
            else:  # IDLE state
                self.publish_status(status="failed - no active task", episode_number="", current_task_name="")
            response.success = False
            response.message = "No active episode."
            return response

        # Check if episode has any timesteps
        if self.current_episode.get_episode_length() == 0:
            self.get_logger().error("Cannot save empty episode with no timesteps.")
            self.publish_status(status="failed - empty episode", episode_number=str(self.episode_count), current_task_name=self.current_task_name)
            response.success = False
            response.message = "Cannot save empty episode."
            return response

        end_time = time.time()
        self.task_manager.add_episode(
            self.current_episode,
            time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.episode_start_time)),
            time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(end_time))
        )
        self.get_logger().info("Episode saved successfully.")
        # Publish status update for saved episode
        episode_str = str(self.episode_count)
        self.publish_status(status="saved", episode_number=episode_str, current_task_name=self.current_task_name)
        self.current_episode = None
        self.state = "TASK_ACTIVE"
        response.success = True
        response.message = "Episode saved."
        return response

    def handle_cancel_episode(self, request, response):
        if self.state != "EPISODE_ACTIVE" or self.current_episode is None:
            self.get_logger().error("No active episode to cancel.")
            # Publish appropriate status based on current state
            if self.state == "TASK_ACTIVE":
                self.publish_status(status="failed - no active episode", episode_number="", current_task_name=self.current_task_name)
            else:  # IDLE state
                self.publish_status(status="failed - no active task", episode_number="", current_task_name="")
            response.success = False
            response.message = "No active episode."
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

    def handle_end_task(self, request, response):
        if self.state == "EPISODE_ACTIVE":
            self.get_logger().warn("Ending task during an active episode; canceling current episode first.")
            # Publish status update for cancelled episode
            episode_str = str(self.episode_count)
            self.publish_status(status="cancelled", episode_number=episode_str, current_task_name=self.current_task_name)
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

    def publish_status(self, status: str, episode_number: str = "", current_task_name: str = ""):
        msg = RecorderStatus()
        msg.current_task_name = current_task_name if current_task_name else self.current_task_name
        msg.episode_number = episode_number
        msg.status = status
        self.status_pub.publish(msg)

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
