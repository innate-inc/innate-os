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
        
        # Get parameter values
        data_directory = self.get_parameter('data_directory').value
        self.data_frequency = self.get_parameter('data_frequency').value
        self.image_topics = self.get_parameter('image_topics').value
        self.arm_state_topic = self.get_parameter('arm_state_topic').value
        self.leader_command_topic = self.get_parameter('leader_command_topic').value
        self.velocity_topic = self.get_parameter('velocity_topic').value
        
        # Initialize TaskManager and internal state
        self.task_manager = TaskManager(data_directory)
        self.current_episode = None  # Will hold an EpisodeData instance when an episode is active
        self.state = "IDLE"          # Possible states: "IDLE", "TASK_ACTIVE", "EPISODE_ACTIVE"
        self.episode_start_time = None
        
        # Latest sensor data variables
        self.latest_raw_image = None
        self.latest_processed_image = None
        self.latest_arm_state = None
        self.latest_leader_command = None
        self.latest_cmd_vel = None
        
        # Create subscribers for sensor topics
        self.create_subscription(Image, '/camera/image_raw', self.camera_raw_callback, 10)
        self.create_subscription(Image, '/camera/image_processed', self.camera_processed_callback, 10)
        self.create_subscription(JointState, '/arm/state', self.arm_state_callback, 10)
        self.create_subscription(Float64MultiArray, '/leader/command', self.leader_command_callback, 10)
        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, 10)
        
        # Create service servers
        self.new_task_srv = self.create_service(ManipulationTask, 'new_task', self.handle_new_task)
        self.new_episode_srv = self.create_service(Trigger, 'new_episode', self.handle_new_episode)
        self.save_episode_srv = self.create_service(Trigger, 'save_episode', self.handle_save_episode)
        self.cancel_episode_srv = self.create_service(Trigger, 'cancel_episode', self.handle_cancel_episode)
        self.end_task_srv = self.create_service(Trigger, 'end_task', self.handle_end_task)
        
        # Create a timer that will attempt to add sensor data as a new timestep at the specified frequency
        self.timer = self.create_timer(1.0 / self.data_frequency, self.timer_callback)
        
        self.get_logger().info("Recorder Node initialized in IDLE state.")

    # --------------- Sensor Callbacks ---------------
    def camera_raw_callback(self, msg: Image):
        self.latest_raw_image = msg

    def camera_processed_callback(self, msg: Image):
        self.latest_processed_image = msg

    def arm_state_callback(self, msg: JointState):
        self.latest_arm_state = msg

    def leader_command_callback(self, msg: Float64MultiArray):
        self.latest_leader_command = msg

    def cmd_vel_callback(self, msg: Twist):
        self.latest_cmd_vel = msg

    # --------------- Timer Callback ---------------
    def timer_callback(self):
        # Only record data if an episode is active
        if self.state != "EPISODE_ACTIVE" or self.current_episode is None:
            return

        # Ensure all sensor data are available
        if (self.latest_raw_image is None or 
            self.latest_processed_image is None or
            self.latest_arm_state is None or
            self.latest_leader_command is None or
            self.latest_cmd_vel is None):
            self.get_logger().warn("Incomplete sensor data; skipping timestep.")
            return
        
        # Build action data by combining leader command and velocity command
        action_data = []
        action_data.extend(self.latest_leader_command.data)  # leader_command is a list of floats
        twist = self.latest_cmd_vel  # cmd_vel (Twist) provides mobile velocity data
        action_data.extend([
            twist.linear.x, twist.linear.y, twist.linear.z,
            twist.angular.x, twist.angular.y, twist.angular.z
        ])
        
        # Get joint positions and velocities from the arm state message
        qpos = list(self.latest_arm_state.position)
        qvel = list(self.latest_arm_state.velocity)
        
        # Images: order is based on configuration (raw, then processed)
        images = [self.latest_raw_image, self.latest_processed_image]
        
        try:
            self.current_episode.add_timestep(action_data, qpos, qvel, images)
            self.get_logger().debug("Added timestep to current episode.")
        except ValueError as e:
            self.get_logger().error(f"Error adding timestep: {e}")

    # --------------- Service Handlers ---------------
    def handle_new_task(self, request, response):
        # If an episode is active, cancel it before starting a new task.
        if self.state == "EPISODE_ACTIVE":
            self.get_logger().warn("New task requested during an active episode; canceling current episode.")
            self.current_episode = None
            self.state = "TASK_ACTIVE"

        # Start a new task with TaskManager.
        self.task_manager.start_new_task(request.task_name, request.task_description, request.mobile_task)
        self.state = "TASK_ACTIVE"
        self.get_logger().info(f"New task '{request.task_name}' started.")
        response.success = True
        return response

    def handle_new_episode(self, request, response):
        if self.state != "TASK_ACTIVE":
            self.get_logger().error("Cannot start a new episode unless a task is active.")
            response.success = False
            response.message = "No active task."
            return response

        # Start a new episode: initialize EpisodeData (camera configuration auto-detected on first timestep)
        self.current_episode = EpisodeData()
        self.episode_start_time = time.time()
        self.state = "EPISODE_ACTIVE"
        self.get_logger().info("Episode started; now recording data.")
        response.success = True
        response.message = "Episode started."
        return response

    def handle_save_episode(self, request, response):
        if self.state != "EPISODE_ACTIVE" or self.current_episode is None:
            self.get_logger().error("No active episode to save.")
            response.success = False
            response.message = "No active episode."
            return response

        end_time = time.time()
        # Save the episode data and update metadata using TaskManager.
        self.task_manager.add_episode(
            self.current_episode,
            time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.episode_start_time)),
            time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(end_time))
        )
        self.get_logger().info("Episode saved successfully.")
        self.current_episode = None
        self.state = "TASK_ACTIVE"
        response.success = True
        response.message = "Episode saved."
        return response

    def handle_cancel_episode(self, request, response):
        if self.state != "EPISODE_ACTIVE" or self.current_episode is None:
            self.get_logger().error("No active episode to cancel.")
            response.success = False
            response.message = "No active episode."
            return response

        self.current_episode.clear()
        self.get_logger().info("Episode canceled; buffered data discarded.")
        self.current_episode = None
        self.state = "TASK_ACTIVE"
        response.success = True
        response.message = "Episode canceled."
        return response

    def handle_end_task(self, request, response):
        # If an episode is active, cancel it before ending the task.
        if self.state == "EPISODE_ACTIVE":
            self.get_logger().warn("Ending task during an active episode; canceling current episode first.")
            self.current_episode.clear()
            self.current_episode = None

        self.task_manager.end_task()
        self.state = "IDLE"
        self.get_logger().info("Task ended; recorder state set to IDLE.")
        response.success = True
        response.message = "Task ended."
        return response

def main(args=None):
    rclpy.init(args=args)
    node = RecorderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Recorder Node shutting down.")
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
