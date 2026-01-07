#!/usr/bin/env python3
"""
PrimitiveExecutionActionServer

This ROS 2 node implements an action server for executing primitives.
When a goal is received (with a primitive type and its parameters encoded
as JSON), the corresponding primitive is executed.
"""

import os
import json
import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, ActionClient, GoalResponse, CancelResponse
import cv2  # For image processing
import base64  # For encoding
import numpy as np  # For map data
import math  # For yaw calculation
import inspect
import types
import threading

from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy


# Import the action definition – ensure that it is built and available.
from brain_messages.action import ExecutePrimitive, ExecuteBehavior
from brain_messages.srv import GetAvailablePrimitives

# Import primitive loader and types
from brain_client.skill_loader import SkillLoader
from brain_client.skill_types import (
    SkillResult,
    RobotStateType,
)
from brain_client.manipulation_interface import ManipulationInterface
from brain_client.mobility_interface import MobilityInterface
from brain_client.head_interface import HeadInterface

# Import ROS message types for subscriptions
from sensor_msgs.msg import CompressedImage  # Image removed as it is unused
from nav_msgs.msg import Odometry, OccupancyGrid
from std_msgs.msg import String
from std_srvs.srv import Trigger


class PrimitiveExecutionActionServer(Node):
    def __init__(self):
        super().__init__("primitive_execution_action_server")

        # Robot state storage
        self.last_main_camera_image = None  # Stores cv2 image object
        self.last_odom = None  # Stores Odometry message
        self.last_map = None  # Stores OccupancyGrid message
        self.last_head_position = None  # Stores head position dict (parsed JSON)
        
        # Track currently executing primitive for continuous state updates
        self._current_primitive = None
        self._current_primitive_lock = threading.Lock()
        self._state_update_timer = None

        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )

        self.declare_parameter("image_topic", "/mars/main_camera/image/compressed")
        self.image_topic = self.get_parameter("image_topic").value

        # Topic for base velocity commands
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")
        self.cmd_vel_topic = self.get_parameter("cmd_vel_topic").value
        
        # Topics for head control
        self.declare_parameter("head_position_topic", "/mars/head/set_position")
        self.head_position_topic = self.get_parameter("head_position_topic").value
        self.declare_parameter("head_current_position_topic", "/mars/head/current_position")
        self.head_current_position_topic = self.get_parameter("head_current_position_topic").value

        self.declare_parameter("simulator_mode", False)
        self.simulator_mode = self.get_parameter("simulator_mode").value

        # Subscribers for robot state
        # TODO: Make topic names configurable if needed (e.g., via parameters)
        self.main_camera_image_sub = self.create_subscription(
            CompressedImage,
            self.image_topic,
            self.main_camera_image_callback,
            image_qos,
        )
        self.odom_sub = self.create_subscription(
            Odometry, "/odom", self.odom_callback, 10
        )
        self.map_sub = self.create_subscription(
            OccupancyGrid,
            "/map",
            self.map_callback,
            1,  # QoS profile with transient local durability might be better for map
        )
        self.head_position_sub = self.create_subscription(
            String, self.head_current_position_topic, self.head_position_callback, 10
        )

        # Create manipulation interface for primitives to use
        self.manipulation = ManipulationInterface(self, self.get_logger())
        # Create mobility interface for base/wheel motion
        self.mobility = MobilityInterface(self, self.get_logger(), self.cmd_vel_topic)
        # Create head interface for head tilt control
        self.head = HeadInterface(self, self.get_logger(), self.head_position_topic)
        
        # Dynamic primitive loading
        self.primitive_loader = SkillLoader(self.get_logger())
        
        # Define directory to scan for skills
        # Using the unified skills directory at the root
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        primitives_directory = os.path.join(maurice_root, 'skills')
        
        if not os.path.exists(primitives_directory):
            self.get_logger().fatal(f"Primitives directory not found: {primitives_directory}")
            raise FileNotFoundError(f"Primitives directory must exist at {primitives_directory}")
        
        # Load all primitives dynamically
        discovered_primitives = self.primitive_loader.discover_skills_in_directory(primitives_directory)

        self.get_logger().info(f"Discovered primitives: {list(discovered_primitives.keys())} in {primitives_directory}")
        
        # Handle special case for navigation primitive based on simulator mode
        if self.simulator_mode and "navigate_to_position_sim" in discovered_primitives:
            # In simulator mode, use the sim version for navigate_to_position
            self.get_logger().info("Simulator mode: using NavigateToPositionSim for navigate_to_position")
            discovered_primitives["navigate_to_position"] = discovered_primitives["navigate_to_position_sim"]
            # Remove the _sim variant so it doesn't appear as a separate primitive
            del discovered_primitives["navigate_to_position_sim"]
        elif "navigate_to_position_sim" in discovered_primitives:
            # In real robot mode, remove the sim version entirely
            self.get_logger().info("Real robot mode: using standard NavigateToPosition")
            del discovered_primitives["navigate_to_position_sim"]

        # Create code primitive instances
        self._code_primitives = {}
        for primitive_name, primitive_class in discovered_primitives.items():
            try:
                primitive_instance = primitive_class(self.get_logger())
                primitive_instance.node = self  # Inject the node
                primitive_instance.manipulation = self.manipulation  # Inject manipulation interface
                primitive_instance.mobility = self.mobility  # Inject mobility interface
                primitive_instance.head = self.head  # Inject head interface
                self._code_primitives[primitive_name] = primitive_instance
                self.get_logger().info(f"Loaded code primitive: {primitive_name}")
            except Exception as e:
                self.get_logger().error(f"Error instantiating primitive {primitive_name}: {e}")
        
        self.get_logger().info(f"Successfully loaded {len(self._code_primitives)} code primitives")
        
        # Load physical primitives from metadata.json files
        self._physical_primitives, self._in_training_primitives = self._load_physical_primitives(primitives_directory)
        self.get_logger().info(f"Successfully loaded {len(self._physical_primitives)} physical primitives")
        self.get_logger().info(f"Found {len(self._in_training_primitives)} in-training primitives")
        
        # Create action client to delegate physical primitives to behavior_server
        self._behavior_client = ActionClient(self, ExecuteBehavior, '/behavior/execute')

        self._action_server = ActionServer(
            self,
            ExecutePrimitive,
            "execute_primitive",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
        )
        
        # Create unified service endpoint for getting all primitives
        self._get_primitives_service = self.create_service(
            GetAvailablePrimitives,
            '/brain/get_available_primitives',
            self._handle_get_available_primitives
        )
        
        # Create service for reloading primitives
        self._reload_srv = self.create_service(
            Trigger,
            '/brain/reload_primitives',
            self._handle_reload_primitives
        )
        
        self.get_logger().debug("Primitive Execution Action Server has started.")
        self.get_logger().info(f"Total primitives available: {len(self._code_primitives) + len(self._physical_primitives)}")

    def _reload_primitives(self):
        """Reload all primitives from disk."""
        self.get_logger().info("Reloading primitives...")
        
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        primitives_directory = os.path.join(maurice_root, 'skills')
        
        # Reload code primitives
        discovered_primitives = self.primitive_loader.discover_skills_in_directory(primitives_directory)
        
        # Handle simulator mode nav swap
        if self.simulator_mode and "navigate_to_position_sim" in discovered_primitives:
            discovered_primitives["navigate_to_position"] = discovered_primitives["navigate_to_position_sim"]
            del discovered_primitives["navigate_to_position_sim"]
        elif "navigate_to_position_sim" in discovered_primitives:
            del discovered_primitives["navigate_to_position_sim"]
        
        self._code_primitives = {}
        for name, cls in discovered_primitives.items():
            try:
                instance = cls(self.get_logger())
                instance.node = self
                instance.manipulation = self.manipulation
                instance.mobility = self.mobility
                instance.head = self.head
                self._code_primitives[name] = instance
                self.get_logger().info(f"Reloaded code primitive: {name}")
            except Exception as e:
                self.get_logger().error(f"Error instantiating {name}: {e}")
        
        # Reload physical primitives
        self._physical_primitives, self._in_training_primitives = self._load_physical_primitives(primitives_directory)
        
        self.get_logger().info(f"Reloaded {len(self._code_primitives)} code + {len(self._physical_primitives)} physical primitives")

    def _handle_reload_primitives(self, request, response):
        """Service handler for reloading primitives."""
        try:
            self._reload_primitives()
            response.success = True
            response.message = f"Reloaded {len(self._code_primitives)} code, {len(self._physical_primitives)} physical primitives"
        except Exception as e:
            response.success = False
            response.message = f"Failed to reload primitives: {e}"
        return response

    def _load_physical_primitives(self, primitives_directory):
        """Load physical primitives from metadata.json files.
        
        Returns:
            tuple: (physical_primitives dict, in_training_primitives dict)
        """
        physical_primitives = {}
        in_training_primitives = {}
        
        for item in os.listdir(primitives_directory):
            item_path = os.path.join(primitives_directory, item)
            
            # Check if it's a directory with metadata.json
            if os.path.isdir(item_path):
                metadata_path = os.path.join(item_path, 'metadata.json')
                if os.path.exists(metadata_path):
                    with open(metadata_path, 'r') as f:
                        metadata = json.load(f)
                        primitive_name = metadata.get('name', item)
                        
                        # Validate primitive before loading
                        is_valid, is_in_training, episode_count = self.primitive_loader.validate_physical_skill(item_path, metadata)
                        
                        if is_valid:
                            primitive_data = {
                                'metadata': metadata,
                                'directory': item_path,
                                'in_training': is_in_training,
                                'episode_count': episode_count
                            }
                            
                            if is_in_training:
                                in_training_primitives[primitive_name] = primitive_data
                                self.get_logger().info(f"Loaded in-training primitive: {primitive_name} (type: {metadata.get('type', 'unknown')})")
                            else:
                                physical_primitives[primitive_name] = primitive_data
                                self.get_logger().info(f"Loaded physical primitive: {primitive_name} (type: {metadata.get('type', 'unknown')})")
                        else:
                            self.get_logger().warn(f"Skipped invalid physical primitive: {primitive_name}")
        
        return physical_primitives, in_training_primitives

    def _handle_get_available_primitives(self, request, response):
        all_primitives = []
        include_in_training = request.include_in_training
        
        # Add code primitives
        for name, primitive_instance in self._code_primitives.items():
            # Extract parameter information using introspection
            inputs = {}
            if hasattr(primitive_instance, 'execute'):
                signature = inspect.signature(primitive_instance.execute)
                
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
                    
                    inputs[param_name] = f"{param_type}"
            
            primitive_info = {
                "name": name,
                "type": "code",
                "guidelines": primitive_instance.guidelines() if hasattr(primitive_instance, 'guidelines') else "",
                "guidelines_when_running": primitive_instance.guidelines_when_running() if hasattr(primitive_instance, 'guidelines_when_running') else "",
                "inputs": inputs
            }
            self.get_logger().info(f"Code primitive '{name}' has inputs: {inputs}")
            all_primitives.append(primitive_info)
        
        # Add physical primitives
        for name, physical_data in self._physical_primitives.items():
            metadata = physical_data['metadata']
            # Fetch episode count dynamically (not cached)
            episode_count = self.primitive_loader._get_episode_count(physical_data['directory'])
            primitive_info = {
                "name": metadata.get('name', name),
                "type": metadata.get('type', 'physical'),
                "guidelines": metadata.get('guidelines', ''),
                "guidelines_when_running": metadata.get('guidelines_when_running', ''),
                "inputs": metadata.get('inputs', {}),
                "in_training": False,
                "episode_count": episode_count
            }
            self.get_logger().info(f"Physical primitive '{name}' has inputs: {metadata.get('inputs', {})}, episodes: {episode_count}")
            all_primitives.append(primitive_info)
        
        # Add in-training primitives if requested
        if include_in_training:
            for name, physical_data in self._in_training_primitives.items():
                metadata = physical_data['metadata']
                # Fetch episode count dynamically (not cached)
                episode_count = self.primitive_loader._get_episode_count(physical_data['directory'])
                primitive_info = {
                    "name": metadata.get('name', name),
                    "type": metadata.get('type', 'physical'),
                    "guidelines": metadata.get('guidelines', ''),
                    "guidelines_when_running": metadata.get('guidelines_when_running', ''),
                    "inputs": metadata.get('inputs', {}),
                    "in_training": True,
                    "episode_count": episode_count
                }
                self.get_logger().info(f"In-training primitive '{name}' has inputs: {metadata.get('inputs', {})}, episodes: {episode_count}")
                all_primitives.append(primitive_info)
        
        response.primitives_json = json.dumps(all_primitives)
        self.get_logger().info(f"Returned {len(all_primitives)} primitives to service caller")
        return response

    def goal_callback(self, goal_request):
        self.get_logger().debug(
            f"Received goal for primitive: '{goal_request.primitive_type}'"
        )
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        try:
            # Get the primitive type from the goal handle
            primitive_type = goal_handle.request.primitive_type

            # Find and cancel the code primitive
            if primitive_type in self._code_primitives:
                primitive = self._code_primitives[primitive_type]
                self.get_logger().debug(f"Canceling code primitive: {primitive_type}")
                primitive.cancel()
            elif primitive_type in self._physical_primitives:
                # Physical primitives are handled by behavior_server
                # Cancellation will be forwarded by the action client
                self.get_logger().debug(f"Canceling physical primitive: {primitive_type}")
            else:
                self.get_logger().warning(f"Unknown primitive type: {primitive_type}")
        except Exception as e:
            self.get_logger().error(f"Error in cancel_callback: {str(e)}")

            # If we couldn't determine the primitive type, try to cancel all code primitives
            self.get_logger().debug("Attempting to cancel all code primitives")
            for name, primitive in self._code_primitives.items():
                try:
                    primitive.cancel()
                except Exception as cancel_error:
                    err_msg = f"Error canceling {name}: {str(cancel_error)}"
                    self.get_logger().error(err_msg)

        return CancelResponse.ACCEPT

    def execute_callback(self, goal_handle):
        # Trace entry into the execute callback for debugging
        self.get_logger().info(
            f"[PEAS] execute_callback ENTER for primitive: '{goal_handle.request.primitive_type}'"
        )
        # Decode the inputs (assumed to be JSON)
        try:
            inputs = json.loads(goal_handle.request.inputs)
        except Exception as e:
            self.get_logger().error(f"Invalid JSON for inputs: {str(e)}")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message="Invalid inputs JSON",
                success_type=SkillResult.FAILURE.value,
            )

        primitive_type = goal_handle.request.primitive_type
        
        # Check if it's a code primitive
        if primitive_type in self._code_primitives:
            self.get_logger().info(
                f"[PEAS] execute_callback DISPATCH to _execute_code_primitive for '{primitive_type}'"
            )
            return self._execute_code_primitive(goal_handle, primitive_type, inputs)
        
        # Check if it's a physical primitive
        elif primitive_type in self._physical_primitives:
            self.get_logger().info(
                f"[PEAS] execute_callback DISPATCH to _execute_physical_primitive for '{primitive_type}'"
            )
            return self._execute_physical_primitive(goal_handle, primitive_type, inputs)
        
        # Primitive not found
        else:
            all_primitives = list(self._code_primitives.keys()) + list(self._physical_primitives.keys())
            self.get_logger().error(f"Primitive '{primitive_type}' not available")
            self.get_logger().error(f"Available primitives: {all_primitives}")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message="Primitive not available",
                primitive_type=primitive_type,
                success_type=SkillResult.FAILURE.value,
            )

    def _update_primitive_robot_state(self, primitive):
        """Helper to update a primitive's robot state from current sensor data."""
        required_states = primitive.get_required_robot_states()
        if not required_states:
            return
        
        robot_state_to_inject = {}

        if RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64 in required_states:
            if self.last_main_camera_image is not None:
                try:
                    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
                    success, encoded_img_bytes = cv2.imencode(
                        ".jpg", self.last_main_camera_image, encode_params
                    )
                    if success:
                        robot_state_to_inject[
                            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value
                        ] = base64.b64encode(encoded_img_bytes.tobytes()).decode(
                            "utf-8"
                        )
                    else:
                        self.get_logger().error(
                            "Failed to encode last_main_camera_image for primitive state"
                        )
                except Exception as e_img:
                    self.get_logger().error(
                        f"Error encoding last_main_camera_image for primitive: {e_img}"
                    )
            else:
                self.get_logger().warn(
                    "Primitive requires "
                    "LAST_MAIN_CAMERA_IMAGE_B64 but none available."
                )

        if RobotStateType.LAST_ODOM in required_states:
            if self.last_odom is not None:
                pos = self.last_odom.pose.pose.position
                ori = self.last_odom.pose.pose.orientation
                siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
                cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
                theta = math.atan2(siny_cosp, cosy_cosp)
                robot_state_to_inject[RobotStateType.LAST_ODOM.value] = {
                    "header": {
                        "stamp": {
                            "sec": self.last_odom.header.stamp.sec,
                            "nanosec": self.last_odom.header.stamp.nanosec,
                        },
                        "frame_id": self.last_odom.header.frame_id,
                    },
                    "child_frame_id": self.last_odom.child_frame_id,
                    "pose": {
                        "pose": {
                            "position": {"x": pos.x, "y": pos.y, "z": pos.z},
                            "orientation": {
                                "x": ori.x,
                                "y": ori.y,
                                "z": ori.z,
                                "w": ori.w,
                            },
                        }
                    },
                    "theta_degrees": math.degrees(theta),
                }
            else:
                self.get_logger().warn(
                    "Primitive requires LAST_ODOM "
                    "but none available."
                )

        if RobotStateType.LAST_MAP in required_states:
            if self.last_map is not None:
                map_data_bytes = np.array(
                    self.last_map.data, dtype=np.int8
                ).tobytes()
                ori_map = self.last_map.info.origin.orientation
                siny_cosp_map = 2.0 * (
                    ori_map.w * ori_map.z + ori_map.x * ori_map.y
                )
                cosy_cosp_map = 1.0 - 2.0 * (
                    ori_map.y * ori_map.y + ori_map.z * ori_map.z
                )
                yaw_map = math.atan2(siny_cosp_map, cosy_cosp_map)
                robot_state_to_inject[RobotStateType.LAST_MAP.value] = {
                    "header": {
                        "stamp": {
                            "sec": self.last_map.header.stamp.sec,
                            "nanosec": self.last_map.header.stamp.nanosec,
                        },
                        "frame_id": self.last_map.header.frame_id,
                    },
                    "info": {
                        "map_load_time": {
                            "sec": self.last_map.info.map_load_time.sec,
                            "nanosec": self.last_map.info.map_load_time.nanosec,
                        },
                        "resolution": self.last_map.info.resolution,
                        "width": self.last_map.info.width,
                        "height": self.last_map.info.height,
                        "origin": {
                            "position": {
                                "x": self.last_map.info.origin.position.x,
                                "y": self.last_map.info.origin.position.y,
                                "z": self.last_map.info.origin.position.z,
                            },
                            "orientation": {
                                "x": ori_map.x,
                                "y": ori_map.y,
                                "z": ori_map.z,
                                "w": ori_map.w,
                            },
                            "yaw_degrees": math.degrees(yaw_map),
                        },
                    },
                    "data_b64": base64.b64encode(map_data_bytes).decode("utf-8"),
                }
            else:
                self.get_logger().warn(
                    "Primitive requires LAST_MAP but "
                    "none available."
                )

        if RobotStateType.LAST_HEAD_POSITION in required_states:
            if self.last_head_position is not None:
                robot_state_to_inject[RobotStateType.LAST_HEAD_POSITION.value] = self.last_head_position
            else:
                self.get_logger().warn(
                    "Primitive requires LAST_HEAD_POSITION but "
                    "none available."
                )

        if robot_state_to_inject:  # Only call if there is state to update
            primitive.update_robot_state(**robot_state_to_inject)
    
    def _continuous_state_update_callback(self):
        """Timer callback to continuously update robot state for running primitive."""
        with self._current_primitive_lock:
            if self._current_primitive is not None:
                try:
                    self._update_primitive_robot_state(self._current_primitive)
                except Exception as e:
                    self.get_logger().error(f"Error in continuous state update: {e}")
    
    def _execute_code_primitive(self, goal_handle, primitive_type, inputs):
        primitive = self._code_primitives[primitive_type]
        # Trace start of code primitive execution
        self.get_logger().info(
            f"[PEAS] _execute_code_primitive START '{primitive_type}' with inputs: {inputs}"
        )

        # Define a feedback publisher for the primitive
        def _publish_feedback(update_message: str):
            feedback_msg = ExecutePrimitive.Feedback()
            feedback_msg.feedback = update_message
            goal_handle.publish_feedback(feedback_msg)
            self.get_logger().debug(
                f"Published feedback for '{primitive_type}': {update_message}"
            )

        # Pass the feedback callback to the primitive if it supports it
        primitive.set_feedback_callback(_publish_feedback)

        try:
            # Initial robot state injection
            self._update_primitive_robot_state(primitive)
            
            # Start continuous state updates if primitive needs real-time data
            required_states = primitive.get_required_robot_states()
            if required_states:
                with self._current_primitive_lock:
                    self._current_primitive = primitive
                # Create timer for 10Hz updates (100ms)
                self._state_update_timer = self.create_timer(
                    0.02, self._continuous_state_update_callback
                )
                self.get_logger().info(
                    f"Started continuous state updates for '{primitive_type}' at 10Hz"
                )

            # Execute the primitive with its direct inputs
            result_message, result_status = primitive.execute(**inputs)

            # Stop continuous state updates
            if self._state_update_timer is not None:
                self._state_update_timer.cancel()
                self._state_update_timer = None
            with self._current_primitive_lock:
                self._current_primitive = None
            
            # Trace completion of the primitive before result handling
            self.get_logger().info(
                f"[PEAS] _execute_code_primitive DONE '{primitive_type}' with status {result_status}"
            )

            # Handle the result based on the SkillResult enum
            if result_status == SkillResult.SUCCESS:
                self.get_logger().info(
                    f"Primitive '{primitive_type}' succeeded: {result_message}"
                )
                goal_handle.succeed()
                return ExecutePrimitive.Result(
                    success=True,
                    message=result_message,
                    primitive_type=primitive_type,
                    success_type=SkillResult.SUCCESS.value,
                )
            elif result_status == SkillResult.CANCELLED:
                self.get_logger().info(
                    f"Primitive '{primitive_type}' cancelled: {result_message}"
                )
                goal_handle.succeed()
                return ExecutePrimitive.Result(
                    success=True,
                    message=result_message,
                    primitive_type=primitive_type,
                    success_type=SkillResult.CANCELLED.value,
                )
            else:  # SkillResult.FAILURE
                self.get_logger().info(
                    f"Primitive '{primitive_type}' failed: {result_message}"
                )
                goal_handle.abort()
                return ExecutePrimitive.Result(
                    success=False,
                    message=result_message,
                    primitive_type=primitive_type,
                    success_type=SkillResult.FAILURE.value,
                )

        except Exception as e:
            self.get_logger().error(f"Error executing primitive: {str(e)}")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message=str(e),
                primitive_type=primitive_type,
                success_type=SkillResult.FAILURE.value,
            )

    def _execute_physical_primitive(self, goal_handle, primitive_type, inputs):
        self.get_logger().info(f"Delegating physical primitive '{primitive_type}' to behavior_server")
        
        # Get the physical primitive metadata
        physical_data = self._physical_primitives[primitive_type]
        metadata = physical_data['metadata']
        
        # Wait for behavior server to be available
        if not self._behavior_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error("Behavior server not available!")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message="Behavior server not available",
                primitive_type=primitive_type,
                success_type=SkillResult.FAILURE.value,
            )
        
        # Create behavior goal with config from metadata
        behavior_goal = ExecuteBehavior.Goal()
        behavior_goal.behavior_name = primitive_type
        behavior_goal.behavior_config = json.dumps(metadata)  # Pass entire metadata as config
        
        # Send goal and wait for result
        self.get_logger().info(f"Sending behavior goal to behavior_server: {primitive_type}")
        send_goal_future = self._behavior_client.send_goal_async(behavior_goal)
        
        # Wait for goal acceptance
        rclpy.spin_until_future_complete(self, send_goal_future, timeout_sec=5.0)
        
        if not send_goal_future.done():
            self.get_logger().error("Timeout waiting for behavior goal acceptance")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message="Timeout waiting for behavior goal acceptance",
                primitive_type=primitive_type,
                success_type=SkillResult.FAILURE.value,
            )
        
        behavior_goal_handle = send_goal_future.result()
        if not behavior_goal_handle.accepted:
            self.get_logger().error("Behavior goal rejected by behavior_server")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message="Behavior goal rejected by behavior_server",
                primitive_type=primitive_type,
                success_type=SkillResult.FAILURE.value,
            )
        
        self.get_logger().info("Behavior goal accepted, waiting for result...")
        
        # Wait for result
        result_future = behavior_goal_handle.get_result_async()
        
        # Spin until complete - this blocks but that's okay in an action callback
        rclpy.spin_until_future_complete(self, result_future)
        
        behavior_result = result_future.result().result
        
        # Convert behavior result to primitive result
        if behavior_result.success:
            self.get_logger().info(f"Physical primitive '{primitive_type}' succeeded: {behavior_result.message}")
            goal_handle.succeed()
            return ExecutePrimitive.Result(
                success=True,
                message=behavior_result.message,
                primitive_type=primitive_type,
                success_type=SkillResult.SUCCESS.value,
            )
        else:
            # Check if it was cancelled
            if "cancel" in behavior_result.message.lower():
                self.get_logger().info(f"Physical primitive '{primitive_type}' cancelled: {behavior_result.message}")
                goal_handle.succeed()
                return ExecutePrimitive.Result(
                    success=True,
                    message=behavior_result.message,
                    primitive_type=primitive_type,
                    success_type=SkillResult.CANCELLED.value,
                )
            else:
                self.get_logger().error(f"Physical primitive '{primitive_type}' failed: {behavior_result.message}")
                goal_handle.abort()
                return ExecutePrimitive.Result(
                    success=False,
                    message=behavior_result.message,
                    primitive_type=primitive_type,
                    success_type=SkillResult.FAILURE.value,
                )

    def destroy(self):
        self._action_server.destroy()
        super().destroy_node()

    # Callbacks for state subscriptions
    def main_camera_image_callback(self, msg: CompressedImage):
        try:
            np_arr = np.frombuffer(msg.data, np.uint8)
            self.last_main_camera_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            self.get_logger().debug("Received and decoded new image for primitives.")
        except Exception as e:
            self.get_logger().error(
                f"Failed to decode compressed image for primitive state: {e}"
            )

    def odom_callback(self, msg: Odometry):
        self.last_odom = msg
        # self.get_logger().debug('Received new odometry for primitives.')

    def map_callback(self, msg: OccupancyGrid):
        self.last_map = msg
        # self.get_logger().debug('Received new map for primitives.')
    
    def head_position_callback(self, msg: String):
        """Parse head position JSON and store it."""
        try:
            import json
            self.last_head_position = json.loads(msg.data)
            # self.get_logger().debug(f'Received head position: {self.last_head_position}')
        except Exception as e:
            self.get_logger().error(f"Failed to parse head position JSON: {e}")


def main(args=None):
    rclpy.init(args=args)
    action_server = PrimitiveExecutionActionServer()
    rclpy.spin(action_server)
    action_server.destroy()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
