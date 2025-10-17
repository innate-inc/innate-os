#!/usr/bin/env python3
import traceback
import rclpy
from rclpy.node import Node
from rclpy.duration import Duration
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy
import threading
import json
import time
import cv2
import base64
import numpy as np
from rclpy.action import ActionClient
import math
import inspect
import types
import typing
from collections import deque
import tempfile
import os

# TF2 imports
# import tf2_ros # Reverted by user, then identified as unused by linter
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

# from tf2_ros import LookupException, ConnectivityException, ExtrapolationException

# from tf2_geometry_msgs import do_transform_pose # Reverted by user, then identified as unused by linter
# from geometry_msgs.msg import PoseStamped # Reverted by user, then identified as unused by linter

from brain_client.message_types import (
    InternalMessage,
    InternalMessageType,
    MessageIn,
    MessageInType,
    MessageOutType,
    MessageOut,
    VisionAgentOutput,
)
from brain_client.primitive_types import PrimitiveResult
from sensor_msgs.msg import CompressedImage
from geometry_msgs.msg import Twist
from std_msgs.msg import String
from sensor_msgs.msg import Image
from nav_msgs.msg import Odometry, OccupancyGrid
from geometry_msgs.msg import PoseWithCovarianceStamped
from brain_messages.srv import GetChatHistory
from brain_messages.action import ExecutePrimitive
from brain_messages.srv import GetAvailableDirectives
from brain_messages.srv import ResetBrain
from brain_messages.srv import SetDirectiveOnStartup
from std_srvs.srv import SetBool

from brain_client.ws_bridge import WSBridge
from brain_client.initializers import initialize_primitives, initialize_directives
from brain_client.tts_handler import TTSHandler


class BrainClientNode(Node):
    def __init__(self):
        super().__init__("brain_client_node")

        # Parameters
        self.declare_parameter("websocket_uri", "ws://localhost:8765")
        self.declare_parameter("token", "MY_HARDCODED_TOKEN")
        self.declare_parameter("image_topic", "/mars/main_camera/image/compressed")
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")

        # New parameters for optional depth processing:
        self.declare_parameter("depth_image_topic", "/camera/depth/image_raw")
        # New parameter for AMCL pose topic
        self.declare_parameter("amcl_pose_topic", "/amcl_pose")

        # New parameter for the map topic
        self.declare_parameter("map_topic", "/map")

        # New parameters for arm camera
        self.declare_parameter("arm_camera_image_topic", "/mars/arm/image_raw/compressed")
        self.declare_parameter("send_arm_camera_image", True)

        # Set to True if you wish to receive and forward depth images as well
        self.declare_parameter("send_depth", True)
        self.declare_parameter("odom_topic", "/odom")  # Removed odom_topic
        self.declare_parameter("use_odom_as_amcl_pose", False)
        self.declare_parameter("current_nav_mode_topic", "/nav/current_mode")

        # New parameters for camera FOV
        self.declare_parameter("vertical_fov", 60.0)
        self.declare_parameter("horizontal_resolution", 640)
        self.declare_parameter("vertical_resolution", 480)

        # New parameters for camera position
        self.declare_parameter("x_cam")  # Camera x position relative to robot base
        self.declare_parameter("height_cam")  # Camera height above ground

        # New parameter for pose image interval
        self.declare_parameter("pose_image_interval", 0.5)  # 0.5 seconds default

        # Parameter for logging complete vision agent output
        self.declare_parameter("log_everything", False)  # Default to False

        # --- New: Video buffering parameters ---
        self.declare_parameter(
            "video_buffer_duration_seconds", 2.0
        )  # Duration of video to send
        self.declare_parameter("video_fps", 10.0)  # FPS for the created video clip
        self.declare_parameter(
            "image_buffer_max_size", 60
        )  # Max images to store (e.g., 2s @ 30fps for a 30fps camera)

        # --- New: Flag to switch between image and video feed ---
        self.declare_parameter(
            "send_video_feed", False
        )  # Default to sending single image

        # --- New: Brain active state flag ---
        self.is_brain_active = False  # Brain starts active

        # --- New: Simulator mode parameter ---
        self.declare_parameter("simulator_mode", False)
        self.simulator_mode = self.get_parameter("simulator_mode").value

        # --- TTS parameters ---
        self.declare_parameter("cartesia_api_key", "")
        self.declare_parameter("cartesia_voice_id", "f786b574-daa5-4673-aa0c-cbe3e8534c02")

        self.get_logger().info(
            f"BrainClient running in {'simulator' if self.simulator_mode else 'real robot'} mode"
        )

        self.ws_uri = (
            self.get_parameter("websocket_uri").get_parameter_value().string_value
        )
        self.token = self.get_parameter("token").get_parameter_value().string_value
        self.image_topic = (
            self.get_parameter("image_topic").get_parameter_value().string_value
        )
        self.cmd_vel_topic = (
            self.get_parameter("cmd_vel_topic").get_parameter_value().string_value
        )
        self.depth_image_topic = (
            self.get_parameter("depth_image_topic").get_parameter_value().string_value
        )
        self.map_topic = (
            self.get_parameter("map_topic").get_parameter_value().string_value
        )
        self.amcl_pose_topic = (
            self.get_parameter("amcl_pose_topic").get_parameter_value().string_value
        )
        self.send_depth = (
            self.get_parameter("send_depth").get_parameter_value().bool_value
        )
        self.arm_camera_image_topic = (
            self.get_parameter("arm_camera_image_topic")
            .get_parameter_value()
            .string_value
        )
        self.send_arm_camera_image = (
            self.get_parameter("send_arm_camera_image").get_parameter_value().bool_value
        )
        self.odom_topic = (
            self.get_parameter("odom_topic").get_parameter_value().string_value
        )
        self.use_odom_as_amcl_pose = (
            self.get_parameter("use_odom_as_amcl_pose").get_parameter_value().bool_value
        )
        self.last_odom = None
        self.last_amcl_pose = None
        self.cur_nav_mode = None

        self.last_arm_camera = None  # Store the latest arm camera image
        self.odom_sub = self.create_subscription(
            Odometry, self.odom_topic, self.odom_callback, 10
        )
        self.current_nav_mode_topic = (
            self.get_parameter("current_nav_mode_topic")
            .get_parameter_value()
            .string_value
        )
        self.current_nav_mode_sub = self.create_subscription(
            String, self.current_nav_mode_topic, self.nav_mode_callback, 10
        )
        # self.map_agnostic_odom_pub = self.create_publisher(Odometry, '/map_agnost_odom', 10)

        # Create a timer to fetch the transform at 30 Hz
        self.transform_timer = self.create_timer(
            1.0 / 30.0, self.fetch_transform_callback
        )

        self.vertical_fov = (
            self.get_parameter("vertical_fov").get_parameter_value().double_value
        )
        self.horizontal_resolution = (
            self.get_parameter("horizontal_resolution")
            .get_parameter_value()
            .integer_value
        )
        self.vertical_resolution = (
            self.get_parameter("vertical_resolution")
            .get_parameter_value()
            .integer_value
        )
        self.horizontal_fov = math.degrees(
            2
            * math.atan(
                math.tan(math.radians(self.vertical_fov) / 2)
                * self.horizontal_resolution
                / self.vertical_resolution
            )
        )

        # Get camera position parameters
        self.x_cam = self.get_parameter("x_cam").get_parameter_value().double_value
        self.height_cam = (
            self.get_parameter("height_cam").get_parameter_value().double_value
        )

        # Initialize head position storage
        self.current_head_pitch = 0.0  # Default to horizontal (0 degrees)

        # Get pose image interval parameter
        self.pose_image_interval = (
            self.get_parameter("pose_image_interval").get_parameter_value().double_value
        )

        # Flag for logging complete vision agent output
        self.log_everything = (
            self.get_parameter("log_everything").get_parameter_value().bool_value
        )

        # --- Get video buffering parameters ---
        self.video_buffer_duration_seconds = (
            self.get_parameter("video_buffer_duration_seconds")
            .get_parameter_value()
            .double_value
        )
        self.video_fps = (
            self.get_parameter("video_fps").get_parameter_value().double_value
        )
        self.image_buffer_max_size = (
            self.get_parameter("image_buffer_max_size")
            .get_parameter_value()
            .integer_value
        )

        # --- Get video feed flag ---
        self.send_video_feed_enabled = (
            self.get_parameter("send_video_feed").get_parameter_value().bool_value
        )

        # image_buffer stores (timestamp_sec, image_numpy_array)
        self.image_buffer = deque(maxlen=self.image_buffer_max_size)

        self.get_logger().info(f"Starting BrainClientNode with ws_uri={self.ws_uri}")
        self.get_logger().info(f"Log everything mode: {self.log_everything}")

        # Directive on startup persistence file
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        self.directive_file = os.path.join(maurice_root, '.directive_on_startup')

        # Initialize TF2 buffer and listener
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        # Publishers, Subscribers, and Service
        self.last_image = None
        self.last_depth_image = None
        self.last_map = None  # Store the latest map data
        self.last_arm_camera = None  # Store the latest arm camera image

        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )

        # RGB image subscription remains unchanged.
        self.image_sub = self.create_subscription(
            CompressedImage, self.image_topic, self.image_callback, image_qos
        )

        # Subscribe to the map topic
        self.map_sub = self.create_subscription(
            OccupancyGrid, self.map_topic, self.map_callback, 1
        )

        # Optionally subscribe to the depth image topic if required.
        if self.send_depth:
            # Assuming that the depth image is published as a sensor_msgs/Image
            # (which contains: header, height, width, encoding, is_bigendian, step, data)

            self.depth_image_sub = self.create_subscription(
                Image, self.depth_image_topic, self.depth_image_callback, 1
            )

        # Optionally subscribe to the arm camera image topic if required.
        if self.send_arm_camera_image:
            self.arm_camera_sub = self.create_subscription(
                CompressedImage,
                self.arm_camera_image_topic,
                self.arm_camera_image_callback,
                image_qos,  # Using the same QoS as the main camera
            )

        # Subscribe to AMCL pose topic
        amcl_pose_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
        )
        self.amcl_pose_sub = self.create_subscription(
            PoseWithCovarianceStamped,
            self.amcl_pose_topic,
            self.amcl_pose_callback,
            amcl_pose_qos,
        )

        # Subscribe to head position topic
        self.head_position_sub = self.create_subscription(
            String,
            "/mars/head/current_position",
            self.head_position_callback,
            10,
        )

        self.cmd_vel_pub = self.create_publisher(Twist, self.cmd_vel_topic, 10)

        self.chat_history = []
        
        # Wait for input_manager to be ready (optional but recommended)
        self._wait_for_input_manager()
        
        self.chat_in_sub = self.create_subscription(
            String, "/brain/chat_in", self.chat_in_callback, 10
        )
        # Subscribe to custom input from input_manager
        self.custom_input_sub = self.create_subscription(
            String, "/input_manager/custom", self.custom_input_callback, 10
        )
        # Publisher to tell input_manager which inputs should be active
        self.active_inputs_pub = self.create_publisher(String, "/input_manager/active_inputs", 10)
        self.chat_out_pub = self.create_publisher(String, "/brain/chat_out", 10)
        self.tts_status_pub = self.create_publisher(String, "/tts/is_playing", 10)
        self.get_chat_history_srv = self.create_service(
            GetChatHistory, "/brain/get_chat_history", self.handle_get_chat_history
        )

        # Create service for setting logging configuration
        self.set_logging_srv = self.create_service(
            SetBool, "/brain/set_logging_config", self.handle_set_logging_config
        )

        # Create service for resetting the brain
        self.reset_srv = self.create_service(
            ResetBrain, "/brain/reset_brain", self.handle_reset_brain
        )

        # --- New: Service for activating/deactivating the brain ---
        self.set_brain_active_srv = self.create_service(
            SetBool, "/brain/set_brain_active", self.handle_set_brain_active
        )

        # Initialize TTS handler (after tts_status_pub is created)
        cartesia_api_key = self.get_parameter("cartesia_api_key").get_parameter_value().string_value
        cartesia_voice_id = self.get_parameter("cartesia_voice_id").get_parameter_value().string_value
        self.tts_handler = TTSHandler(cartesia_api_key, self.get_logger(), cartesia_voice_id, self.tts_status_pub)
        if self.tts_handler.is_available():
            self.get_logger().info(f"🗣️ Text-to-speech enabled with Cartesia (Voice ID: {cartesia_voice_id})")
        else:
            self.get_logger().info("🔇 Text-to-speech disabled (no API key provided)")

        self.exit_event = threading.Event()
        self.ready_for_image = False

        # New variables for pose image timer
        self.pose_image_timer = None
        self.pose_image_started = False

        self.agent_timer = self.create_timer(0.1, self.agent_loop_callback)

        self.ws_bridge = WSBridge(
            self, incoming_topic="ws_messages", outgoing_topic="ws_outgoing"
        )
        self.ws_bridge.register_handler(
            MessageOutType.READY_FOR_IMAGE, self._handle_ready_for_image
        )
        self.ws_bridge.register_handler(
            MessageOutType.VISION_AGENT_OUTPUT, self._handle_vision_agent_output
        )
        self.ws_bridge.register_handler(MessageOutType.CHAT_OUT, self._handle_chat_out)
        self.ws_bridge.register_handler(
            MessageOutType.PRIMITIVES_AND_DIRECTIVE_REGISTERED,
            self._handle_primitives_and_directive_registered,
        )

        for _ in range(10):
            self.ws_bridge.send_message(
                InternalMessage(type=InternalMessageType.READY_FOR_CONNECTION)
            )
            time.sleep(1.0)

        # Initialize primitives - query from primitive_execution_action_server
        self.primitives_dict = self._query_available_primitives()
        if not self.primitives_dict:
            self.get_logger().warn("No primitives available from primitive_execution_action_server, using fallback local loading")
            self.primitives_dict = initialize_primitives(self.get_logger(), self.simulator_mode)
        
        self.directives, self.current_directive = initialize_directives(self.get_logger(), self.primitives_dict)

        # Load startup directive from file
        startup_directive = self.load_startup_directive()
        if startup_directive and startup_directive in self.directives:
            self.current_directive = self.directives[startup_directive]
            self.get_logger().info(f"Applied startup directive: {startup_directive}")
            # Auto-activate brain if a startup directive is configured
            if not self.is_brain_active:
                self.is_brain_active = True
                self.get_logger().info(
                    "\033[1;92m[BrainClient] Auto-activating brain because startup directive is configured\033[0m"
                )
        elif startup_directive:
            self.get_logger().warn(
                f"Startup directive '{startup_directive}' not found in available directives. "
                f"Available: {list(self.directives.keys())}. Using default."
            )

        self.primitive_running = None
        # Add a variable to store the current goal handle
        self._goal_handle = None

        # Add a subscription to change directive
        self.directive_sub = self.create_subscription(
            String, "/brain/set_directive", self.set_directive_callback, 10
        )
        


        # Create service to get available directives
        self.get_directives_srv = self.create_service(
            GetAvailableDirectives,
            "/brain/get_available_directives",
            self.handle_get_available_directives,
        )

        # Create the primitive execution action client once in the init.
        self.primitive_action_client = ActionClient(
            self, ExecutePrimitive, "execute_primitive"
        )

        # Flag to keep track of primitive registration status
        self.primitives_registered = False
        self._pending_next_task = None

        # After initializing the primitive_action_client
        # Register the primitives with the server
        self.register_primitives_and_directive()

        self.get_logger().info(
            "\033[1;92m[BrainClient] BrainClientNode initialized\033[0m"
        )

    def _query_available_primitives(self):
        """
        Query available primitives from primitive_execution_action_server.
        Returns a dict of primitive names mapped to their metadata (for directive validation and registration).
        """
        from brain_messages.srv import GetAvailablePrimitives
        
        # Create service client
        client = self.create_client(GetAvailablePrimitives, '/brain/get_available_primitives')
        
        # Wait for service to be available
        self.get_logger().info("Waiting for /brain/get_available_primitives service...")
        if not client.wait_for_service(timeout_sec=10.0):
            self.get_logger().error("Timeout waiting for /brain/get_available_primitives service")
            return {}
        
        # Call service
        request = GetAvailablePrimitives.Request()
        future = client.call_async(request)
        
        # Wait for response
        rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)
        
        if not future.done():
            self.get_logger().error("Service call timeout")
            return {}
        
        try:
            response = future.result()
            primitives_list = json.loads(response.primitives_json)
            
            # Store the full primitives list for later use in registration
            self.primitives_metadata_list = primitives_list
            
            # Create a simple dict with primitive instances (mock objects with metadata)
            primitives_dict = {}
            for prim in primitives_list:
                # Create a mock primitive object that has guidelines methods
                class MockPrimitive:
                    def __init__(self, metadata):
                        self.metadata = metadata
                    def guidelines(self):
                        return self.metadata.get('guidelines', '')
                    def guidelines_when_running(self):
                        return self.metadata.get('guidelines_when_running', '')
                
                primitives_dict[prim['name']] = MockPrimitive(prim)
            
            # Log validation status
            learned_count = sum(1 for prim in primitives_list if prim.get('type') == 'learned')
            replay_count = sum(1 for prim in primitives_list if prim.get('type') == 'replay')
            code_count = sum(1 for prim in primitives_list if prim.get('type') == 'code')
            
            self.get_logger().info(f"Loaded {len(primitives_dict)} validated primitives from service: {list(primitives_dict.keys())}")
            self.get_logger().info(f"Primitive types: {code_count} code, {learned_count} learned, {replay_count} replay")
            
            return primitives_dict
            
        except Exception as e:
            self.get_logger().error(f"Error parsing primitives service response: {e}")
            return {}

    def _wait_for_input_manager(self, timeout_sec=5.0):
        """
        Wait for input_manager_node to be available.
        This ensures proper startup ordering when launched separately.
        """
        self.get_logger().info("⏳ Waiting for input_manager_node...")
        start_time = self.get_clock().now()
        
        while (self.get_clock().now() - start_time).nanoseconds / 1e9 < timeout_sec:
            # Check if input_manager_node exists in the node graph
            node_names = self.get_node_names()
            if 'input_manager_node' in node_names:
                self.get_logger().info("✅ input_manager_node is ready")
                return True
            time.sleep(0.1)
        
        self.get_logger().warning("⚠️ input_manager_node not found - continuing anyway")
        return False
    
    def chat_in_callback(self, msg: String):
        data = json.loads(msg.data)
        self.get_logger().info(f"\033[1;92mReceived brain/chat_in: {data}\033[0m")
        self.chat_history.append(data)
        outgoing_msg = MessageIn(type=MessageInType.CHAT_IN, payload={"text": data['text']})
        self.ws_bridge.send_message(outgoing_msg)
        self.get_logger().info(f"\033[1;92mSent MessageIn: {outgoing_msg}\033[0m")

    def custom_input_callback(self, msg: String):
        """Handle custom input data from input_manager."""
        try:
            import json
            data = json.loads(msg.data)
            self.get_logger().info(f"\033[1;94mReceived custom input from {data.get('input_device', 'unknown')}\033[0m")
            outgoing_msg = MessageIn(type=MessageInType.CUSTOM_INPUT, payload=data)
            self.ws_bridge.send_message(outgoing_msg)
        except Exception as e:
            self.get_logger().error(f"Error processing custom input: {e}")

    def activate_directive_inputs(self):
        """
        Publish the list of input devices that should be active based on current directive.
        The input_manager_node subscribes to this and activates/deactivates inputs accordingly.
        """
        if not self.current_directive:
            return
        
        try:
            import json
            required_inputs = self.current_directive.get_inputs()
            msg = String()
            msg.data = json.dumps({"inputs": required_inputs})
            self.active_inputs_pub.publish(msg)
            
            if required_inputs:
                self.get_logger().info(f"🔌 Activated inputs for directive '{self.current_directive.name}': {required_inputs}")
            else:
                self.get_logger().debug(f"No inputs required for directive '{self.current_directive.name}'")
        except Exception as e:
            self.get_logger().error(f"Error activating directive inputs: {e}")

    def custom_input_callback(self, msg: String):
        """Handle custom input data from input_manager."""
        try:
            import json
            data = json.loads(msg.data)
            self.get_logger().info(f"\033[1;94mReceived custom input from {data.get('input_device', 'unknown')}\033[0m")
            outgoing_msg = MessageIn(type=MessageInType.CUSTOM_INPUT, payload=data)
            self.ws_bridge.send_message(outgoing_msg)
        except Exception as e:
            self.get_logger().error(f"Error processing custom input: {e}")

    def activate_directive_inputs(self):
        """
        Publish the list of input devices that should be active based on current directive.
        The input_manager_node subscribes to this and activates/deactivates inputs accordingly.
        """
        if not self.current_directive:
            return
        
        try:
            import json
            required_inputs = self.current_directive.get_inputs()
            msg = String()
            msg.data = json.dumps({"inputs": required_inputs})
            self.active_inputs_pub.publish(msg)
            
            if required_inputs:
                self.get_logger().info(f"🔌 Activated inputs for directive '{self.current_directive.name}': {required_inputs}")
            else:
                self.get_logger().debug(f"No inputs required for directive '{self.current_directive.name}'")
        except Exception as e:
            self.get_logger().error(f"Error activating directive inputs: {e}")

    def handle_get_chat_history(self, request, response):
        self.get_logger().debug(
            f"\033[1;94mReceived get_chat_history request. History: {self.chat_history}\033[0m"
        )
        response.history = json.dumps(self.chat_history)
        return response

    def handle_set_logging_config(self, request, response):
        """
        Service handler for setting the logging configuration.
        Sets the log_everything flag based on the request data.
        """
        self.log_everything = request.data
        self.get_logger().info(
            f"\033[1;92m[BrainClient] Set logging configuration: "
            f"log_everything={self.log_everything}\033[0m"
        )
        response.success = True
        response.message = (
            f"Logging configuration set: log_everything={self.log_everything}"
        )
        return response

    def image_callback(self, msg: CompressedImage):
        try:
            # self.get_logger().info(f"\033[1;92m[BrainClient] Received image_callback\033[0m")
            np_arr = np.frombuffer(msg.data, np.uint8)
            # self.last_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            decoded_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if decoded_image is not None:
                self.last_image = decoded_image  # Keep updating for other uses (e.g. pose_image_callback)
                current_time_sec = self.get_clock().now().nanoseconds / 1e9
                self.image_buffer.append((current_time_sec, decoded_image))
            else:
                self.get_logger().warn(
                    "Failed to decode image in image_callback, decoded_image is None."
                )
        except Exception as e:
            self.get_logger().error(
                f"Failed to decode compressed image or add to buffer: {e}"
            )

    def arm_camera_image_callback(self, msg: CompressedImage):
        try:
            np_arr = np.frombuffer(msg.data, np.uint8)
            self.last_arm_camera = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            # self.get_logger().info(f"\033[1;92m[BrainClient] Received arm_camera_image_callback\033[0m")
        except Exception as e:
            self.get_logger().error(
                f"Failed to decode compressed arm camera image: {e}"
            )

    def depth_image_callback(self, msg):
        try:
            # This callback assumes a sensor_msgs/Image message for the depth channel.
            # Determine the correct numpy dtype based on the image encoding.
            if msg.encoding in ["16UC1", "mono16"]:
                dtype = np.uint16
            elif msg.encoding in ["32FC1", "mono32"]:
                dtype = np.float32
            else:
                # Fallback to uint8 if the encoding is unexpected.
                self.get_logger().warn(
                    f"Unexpected depth image encoding: {msg.encoding}, "
                    f"defaulting to uint8"
                )
                dtype = np.uint8
            depth_array = np.frombuffer(msg.data, dtype=dtype)
            depth_array = depth_array.reshape((msg.height, msg.width))
            self.last_depth_image = depth_array
        except Exception as e:
            self.get_logger().error(f"Failed to decode depth image: {e}")

    def fetch_transform_callback(self):
        try:
            # In mapfree mode there is no global map frame; skip map->base_link TF lookups
            if getattr(self, "cur_nav_mode", None) == "mapfree":
                return
            robot_base_frame = "base_link"  # The frame whose pose we want
            map_frame = "map"  # The frame in which we want the pose expressed
            when = rclpy.time.Time()

            if self.tf_buffer.can_transform(
                map_frame,  # Target frame ("map")
                robot_base_frame,  # Source frame ("base_link")
                when,
                timeout=Duration(seconds=0.1),  # Short timeout for can_transform
            ):
                transform_stamped = self.tf_buffer.lookup_transform(
                    map_frame,  # Target frame ("map")
                    robot_base_frame,  # Source frame ("base_link")
                    when,
                    timeout=Duration(
                        seconds=0.1
                    ),  # Shorter timeout as can_transform likely passed
                )

                # Create an Odometry message to store the pose (or a simpler structure if preferred)
                # For now, let's try to populate an Odometry message similarly to before,
                # but using the transform directly.
                # This part might need adjustment based on how self.last_odom is used elsewhere.
                odom_msg = Odometry()
                odom_msg.header.stamp = (
                    self.get_clock().now().to_msg()
                )  # Use current time for the header
                odom_msg.header.frame_id = map_frame  # "map"
                odom_msg.child_frame_id = robot_base_frame  # "base_link"

                odom_msg.pose.pose.position.x = (
                    transform_stamped.transform.translation.x
                )
                odom_msg.pose.pose.position.y = (
                    transform_stamped.transform.translation.y
                )
                odom_msg.pose.pose.position.z = (
                    transform_stamped.transform.translation.z
                )
                odom_msg.pose.pose.orientation = transform_stamped.transform.rotation
                # Covariance and Twist are not directly available from lookup_transform
                # and might need to be handled differently or zeroed out if not critical.
                # For simplicity, let's zero them or leave them default for now.

                self.last_odom = odom_msg

                # Calculate yaw (theta) from quaternion - this theta_degrees is not used here
                ori = odom_msg.pose.pose.orientation
                siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
                cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
                theta_radians = math.atan2(siny_cosp, cosy_cosp)
                theta_degrees = math.degrees(theta_radians)
            else:
                self.get_logger().warn(
                    f"Could not get transform from '{robot_base_frame}' to "
                    f"{map_frame}' at time {when.nanoseconds / 1e9:.3f}s. Waiting..."
                )
        except TransformException as ex:
            # Adjusted error message to reflect the intended transformation
            self.get_logger().error(
                f"TransformException looking up transform from "
                f"'{robot_base_frame}' to '{map_frame}': {ex}"
            )
        except Exception as e:
            self.get_logger().error(
                f"Error in fetch_transform_callback: {e}, " f"{traceback.format_exc()}"
            )

    def map_callback(self, msg: OccupancyGrid):
        """Store the latest map data."""
        self.last_map = msg

    def amcl_pose_callback(self, msg: PoseWithCovarianceStamped):
        """Store the latest AMCL pose data."""
        self.last_amcl_pose = msg
        self.get_logger().debug(
            f"Received amcl_pose at X: {msg.pose.pose.position.x}, Y: {msg.pose.pose.position.y}"
        )

    def odom_callback(self, msg: Odometry):
        """Store the latest odometry data."""
        self.last_odom = msg
        self.get_logger().debug(
            f"Received odom at X: {msg.pose.pose.position.x}, Y: {msg.pose.pose.position.y}"
        )

        # If we're in the SIM, we can use the odom pose as the amcl pose.
        if self.use_odom_as_amcl_pose:
            self.last_amcl_pose = self.last_odom

    def nav_mode_callback(self, msg: String):
        """Store the Current navigation mode. mapfree, mapping, navigation"""
        self.cur_nav_mode = msg.data
        self.get_logger().debug(f"Current Navigation Mode is {self.cur_nav_mode}")
        # if msg.data == 'mapfree':
        #     if self.last_odom is None:
        #         self.get_logger().warn("No odometry received yet. Cannot publish map-agnostic odometry.")
        #         return
        #     self.last_odom.pose.covariance = [1e4] * 36
        # self.map_agnostic_odom_pub.publish(self.last_odom)

    def head_position_callback(self, msg: String):
        """Store the latest head position data."""
        try:
            position_data = json.loads(msg.data)
            self.current_head_pitch = position_data.get("current_position", 0.0)
            self.get_logger().debug(
                f"Received head pitch: {self.current_head_pitch} degrees"
            )
        except (json.JSONDecodeError, KeyError) as e:
            self.get_logger().error(f"Failed to parse head position data: {e}")
            self.current_head_pitch = 0.0  # Default to horizontal

    def _handle_ready_for_image(self, msg):
        self.get_logger().info("Received READY_FOR_IMAGE; setting flag.")
        self.ready_for_image = True

        # Start the pose image timer after the first ready_for_image, if not already started
        # and if the brain is active
        if (
            self.is_brain_active
            and not self.pose_image_started
            and self.primitives_registered
        ):
            self.get_logger().info("Starting regular pose image transmission")
            self.pose_image_started = True
            self.pose_image_timer = self.create_timer(
                self.pose_image_interval, self.pose_image_callback
            )

    def pose_image_callback(self):
        """Send pose images regularly with the robot's current position."""
        try:
            # Skip if brain is not active
            if not self.is_brain_active:
                self.get_logger().debug(
                    "Brain not active, skipping pose_image_callback."
                )
                return

            # Skip if no valid image or odometry data is available
            if self.last_image is None or self.last_odom is None:
                self.get_logger().warn(
                    "Skipping pose_image: No image or odom/amcl_pose."
                )
                return
            # In simulator mode, allow pose_image_callback even if nav_mode is None
            if (
                self.cur_nav_mode is None or self.cur_nav_mode == "mapping"
            ) and not self.simulator_mode:
                self.get_logger().warn(
                    f"Skipping pose_image_callback as navigation mode is {self.cur_nav_mode}"
                )
                return

            # Use self.last_amcl_pose if available, otherwise fallback to self.last_odom (or skip)
            current_pose_source = None
            # try:
            #     transform = self.tf_buffer.lookup_transform(
            #         target_frame='map',
            #         source_frame='base_link',
            #         time=rclpy.time.Time(),
            #         timeout=rclpy.time.Duration(seconds=1.0),
            #     )

            # except Exception as err:
            #     # self.get_logger().error(f"Error in pose_image_callback: {err}")
            #     self.get_logger().info(f"Transform not ready {err}")
            #     return

            if self.cur_nav_mode == "navigation" and self.last_amcl_pose:
                current_pose_source = self.last_amcl_pose.pose
                # self.get_logger().debug("Using amcl_pose for pose_image_callback")
            elif (
                self.last_odom
            ):  # Fallback, though ideally amcl_pose is what we want for covariance
                if self.cur_nav_mode == "mapfree":
                    self.last_odom.pose.covariance = [1e4] * 36
                current_pose_source = self.last_odom.pose

                # Only show warning in real robot mode, not in simulator where odom is expected
                if not self.simulator_mode:
                    self.get_logger().warn(
                        "Falling back to last_odom for pose_image_callback (no covariance will be sent)."
                    )
            else:
                self.get_logger().warn(
                    f"Skipping pose_image: No amcl_pose or odom available at navigation mode {self.cur_nav_mode}"
                )
                return

            # Compress the image as JPEG
            encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
            success, encoded_img = cv2.imencode(".jpg", self.last_image, encode_params)
            if not success:
                self.get_logger().error("Failed to encode image for pose_image")
                return

            # # Extract position and orientation data
            pos = current_pose_source.pose.position  # Use selected source
            ori = current_pose_source.pose.orientation  # Use selected source

            # pos = transform.transform.translation
            # ori = transform.transform.rotation

            # Compute yaw from quaternion
            siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
            cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
            theta = math.atan2(siny_cosp, cosy_cosp)
            self.pos_data_xyt = (pos.x, pos.y, theta)
            # self.get_logger().info(f"Are you working? {pos.x}  {pos.y} and theta {theta}")

            # Create and send the pose_image message
            # No user_token is needed as the server will use the connection_id
            payload = {
                "image": base64.b64encode(encoded_img.tobytes()).decode("utf-8"),
                "x": pos.x,
                "y": pos.y,
                "theta": theta,
                "camera_info": {
                    "horizontal_fov": self.horizontal_fov,
                    "vertical_fov": self.vertical_fov,
                    "pitch_deg": self.current_head_pitch,
                    "x_cam": self.x_cam,
                    "height_cam": self.height_cam,
                },
            }

            # Add covariance if available (i.e., from amcl_pose)
            if hasattr(current_pose_source, "covariance"):
                payload["cov_x"] = current_pose_source.covariance[0]  # Variance of x
                payload["cov_y"] = current_pose_source.covariance[7]  # Variance of y
                payload["cov_yaw"] = current_pose_source.covariance[
                    35
                ]  # Variance of yaw (renamed from cov_angle_z)

            pose_image_msg = MessageIn(type=MessageInType.POSE_IMAGE, payload=payload)
            self.ws_bridge.send_message(pose_image_msg)
            self.get_logger().debug(
                f"Sent pose_image at position ({pos.x:.2f}, {pos.y:.2f}, {theta:.2f})"
            )
        except Exception as e:
            self.get_logger().error(f"Error in pose_image_callback: {e}")

    def _handle_vision_agent_output(self, msg):
        try:
            self.get_logger().info("[BrainClient] Received VisionAgentOutput")

            if not self.is_brain_active:
                self.get_logger().warn(
                    "\033[93m[BrainClient] Brain is not active. Skipping VisionAgentOutput.\033[0m"
                )
                return

            if not self.primitives_registered:
                self.get_logger().warn(
                    "\033[93m[BrainClient] Primitives not registered. Skipping VisionAgentOutput.\033[0m"
                )
                return

            # HOTFIX: If there's a msg.payload["next_task"] with a "name" field, replace it with "type".
            if "next_task" in msg.payload and msg.payload["next_task"] is not None:
                if "name" in msg.payload["next_task"]:
                    msg.payload["next_task"]["type"] = msg.payload["next_task"]["name"]
                    msg.payload["next_task"].pop("name", None)

            payload = VisionAgentOutput.model_validate(msg.payload)

            # If log_everything is enabled, send the complete vision agent output as a chat message
            if self.log_everything:
                # Convert the complete payload to a JSON string for the chat message
                complete_output_text = json.dumps(msg.payload)
                chat_entry = {
                    "sender": "vision_agent_output",
                    "text": complete_output_text,
                    "timestamp": time.time(),
                }
                self.chat_history.append(chat_entry)
                out_msg = String(data=json.dumps(chat_entry))
                self.chat_out_pub.publish(out_msg)

            self.handle_vision_agent_output(payload)
        except Exception as e:
            self.get_logger().error(
                f"Error processing vision output: {e}. Traceback: {traceback.format_exc()}"
            )

    def _handle_chat_out(self, msg, sender="robot"):
        text = msg.payload.get("text", "")
        chat_entry = {"sender": sender, "text": text, "timestamp": time.time()}
        self.chat_history.append(chat_entry)
        self.get_logger().debug(f"Received brain/chat_out: {chat_entry}")
        out_msg = String(data=json.dumps(chat_entry))
        self.chat_out_pub.publish(out_msg)
        
        # Generate speech for robot messages (but not thoughts or anticipation)
        if sender == "robot" and text and text.strip():
            self.tts_handler.speak_text_async(text)

    def handle_vision_agent_output(self, payload: VisionAgentOutput):
        execute_next_task_immediately = True

        if payload.stop_current_task:
            self.get_logger().info("\033[91m[BrainClient] Stop signal received.\033[0m")

            self.get_logger().info(f"Payload received: {payload}")

            # Cancel the current goal if it exists
            if self._goal_handle is not None:
                self.get_logger().info(
                    "\033[91m[BrainClient] Canceling current goal.\033[0m"
                )

                # Store the next task if it exists, prevent immediate execution
                if payload.next_task is not None:
                    self.get_logger().info(
                        f"Storing pending task: {payload.next_task.type}"
                    )
                    self._pending_next_task = payload.next_task
                    execute_next_task_immediately = False  # Don't execute now

                # Request cancellation
                cancel_future = self._goal_handle.cancel_goal_async()
                cancel_future.add_done_callback(self.cancel_response_callback)
            else:
                self.get_logger().info(
                    "\033[93m[BrainClient] Stop received but no goal handle active.\033[0m"
                )

        if payload.thoughts:
            chat_entry = MessageOut(
                type=MessageOutType.CHAT_OUT,
                payload={"text": payload.thoughts},
            )
            self._handle_chat_out(chat_entry, sender="robot_thoughts")

        if payload.to_tell_user:
            chat_entry = MessageOut(
                type=MessageOutType.CHAT_OUT,
                payload={"text": payload.to_tell_user},
            )
            self._handle_chat_out(chat_entry, sender="robot")

        if payload.anticipation:
            chat_entry = MessageOut(
                type=MessageOutType.CHAT_OUT,
                payload={"text": payload.anticipation},
            )
            self._handle_chat_out(chat_entry, sender="robot_anticipation")

        # Only execute the next task immediately if not stopping/pending
        if execute_next_task_immediately and payload.next_task is not None:
            # Ensure any previously pending task is cleared if we are executing a new one directly
            self._pending_next_task = None

            self.get_logger().info(
                f"\033[92m[BrainClient] Next task: {payload.next_task}\033[0m"
            )
            self.get_logger().info(
                f"Primitive task type: {payload.next_task.type}"
            )

            if payload.next_task.type in self.primitives_dict:
                self.send_primitive_goal(
                    payload.next_task.type,
                    payload.next_task.inputs,
                )
                status_msg = MessageIn(
                    type=MessageInType.PRIMITIVE_ACTIVATED,
                    payload={
                        "primitive_name": payload.next_task.type,
                        "primitive_id": payload.next_task.primitive_id,
                    },
                )
                self.ws_bridge.send_message(status_msg)
                self.primitive_running = {
                    "primitive_name": payload.next_task.type,
                    "primitive_id": payload.next_task.primitive_id,
                }
            else:
                self.get_logger().warn(
                    f"Unknown primitive type: {payload.next_task.type}"
                )
        elif not execute_next_task_immediately and payload.next_task is not None:
            self.get_logger().info(
                "\033[94m[BrainClient] Next task stored, waiting for cancellation to complete.\033[0m"
            )
        else:
            # Ensure pending task is cleared if no next task is given
            self._pending_next_task = None
            self.get_logger().info(
                "\033[94m[BrainClient] No next task provided or task is pending.\033[0m"
            )

    def agent_loop_callback(self):
        # This callback will send the RGB image and, if allowed, the depth image
        if not self.is_brain_active:
            self.get_logger().debug(
                "\033[93m[BrainClient] Brain not active. Skipping agent_loop_callback.\033[0m"
            )
            return

        if not self.primitives_registered:
            # If primitives are not yet registered, don't send images
            self.get_logger().info(
                "\033[93m[BrainClient] Primitives not registered. Skipping image callback.\033[0m"
            )
            return

        if self.ready_for_image and self.last_image is not None:
            self.get_logger().info(
                "\033[93m[BrainClient] Sending image callback.\033[0m"
            )
            try:
                # Select pose source based on navigation mode
                use_mapfree = (self.cur_nav_mode == "mapfree")
                pose_source = None  # Either AMCL (preferred) or ODOM (mapfree)
                if not use_mapfree and self.last_amcl_pose:
                    pose_source = ("amcl", self.last_amcl_pose.pose)
                elif use_mapfree and self.last_odom is not None:
                    # Inflate covariance in mapfree mode to communicate uncertainty
                    try:
                        cov = getattr(self.last_odom.pose, "covariance", None)
                        needs_set = cov is None
                        if not needs_set and hasattr(cov, "__len__"):
                            needs_set = len(cov) < 36
                        if needs_set:
                            self.last_odom.pose.covariance = [1e4] * 36
                    except Exception:
                        # If anything goes wrong, still attempt to proceed
                        pass
                    pose_source = ("odom", self.last_odom.pose)
                else:
                    self.get_logger().warn(
                        "\033[93m[BrainClient] No suitable pose source (amcl/odom). Skipping image callback.\033[0m"
                    )
                    return

                # Compress the RGB image as JPEG (70% quality) - User's addition
                encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
                success, encoded_img = cv2.imencode(
                    ".jpg", self.last_image, encode_params
                )
                if not success:
                    self.get_logger().error("Failed to encode RGB image")
                    self.ready_for_image = False
                    return
                rgb_b64 = base64.b64encode(encoded_img.tobytes()).decode("utf-8")

                # --- BEGIN IMAGE/VIDEO PROCESSING AND SENDING WITH TIMING ---
                processing_start_time = time.perf_counter()

                payload = {"image_b64": rgb_b64}  # Start with the always-encoded image
                log_message_label = "Image"

                if self.send_video_feed_enabled:
                    log_message_label = "Video"
                    # Grab images from the buffer that are within the desired time window
                    current_time_sec = self.get_clock().now().nanoseconds / 1e9
                    recent_frames_data = [
                        img
                        for ts, img in list(self.image_buffer)
                        if current_time_sec - ts <= self.video_buffer_duration_seconds
                    ]

                    if not recent_frames_data:
                        self.get_logger().warn(
                            "\033[93m[BrainClient] Video feed enabled, but no recent images in buffer. Sending only single image.\033[0m"
                        )
                        # Fallback to sending just the single image if buffer is empty for video
                    else:
                        video_b64 = None
                        temp_video_path = ""
                        try:
                            first_image = recent_frames_data[0]
                            height, width, _ = first_image.shape

                            if height == 0 or width == 0:
                                self.get_logger().error(
                                    f"Invalid image dimensions for video: {width}x{height}"
                                )
                                # Don't return, will send single image instead
                            else:
                                with tempfile.NamedTemporaryFile(
                                    suffix=".avi", delete=False
                                ) as tmpfile_obj:
                                    temp_video_path = tmpfile_obj.name

                                if not temp_video_path:
                                    self.get_logger().error(
                                        "Failed to create temporary file path for video."
                                    )
                                    # Don't return, will send single image instead
                                else:
                                    fourcc = cv2.VideoWriter_fourcc(
                                        *"MJPG"
                                    )  # Motion JPEG
                                    video_writer = cv2.VideoWriter(
                                        temp_video_path,
                                        fourcc,
                                        float(self.video_fps),
                                        (width, height),
                                    )

                                    if not video_writer.isOpened():
                                        self.get_logger().error(
                                            f"Failed to open VideoWriter for path: {temp_video_path} with res {width}x{height}, FPS {self.video_fps}"
                                        )
                                        if os.path.exists(temp_video_path):
                                            os.remove(temp_video_path)

                                        # Don't return, will send single image instead
                                    else:
                                        for frame_img in recent_frames_data:
                                            if (
                                                frame_img is not None
                                                and frame_img.shape[0] == height
                                                and frame_img.shape[1] == width
                                            ):
                                                video_writer.write(frame_img)
                                            elif frame_img is None:
                                                self.get_logger().warn(
                                                    "Skipping None frame in video creation."
                                                )
                                            else:
                                                self.get_logger().warn(
                                                    f"Skipping frame with mismatched dimensions: {frame_img.shape} vs {height}x{width}"
                                                )

                                        video_writer.release()

                                        if (
                                            os.path.exists(temp_video_path)
                                            and os.path.getsize(temp_video_path) > 0
                                        ):
                                            with open(temp_video_path, "rb") as f:
                                                video_bytes = f.read()
                                            video_b64 = base64.b64encode(
                                                video_bytes
                                            ).decode("utf-8")
                                        else:
                                            self.get_logger().error(
                                                f"Temporary video file is empty or does not exist after writing: {temp_video_path}"
                                            )
                                            if os.path.exists(temp_video_path):
                                                os.remove(temp_video_path)  # Clean up
                        except Exception as e:
                            self.get_logger().error(
                                f"Error during video creation: {e}, Traceback: {traceback.format_exc()}"
                            )
                        finally:
                            if temp_video_path and os.path.exists(temp_video_path):
                                os.remove(temp_video_path)

                        if video_b64:
                            payload["video_b64"] = video_b64
                            payload["video_format"] = "avi_mjpeg"
                        else:
                            self.get_logger().warn(
                                "Video creation failed or resulted in no data. Sending only single image."
                            )

                # --- Add other payload components (FOV, depth, map, robot_coords, arm_camera) ---
                # Include the horizontal and vertical FOV of the camera
                payload["camera_info"] = {
                    "horizontal_fov": self.horizontal_fov,
                    "vertical_fov": self.vertical_fov,
                    "pitch_deg": self.current_head_pitch,
                    "x_cam": self.x_cam,
                    "height_cam": self.height_cam,
                }

                # Optionally include depth data if enabled and available.
                if self.send_depth and self.last_depth_image is None:
                    self.get_logger().warn(
                        "\033[93m[BrainClient] No depth image available.\033[0m"
                    )
                    return
                elif self.send_depth and self.last_depth_image is not None:
                    depth_frame = self.last_depth_image
                    depth_data = depth_frame.tobytes()
                    if depth_frame.dtype == np.uint16:
                        encoding = "16UC1"
                        bytes_per_pixel = 2
                    elif depth_frame.dtype == np.float32:
                        encoding = "32FC1"
                        bytes_per_pixel = 4
                    else:
                        encoding = "8UC1"
                        bytes_per_pixel = 1
                    depth_payload = {
                        "height": int(depth_frame.shape[0]),
                        "width": int(depth_frame.shape[1]),
                        "encoding": encoding,
                        "is_bigendian": 0,
                        "step": int(depth_frame.shape[1] * bytes_per_pixel),
                        "data": base64.b64encode(depth_data).decode("utf-8"),
                    }
                    payload["depth"] = depth_payload

                # Include map data if available; in mapfree mode map may be absent
                if self.last_map is not None:
                    # Create a simplified map payload with only the necessary information
                    map_data = self.last_map.data

                    # Convert quaternion to yaw
                    ori = self.last_map.info.origin.orientation
                    siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
                    cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
                    yaw = math.atan2(siny_cosp, cosy_cosp)

                    map_payload = {
                        "resolution": self.last_map.info.resolution,
                        "width": self.last_map.info.width,
                        "height": self.last_map.info.height,
                        "origin_x": self.last_map.info.origin.position.x,
                        "origin_y": self.last_map.info.origin.position.y,
                        "origin_z": self.last_map.info.origin.position.z,
                        "origin_yaw": yaw,
                        "frame_id": self.last_map.header.frame_id,
                        "data": base64.b64encode(np.array(map_data).tobytes()).decode(
                            "utf-8"
                        ),
                    }
                    payload["map"] = map_payload
                    self.get_logger().debug("Including map data in image message")
                else:
                    if not use_mapfree:
                        self.get_logger().warn(
                            "\033[93m[BrainClient] No map data available. Skipping image callback.\033[0m"
                        )
                        return
                    # In mapfree, proceed without a map

                # Include robot coordinates (use selected pose source)
                pose_msg = pose_source[1]  # PoseWithCovariance or Odometry.pose
                pos = pose_msg.pose.position
                ori = pose_msg.pose.orientation
                siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
                cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
                theta = math.atan2(siny_cosp, cosy_cosp)
                robot_coords_payload = {
                    "x": pos.x,
                    "y": pos.y,
                    "z": pos.z,
                    "theta": theta,
                    # Frame depends on pose source; use "map" for AMCL and pose.header.frame_id if available for ODOM
                    "frame_id": (
                        self.last_amcl_pose.header.frame_id
                        if pose_source[0] == "amcl"
                        else (getattr(self.last_odom, "header", None).frame_id if hasattr(self.last_odom, "header") else "odom")
                    ),
                }
                # Add covariance if available
                cov = getattr(pose_msg, "covariance", None)
                if cov is not None:
                    try:
                        # Ensure we can index expected positions
                        if not hasattr(cov, "__len__") or len(cov) >= 36:
                            robot_coords_payload["cov_x"] = cov[0]
                            robot_coords_payload["cov_y"] = cov[7]
                            robot_coords_payload["cov_yaw"] = cov[35]
                    except Exception:
                        pass
                payload["robot_coords"] = robot_coords_payload

                # Optionally include arm camera image if enabled and available
                if self.send_arm_camera_image and self.last_arm_camera is not None:
                    # Compress the arm camera image as JPEG (70% quality)
                    arm_encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
                    arm_success, arm_encoded_img = cv2.imencode(
                        ".jpg", self.last_arm_camera, arm_encode_params
                    )
                    if arm_success:
                        arm_rgb_b64 = base64.b64encode(
                            arm_encoded_img.tobytes()
                        ).decode("utf-8")
                        payload["additional_camera"] = {
                            "image_b64": arm_rgb_b64,
                            "camera_type": "arm_wrist",
                        }
                        self.get_logger().debug("Including arm camera image in message")
                    else:
                        self.get_logger().error(
                            "Failed to encode arm camera image for agent loop"
                        )
                elif self.send_arm_camera_image and self.last_arm_camera is None:
                    self.get_logger().warn(
                        "\033[93m[BrainClient] No arm camera image available (send_arm_camera_image is True).\\033[0m"
                    )
                    return

                # Build and send the message
                image_msg = MessageIn(type=MessageInType.IMAGE, payload=payload)
                self.ws_bridge.send_message(image_msg)

                processing_end_time = time.perf_counter()
                self.get_logger().info(
                    f"{log_message_label} processing and sending took {processing_end_time - processing_start_time:.4f} seconds."
                )
                # --- END IMAGE/VIDEO PROCESSING AND SENDING WITH TIMING ---

                # Reset flags so we do not resend the same images
                self.ready_for_image = False
                self.last_depth_image = None  # Depth is reset after sending.
                self.last_arm_camera = None  # Arm camera is reset after sending.
            except Exception as e:
                self.get_logger().error(f"Error in agent_loop_callback: {e}")
                # It's important to re-raise the exception if we are not handling it here,
                # otherwise, the error might be silently ignored and lead to difficult debugging.
                raise  # Re-raise the caught exception

    def send_primitive_goal(self, task_type, inputs):
        goal_msg = ExecutePrimitive.Goal()
        goal_msg.primitive_type = task_type

        # Inputs are now only the direct arguments for the primitive's execute method.
        # Robot state injection is handled by the PrimitiveExecutionActionServer.
        goal_msg.inputs = json.dumps(inputs if inputs is not None else {})

        self.get_logger().info(
            f"Sending goal for primitive: {goal_msg.primitive_type} with inputs: {goal_msg.inputs}"
        )
        if not self.primitive_action_client.wait_for_server(timeout_sec=1.0):
            self.get_logger().error("Primitive execution action server not available!")
            return

        send_goal_future = self.primitive_action_client.send_goal_async(
            goal_msg,
            feedback_callback=self.primitive_feedback_callback,  # Add feedback callback
        )
        send_goal_future.add_done_callback(self.goal_response_callback)

    def primitive_feedback_callback(self, feedback_msg_wrapper):
        """Handles feedback messages from the primitive execution action server."""
        try:
            # The actual feedback message is wrapped, access it via the 'feedback' attribute.
            # And the string message itself is in another 'feedback' field within that.
            feedback_text = feedback_msg_wrapper.feedback.feedback
            self.get_logger().info(f"Received primitive feedback: {feedback_text}")

            # Send this feedback to the server
            feedback_msg = MessageIn(
                type=MessageInType.PRIMITIVE_FEEDBACK,
                payload={"feedback": feedback_text},
            )
            self.ws_bridge.send_message(feedback_msg)
        except AttributeError:
            self.get_logger().error(
                f"Error accessing feedback text. Received feedback structure: {feedback_msg_wrapper}"
            )
        except Exception as e:
            self.get_logger().error(f"Error in primitive_feedback_callback: {e}")

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info("Primitive execution goal rejected.")
            return
        # Store the goal handle for potential cancellation, using the same naming as in the example
        self._goal_handle = goal_handle
        self.get_logger().info("Primitive execution goal accepted.")
        get_result_future = goal_handle.get_result_async()
        get_result_future.add_done_callback(self.get_result_callback)

    def cancel_response_callback(self, future):
        """Handle the response from a cancel_goal_async request."""
        # This is not REALLY necessary, but for logging purposes it's nice to have.
        cancel_response = future.result()

        self.get_logger().info("\033[92m[BrainClient] Cancel response received.\033[0m")

        # Check if any goals were canceled, following the ROS2 example
        if (
            hasattr(cancel_response, "goals_canceling")
            and len(cancel_response.goals_canceling) > 0
        ):
            self.get_logger().info("Goal cancellation accepted.")
        else:
            self.get_logger().error("Goal cancellation rejected.")

    def get_result_callback(self, future):
        self.get_logger().info(" [92m[BrainClient] Get result callback. [0m")
        result = future.result().result

        status_color = "\033[92m" if result.success else "\033[91m"
        self.get_logger().info(
            f"{status_color}Primitive execution result: {result.success}, Type: {result.success_type}\033[0m"
        )

        # Clear the goal handle since the goal is complete
        self._goal_handle = None

        # Stop the robot (consider moving this if primitives should stop themselves)
        # Note that I think cancelling the goal will also send a stop command, so maybe we can look into that.
        stop_cmd = Twist()
        stop_cmd.linear.x = 0.0
        stop_cmd.angular.z = 0.0
        self.cmd_vel_pub.publish(stop_cmd)

        # --- Send status message FIRST ---
        # Get primitive info from the primitive_running dictionary
        primitive_id = None
        primitive_name = result.primitive_type  # Use name from result
        if (
            self.primitive_running
            and self.primitive_running["primitive_name"] == primitive_name
        ):
            primitive_id = self.primitive_running["primitive_id"]
        elif self.primitive_running:
            self.get_logger().warn(
                f"Primitive name mismatch in result ({primitive_name}) and running ({self.primitive_running['primitive_name']})"
            )
            primitive_id = self.primitive_running[
                "primitive_id"
            ]  # Use stored ID anyway?

        self.primitive_running = None  # Clear running state

        # Determine the appropriate message type based on the result
        self.get_logger().info(f"Primitive result details: {result}")
        outgoing_msg = None
        if result.success and result.success_type == PrimitiveResult.SUCCESS.value:
            outgoing_msg = MessageIn(
                type=MessageInType.PRIMITIVE_COMPLETED,
                payload={
                    "primitive_name": primitive_name,
                    "primitive_id": primitive_id,
                },
            )
        elif result.success_type == PrimitiveResult.CANCELLED.value:
            outgoing_msg = MessageIn(
                type=MessageInType.PRIMITIVE_INTERRUPTED,
                payload={
                    "primitive_name": primitive_name,
                    "primitive_id": primitive_id,
                },
            )
        elif not result.success or result.success_type == PrimitiveResult.FAILURE.value:
            outgoing_msg = MessageIn(
                type=MessageInType.PRIMITIVE_FAILED,
                payload={
                    "primitive_name": primitive_name,
                    "reason": result.message,
                    "primitive_id": primitive_id,
                },
            )
        else:
            # Log success status as well for debugging unknown cases
            self.get_logger().error(
                f"Unknown primitive result combination: success={result.success}, type={result.success_type}"
            )

        # Send the determined status message
        if outgoing_msg:
            self.ws_bridge.send_message(outgoing_msg)
            self.get_logger().info(
                f"Sent primitive status message: {outgoing_msg.type.name}"
            )

        # --- THEN, check and execute pending task if previous was cancelled OR succeeded in the meantime ---
        if (
            result.success_type
            in [PrimitiveResult.CANCELLED.value, PrimitiveResult.SUCCESS.value]
            and self._pending_next_task is not None
        ):
            self.get_logger().info(
                f"Executing pending task after internal cancellation: {self._pending_next_task.type}"
            )
            pending_task = self._pending_next_task
            self._pending_next_task = None  # Clear before sending new goal
            status_msg = MessageIn(
                type=MessageInType.PRIMITIVE_ACTIVATED,
                payload={
                    "primitive_name": pending_task.type,
                    "primitive_id": pending_task.primitive_id,
                },
            )
            self.ws_bridge.send_message(status_msg)
            self.primitive_running = {
                "primitive_name": pending_task.type,
                "primitive_id": pending_task.primitive_id,
            }
            self.send_primitive_goal(pending_task.type, pending_task.inputs)
        elif self._pending_next_task is not None:
            # Clear pending task if the goal finished differently (SUCCESS/FAILURE)
            self.get_logger().warn(
                f"Clearing pending task {self._pending_next_task.type} because previous task finished with type {result.success_type}"
            )
            self._pending_next_task = None

    def _handle_primitives_and_directive_registered(self, msg):
        """
        Handle the PRIMITIVES_REGISTERED response from the server.
        """
        self.get_logger().info(f"Registration response: {msg.payload}")
        if msg.payload.get("success", False):
            self.primitives_registered = True
            self.directive_registered = True
            primitive_count = msg.payload.get("count", 0)
            directive_registered = msg.payload.get("directive_registered", False)
            self.get_logger().info(
                f"Successfully registered {primitive_count} primitives and directive: {directive_registered}"
            )

            # Auto-activate brain in simulator mode
            if self.simulator_mode and not self.is_brain_active:
                self.get_logger().info(
                    "\033[1;92m[BrainClient] Auto-activating brain in simulator mode\033[0m"
                )
                self.is_brain_active = True

            # Start the pose image timer if we've already received a ready_for_image message
            if self.ready_for_image and not self.pose_image_started:
                self.get_logger().info(
                    "Starting regular pose image transmission after registration"
                )
                self.pose_image_started = True
                self.pose_image_timer = self.create_timer(
                    self.pose_image_interval, self.pose_image_callback
                )
        else:
            self.get_logger().error(
                "Failed to register primitives and/or directive with server"
            )

    def register_primitives_and_directive(self):
        """
        Collects information about available primitives and directive and sends it to the server
        for registration.
        """
        self.get_logger().info(
            "Collecting primitive and directive definitions for registration..."
        )

        # Use primitives metadata from service if available
        if hasattr(self, 'primitives_metadata_list') and self.primitives_metadata_list:
            primitives = self.primitives_metadata_list
            self.get_logger().info(f"Using {len(primitives)} primitives from service")
        else:
            self.get_logger().warn("No primitives metadata available, falling back to local introspection")
            primitives = []
            if self.primitives_dict:
                for primitive_name, primitive in self.primitives_dict.items():
                    # Extract parameter information using introspection
                    params = {}
                    if hasattr(primitive, 'execute'):
                        signature = inspect.signature(primitive.execute)

                        for param_name, param in signature.parameters.items():
                            if param_name == "self":
                                continue

                            # Get parameter type from annotation if available
                            param_type = "any"
                            if param.annotation != inspect.Parameter.empty:
                                # Handle UnionType (e.g., int | str) and GenericAlias (e.g., list[int])
                                if (
                                    isinstance(
                                        param.annotation, (types.UnionType, types.GenericAlias)
                                    )
                                    or hasattr(param.annotation, "_name")
                                    and param.annotation._name
                                    in ["List", "Optional", "Dict", "Tuple", "Union"]
                                ):  # Covers typing.List, typing.Optional etc.
                                    param_type = str(param.annotation)
                                elif hasattr(param.annotation, "__name__"):
                                    param_type = param.annotation.__name__
                                else:
                                    # Fallback for other complex types, str() might be a reasonable default
                                    param_type = str(param.annotation)
                                # Clean up "typing." prefix if present
                                param_type = param_type.replace("typing.", "")

                            params[param_name] = f"{param_type}"

                    primitives.append(
                        {
                            "name": primitive_name,
                            "guidelines": primitive.guidelines(),
                            "guidelines_when_running": primitive.guidelines_when_running(),
                            "inputs": params,
                        }
                    )

        included_primitives = [
            p
            for p in primitives
            if p["name"] in self.current_directive.get_primitives()
        ]

        # Create and send the registration message
        reg_msg = MessageIn(
            type=MessageInType.REGISTER_PRIMITIVES_AND_DIRECTIVE,
            payload={
                "primitives": included_primitives if included_primitives else None,
                "directive": self.current_directive.get_prompt(),
                "token": self.token,
            },
        )
        self.get_logger().info(
            f"Registering {len(primitives)} primitives and directive '{self.current_directive.name}' with server"
        )
        self.ws_bridge.send_message(reg_msg)

    def set_directive_callback(self, msg: String):
        """
        Callback for changing the AI's directive.
        Note: This only changes the current directive, NOT the startup directive.
        """
        directive_name = msg.data.strip()
        self.get_logger().info(f"Received directive change request: {directive_name}")

        if directive_name in self.directives:
            self.current_directive = self.directives[directive_name]
            self.get_logger().info(f"Activated directive: {directive_name}")

            # Re-register primitives and directive with the server to update
            self.register_primitives_and_directive()
            
            # Activate input devices required by this directive
            self.activate_directive_inputs()

            # Publish confirmation
            chat_entry = {
                "sender": "system",
                "text": f"Directive updated to: {directive_name}",
                "timestamp": time.time(),
            }
            self.chat_history.append(chat_entry)
            out_msg = String(data=json.dumps(chat_entry))
            self.chat_out_pub.publish(out_msg)
        else:
            self.get_logger().error(f"Unknown directive: {directive_name}")
            available_directives = list(self.directives.keys())
            error_msg = {
                "sender": "system",
                "text": f"Error: Unknown directive '{directive_name}'. Available directives: {available_directives}",
                "timestamp": time.time(),
            }
            self.chat_history.append(error_msg)
            out_msg = String(data=json.dumps(error_msg))
            self.chat_out_pub.publish(out_msg)

    def handle_get_available_directives(self, request, response):
        """
        Service handler that returns detailed information about all available directives.
        """
        self.get_logger().info("Received request for available directives")
        
        # Build detailed directive information as JSON
        directive_details = []
        for directive_name, directive in self.directives.items():
            directive_info = {
                "name": directive.name,
                "prompt": directive.get_prompt(),
                "primitives": directive.get_primitives()
            }
            directive_details.append(directive_info)
        
        # Convert the detailed info to JSON string for the directives field
        response.directives = [json.dumps(directive_details)]
        response.current_directive = self.current_directive.name
        
        # Get startup directive from file
        startup_directive = self.load_startup_directive()
        response.startup_directive = startup_directive if startup_directive else ""
        
        return response

    def _unregister_primitives(self):
        """Internal method to unregister primitives."""
        self.get_logger().info(
            f"\033[1;92m[BrainClient] Unregistering primitives\033[0m"
        )

        # As long as we don't have
        # confirmation that the new primitives have been registered, we should not
        # accept new VisionAgentOutput messages.
        self.primitives_registered = False

        # Stop any running primitive
        if self.primitive_running:
            self.get_logger().info(
                "\033[1;92m[BrainClient] Stopping running primitive due to reset\033[0m"
            )
            if self._goal_handle:  # Check if goal_handle exists before trying to cancel
                cancel_future = self._goal_handle.cancel_goal_async()
                # We don't necessarily need to wait for this in a reset context,
                # but good to be aware it's async.
                # cancel_future.add_done_callback(self.cancel_response_callback) # Optional: log cancel response
                self._goal_handle = None  # Clear handle after requesting cancel
            self.primitive_running = None

            stop_cmd = Twist()
            stop_cmd.linear.x = 0.0
            stop_cmd.angular.z = 0.0
            self.cmd_vel_pub.publish(stop_cmd)

        self._pending_next_task = None  # Clear any pending task

    def _perform_brain_reset(self, memory_state: str):
        # Clear local chat history
        self.get_logger().info("\033[1;92m[BrainClient] Resetting brain\033[0m")

        # Unregister primitives
        self._unregister_primitives()

        # Clear local chat history
        self.chat_history = []

        # Send a reset message to the server
        reset_msg = MessageIn(
            type=MessageInType.RESET, payload={"memory_state": memory_state}
        )
        self.ws_bridge.send_message(reset_msg)

        # Publish a system message to the chat
        chat_entry = {
            "sender": "system",
            "text": f"Brain has been reset with memory state: {memory_state}",
            "timestamp": time.time(),
        }
        self.chat_history.append(chat_entry)
        out_msg = String(data=json.dumps(chat_entry))
        self.chat_out_pub.publish(out_msg)

        # Re-register primitives and directive with the server
        # This will also re-trigger ready_for_image if server responds positively
        self.register_primitives_and_directive()

    def handle_reset_brain(self, request, response):
        """
        Service handler for resetting the brain.
        Uses the internal _perform_brain_reset method.
        """
        self.get_logger().info(
            "\033[1;92m[BrainClient] Received /reset_brain request\033[0m"
        )
        if not self.is_brain_active:
            self.get_logger().warn(
                "\033[93m[BrainClient] Brain is currently inactive. Reset request will proceed but brain remains inactive until /set_brain_active is called.\033[0m"
            )
            # Still allow reset even if inactive, as it's an explicit user command.
            # The brain won't *do* anything until reactivated, but its state will be reset.

        self._perform_brain_reset(request.memory_state)
        response.success = True
        return response

    def _deactivate_brain(self):
        """Deactivates the brain's main operational loops and interactions."""
        self.get_logger().info("\033[1;93m[BrainClient] Deactivating brain...\033[0m")
        self.is_brain_active = False

        # Stop timers
        if self.agent_timer and not self.agent_timer.is_canceled():
            self.agent_timer.cancel()
            self.get_logger().info("Agent timer cancelled.")
        if self.pose_image_timer and not self.pose_image_timer.is_canceled():
            self.pose_image_timer.cancel()
            self.get_logger().info("Pose image timer cancelled.")

        self.ready_for_image = False
        self.pose_image_started = False  # Reset this flag
        self.primitives_registered = (
            False  # Mark primitives as not registered during deactivation
        )

        # Stop any running primitive
        if self.primitive_running and self._goal_handle:
            self.get_logger().info(
                "\033[91m[BrainClient] Deactivating: Canceling current goal.\033[0m"
            )
            # Store the next task if it exists, prevent immediate execution
            # For a full deactivation, we probably don't want to store a pending task.
            # self._pending_next_task = None # Ensure it's cleared.

            cancel_future = self._goal_handle.cancel_goal_async()
            # We might want to add a callback to confirm or log, but for deactivation, just sending is key.
            # cancel_future.add_done_callback(self.cancel_response_callback)
            # Also notify the server that the primitive has been cancelled
            self.ws_bridge.send_message(
                MessageIn(
                    type=MessageInType.PRIMITIVE_INTERRUPTED,
                    payload={
                        "primitive_name": self.primitive_running["primitive_name"],
                        "primitive_id": self.primitive_running["primitive_id"],
                    },
                )
            )
            self._goal_handle = None  # Clear after requesting cancel

        self.primitive_running = None
        self._pending_next_task = None  # Explicitly clear pending task on deactivation

        # Stop robot motion
        stop_cmd = Twist()
        stop_cmd.linear.x = 0.0
        stop_cmd.angular.z = 0.0
        self.cmd_vel_pub.publish(stop_cmd)
        self.get_logger().info("Stop command sent to robot.")

        # Optionally, send a message to the server indicating deactivation
        # deactivate_msg = MessageIn(type=MessageInType.CLIENT_DEACTIVATED, payload={})
        # self.ws_bridge.send_message(deactivate_msg)
        # For now, we'll rely on the client just going silent.

        self.get_logger().info("\033[1;93m[BrainClient] Brain deactivated.\033[0m")

    def _reactivate_brain(self):
        """Reactivates the brain and performs a reset."""
        self.get_logger().info("\033[1;92m[BrainClient] Reactivating brain...\033[0m")
        self.is_brain_active = True

        # NOTE: We do NOT apply directive_on_startup here - that's only for initial startup
        # On reactivation, we keep whatever directive was active before
        self.get_logger().info(
            f"\033[1;92m[BrainClient] Continuing with current directive: {self.current_directive.name}\033[0m"
        )

        # Restart the agent timer (which sends images based on ready_for_image)
        # The pose_image_timer will be started by _handle_ready_for_image or
        # _handle_primitives_and_directive_registered once the server is ready and primitives are registered.
        if self.agent_timer and self.agent_timer.is_canceled():
            self.agent_timer = self.create_timer(
                0.1, self.agent_loop_callback
            )  # Re-create timer
            self.get_logger().info("Agent timer restarted.")
        elif not self.agent_timer:  # If it was never created or somehow None
            self.agent_timer = self.create_timer(0.1, self.agent_loop_callback)
            self.get_logger().info("Agent timer created and started.")

        # Send a READY_FOR_CONNECTION message to the server
        self.get_logger().info(
            "\033[1;92m[BrainClient] Sending READY_FOR_CONNECTION to server.\033[0m"
        )
        ready_msg = InternalMessage(type=InternalMessageType.READY_FOR_CONNECTION)
        self.ws_bridge.send_message(ready_msg)

        # Wait 0.5 seconds to ensure the server has time to process the message
        time.sleep(0.5)

        self._unregister_primitives()
        self.register_primitives_and_directive()
        self.ready_for_image = True

        self.get_logger().info(
            "\033[1;92m[BrainClient] Brain reactivated and reset initiated.\033[0m"
        )

    def handle_set_brain_active(self, request, response):
        """Service handler for activating or deactivating the brain."""
        if request.data:  # True means activate
            if self.is_brain_active:
                msg = "Brain is already active."
                self.get_logger().info(msg)
                response.success = True
                response.message = msg
            else:
                self._reactivate_brain()
                response.success = True
                response.message = "Brain reactivated and reset initiated."
        else:  # False means deactivate
            if not self.is_brain_active:
                msg = "Brain is already inactive."
                self.get_logger().info(msg)
                response.success = True
                response.message = msg
            else:
                self._deactivate_brain()
                response.success = True
                response.message = "Brain deactivated."
        return response

    def handle_set_directive_on_startup(self, request, response):
        """
        Service handler for setting the directive on startup.
        Accepts directive name or empty string to clear.
        Note: This only affects the NEXT startup, not reactivation.
        """
        directive_name = request.directive_name.strip()
        
        if not directive_name:  # Empty string means clear the saved directive
            # Remove the directive file
            try:
                if os.path.exists(self.directive_file):
                    os.remove(self.directive_file)
                self.get_logger().info(
                    "\033[1;92m[BrainClient] Startup directive cleared. "
                    "Will use default from initialize_directives() on next startup.\033[0m"
                )
            except Exception as e:
                self.get_logger().error(f"Error clearing directive file: {e}")
            
            # Publish confirmation
            chat_entry = {
                "sender": "system",
                "text": "Startup directive cleared. Default will be used on next startup.",
                "timestamp": time.time(),
            }
            self.chat_history.append(chat_entry)
            out_msg = String(data=json.dumps(chat_entry))
            self.chat_out_pub.publish(out_msg)
            
            response.success = True
            response.message = "Startup directive cleared. Will use default on next startup."
            
        elif directive_name in self.directives:
            # Save directive to file
            self.save_startup_directive(directive_name)
            
            # Also immediately switch to it if brain is active
            self.current_directive = self.directives[directive_name]
            self.get_logger().info(
                f"\033[1;92m[BrainClient] Startup directive set to: {directive_name}\033[0m"
            )
            
            # Re-register primitives and directive with the server to update immediately
            if self.is_brain_active and self.primitives_registered:
                self.register_primitives_and_directive()
            
            # Activate input devices required by this directive
            self.activate_directive_inputs()
            
            # Publish confirmation
            chat_entry = {
                "sender": "system",
                "text": f"Startup directive set to: {directive_name}. Active now and will be used on next startup.",
                "timestamp": time.time(),
            }
            self.chat_history.append(chat_entry)
            out_msg = String(data=json.dumps(chat_entry))
            self.chat_out_pub.publish(out_msg)
            
            response.success = True
            response.message = f"Startup directive set to: {directive_name}. Active now and will be used on next startup."
            
        else:
            self.get_logger().error(
                f"\033[91m[BrainClient] Unknown directive: {directive_name}\033[0m"
            )
            available_directives = list(self.directives.keys())
            error_msg = {
                "sender": "system",
                "text": f"Error: Unknown directive '{directive_name}'. Available directives: {available_directives}",
                "timestamp": time.time(),
            }
            self.chat_history.append(error_msg)
            out_msg = String(data=json.dumps(error_msg))
            self.chat_out_pub.publish(out_msg)
            
            response.success = False
            response.message = f"Unknown directive '{directive_name}'. Available directives: {available_directives}"
        
        return response



    def load_startup_directive(self):
        """Load the startup directive from file, returns None if not configured"""
        try:
            if os.path.exists(self.directive_file):
                with open(self.directive_file, 'r') as f:
                    saved_directive = f.read().strip()
                    if saved_directive:
                        self.get_logger().info(f"Loaded startup directive: {saved_directive}")
                        return saved_directive
            self.get_logger().info("No startup directive configured, using default from initialize_directives()")
            return None
        except Exception as e:
            self.get_logger().error(f"Error loading startup directive: {e}")
            return None

    def save_startup_directive(self, directive_name):
        """Save the directive to load on startup"""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.directive_file), exist_ok=True)
            with open(self.directive_file, 'w') as f:
                f.write(directive_name)
            self.get_logger().debug(f"Saved startup directive: {directive_name}")
        except Exception as e:
            self.get_logger().error(f"Error saving startup directive: {e}")

    def destroy_node(self):
        self.exit_event.set()
        # Cancel the pose image timer if it exists
        if self.pose_image_timer and not self.pose_image_timer.is_canceled():
            self.pose_image_timer.cancel()
        # Cancel the agent timer if it exists
        if self.agent_timer and not self.agent_timer.is_canceled():
            self.agent_timer.cancel()
        # Clean up TTS handler
        if hasattr(self, 'tts_handler'):
            self.tts_handler.close()
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = BrainClientNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("KeyboardInterrupt, shutting down.")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
