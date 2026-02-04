#!/usr/bin/env python3
"""
SkillsActionServer

This ROS 2 node implements an action server for executing skills.
When a goal is received (with a skill type and its parameters encoded
as JSON), the corresponding skill is executed.
"""

import base64  # For encoding
import inspect
import json
import math  # For yaw calculation
import os
import threading
import types

import cv2  # For image processing
import numpy as np  # For map data
import rclpy

# Import the action definition – ensure that it is built and available.
from brain_messages.action import ExecuteBehavior, ExecuteSkill
from brain_messages.srv import GetAvailableSkills
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy

# Import ROS message types for subscriptions
from sensor_msgs.msg import CompressedImage  # Image removed as it is unused
from std_msgs.msg import String
from std_srvs.srv import Trigger

from brain_client.head_interface import HeadInterface
from brain_client.manipulation_interface import ManipulationInterface
from brain_client.mobility_interface import MobilityInterface

# Import skill loader and types
from brain_client.skill_loader import SkillLoader
from brain_client.skill_types import (
    InterfaceType,
    RobotStateType,
    SkillResult,
)


class SkillsActionServer(Node):
    def __init__(self):
        super().__init__("skills_action_server")

        # Robot state storage
        self.last_main_camera_image = None  # Stores cv2 image object
        self.last_odom = None  # Stores Odometry message
        self.last_map = None  # Stores OccupancyGrid message
        self.last_head_position = None  # Stores head position dict (parsed JSON)

        # Track currently executing skill for continuous state updates
        self._current_skill = None
        self._current_skill_lock = threading.Lock()
        self._state_update_thread = None
        self._state_update_stop_event = threading.Event()

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
        self.odom_sub = self.create_subscription(Odometry, "/odom", self.odom_callback, 10)
        self.map_sub = self.create_subscription(
            OccupancyGrid,
            "/map",
            self.map_callback,
            1,  # QoS profile with transient local durability might be better for map
        )
        self.head_position_sub = self.create_subscription(
            String, self.head_current_position_topic, self.head_position_callback, 10
        )

        # Create manipulation interface for skills to use
        self.manipulation = ManipulationInterface(self, self.get_logger())
        # Create mobility interface for base/wheel motion
        self.mobility = MobilityInterface(self, self.get_logger(), self.cmd_vel_topic)
        # Create head interface for head tilt control
        self.head = HeadInterface(self, self.get_logger(), self.head_position_topic)

        # Dynamic skill loading
        self.skill_loader = SkillLoader(self.get_logger())

        # Define directories to scan for skills
        maurice_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
        skills_directory = os.path.join(maurice_root, "skills")
        user_skills_directory = os.path.join(os.path.expanduser("~"), "skills")

        if not os.path.exists(skills_directory):
            self.get_logger().fatal(f"Skills directory not found: {skills_directory}")
            raise FileNotFoundError(f"Skills directory must exist at {skills_directory}")

        # Store directories for reload
        self._skills_directories = [skills_directory]
        if os.path.exists(user_skills_directory):
            self._skills_directories.append(user_skills_directory)
            self.get_logger().info(f"Also scanning user skills directory: {user_skills_directory}")

        # Load all skills dynamically
        discovered_skills = self.skill_loader.discover_skills_in_directory(skills_directory)

        self.get_logger().info(f"Discovered skills: {list(discovered_skills.keys())} in {skills_directory}")

        # Handle special case for navigation skill based on simulator mode
        if self.simulator_mode and "navigate_to_position_sim" in discovered_skills:
            # In simulator mode, use the sim version for navigate_to_position
            self.get_logger().info("Simulator mode: using NavigateToPositionSim for navigate_to_position")
            discovered_skills["navigate_to_position"] = discovered_skills["navigate_to_position_sim"]
            # Remove the _sim variant so it doesn't appear as a separate skill
            del discovered_skills["navigate_to_position_sim"]
        elif "navigate_to_position_sim" in discovered_skills:
            # In real robot mode, remove the sim version entirely
            self.get_logger().info("Real robot mode: using standard NavigateToPosition")
            del discovered_skills["navigate_to_position_sim"]

        # Create code skill instances
        self._code_skills = {}
        for skill_name, skill_class in discovered_skills.items():
            try:
                skill_instance = skill_class(self.get_logger())
                skill_instance.node = self  # Inject the node
                self._inject_required_interfaces(skill_instance)
                self._code_skills[skill_name] = skill_instance
                self.get_logger().info(f"Loaded code skill: {skill_name}")
            except Exception as e:
                self.get_logger().error(f"Error instantiating skill {skill_name}: {e}")

        self.get_logger().info(f"Successfully loaded {len(self._code_skills)} code skills")

        # Load physical skills from metadata.json files (from all skill directories)
        self._physical_skills, self._in_training_skills = self._load_physical_skills(self._skills_directories)
        self.get_logger().info(f"Successfully loaded {len(self._physical_skills)} physical skills")
        self.get_logger().info(f"Found {len(self._in_training_skills)} in-training skills")

        # Create action client to delegate physical skills to behavior_server
        self._behavior_client = ActionClient(self, ExecuteBehavior, "/behavior/execute")

        self._action_server = ActionServer(
            self,
            ExecuteSkill,
            "execute_skill",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
        )

        # Create unified service endpoint for getting all skills
        self._get_skills_service = self.create_service(
            GetAvailableSkills,
            "/brain/get_available_skills",
            self._handle_get_available_skills,
        )

        # Create service for reloading skills
        self._reload_srv = self.create_service(Trigger, "/brain/reload_primitives", self._handle_reload_skills)

        self.get_logger().debug("Skills Action Server has started.")
        self.get_logger().info(
            f"Total skills available: {len(self._code_skills) + len(self._physical_skills)}"
        )

    def _reload_skills(self):
        """Reload all skills from disk."""
        self.get_logger().info("Reloading skills...")

        maurice_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
        skills_directory = os.path.join(maurice_root, "skills")
        user_skills_directory = os.path.join(os.path.expanduser("~"), "skills")

        # Update directories list
        self._skills_directories = [skills_directory]
        if os.path.exists(user_skills_directory):
            self._skills_directories.append(user_skills_directory)

        # Reload code skills
        discovered_skills = self.skill_loader.discover_skills_in_directory(skills_directory)

        # Handle simulator mode nav swap
        if self.simulator_mode and "navigate_to_position_sim" in discovered_skills:
            discovered_skills["navigate_to_position"] = discovered_skills["navigate_to_position_sim"]
            del discovered_skills["navigate_to_position_sim"]
        elif "navigate_to_position_sim" in discovered_skills:
            del discovered_skills["navigate_to_position_sim"]

        self._code_skills = {}
        for name, cls in discovered_skills.items():
            try:
                instance = cls(self.get_logger())
                instance.node = self
                self._inject_required_interfaces(instance)
                self._code_skills[name] = instance
                self.get_logger().info(f"Reloaded code skill: {name}")
            except Exception as e:
                self.get_logger().error(f"Error instantiating {name}: {e}")

        # Reload physical skills (from all skill directories)
        self._physical_skills, self._in_training_skills = self._load_physical_skills(self._skills_directories)

        self.get_logger().info(
            f"Reloaded {len(self._code_skills)} code + {len(self._physical_skills)} physical skills"
        )

    def _handle_reload_skills(self, request, response):
        """Service handler for reloading skills."""
        try:
            self._reload_skills()
            response.success = True
            response.message = (
                f"Reloaded {len(self._code_skills)} code, {len(self._physical_skills)} physical skills"
            )
        except Exception as e:
            response.success = False
            response.message = f"Failed to reload skills: {e}"
        return response

    def _load_physical_skills(self, skills_directories):
        """Load physical skills from metadata.json files.

        Args:
            skills_directories: List of directories to scan for physical skills.

        Returns:
            tuple: (physical_skills dict, in_training_skills dict)
        """
        physical_skills = {}
        in_training_skills = {}

        for skills_directory in skills_directories:
            if not os.path.exists(skills_directory):
                continue

            for item in os.listdir(skills_directory):
                item_path = os.path.join(skills_directory, item)

                # Check if it's a directory with metadata.json
                if os.path.isdir(item_path):
                    metadata_path = os.path.join(item_path, "metadata.json")
                    if os.path.exists(metadata_path):
                        with open(metadata_path) as f:
                            metadata = json.load(f)
                            skill_name = metadata.get("name", item)

                            # Validate skill before loading
                            is_valid, is_in_training, episode_count = self.skill_loader.validate_physical_skill(
                                item_path, metadata
                            )

                            if is_valid:
                                skill_data = {
                                    "metadata": metadata,
                                    "directory": item_path,
                                    "in_training": is_in_training,
                                    "episode_count": episode_count,
                                }

                                if is_in_training:
                                    in_training_skills[skill_name] = skill_data
                                    self.get_logger().info(
                                        f"Loaded in-training skill: {skill_name} (type: {metadata.get('type', 'unknown')})"
                                    )
                                else:
                                    physical_skills[skill_name] = skill_data
                                    self.get_logger().info(
                                        f"Loaded physical skill: {skill_name} (type: {metadata.get('type', 'unknown')})"
                                    )
                            else:
                                self.get_logger().warn(f"Skipped invalid physical skill: {skill_name}")

        return physical_skills, in_training_skills

    def _handle_get_available_skills(self, request, response):
        all_skills = []
        include_in_training = request.include_in_training

        # Add code skills
        for name, skill_instance in self._code_skills.items():
            # Extract parameter information using introspection
            inputs = {}
            if hasattr(skill_instance, "execute"):
                signature = inspect.signature(skill_instance.execute)

                for param_name, param in signature.parameters.items():
                    if param_name == "self":
                        continue

                    # Get parameter type from annotation if available
                    param_type = "any"
                    if param.annotation != inspect.Parameter.empty:
                        # Handle UnionType (e.g., int | str) and GenericAlias (e.g., list[int])
                        if (
                            isinstance(param.annotation, (types.UnionType, types.GenericAlias))
                            or hasattr(param.annotation, "_name")
                            and param.annotation._name in ["List", "Optional", "Dict", "Tuple", "Union"]
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

            skill_info = {
                "name": name,
                "type": "code",
                "guidelines": (skill_instance.guidelines() if hasattr(skill_instance, "guidelines") else ""),
                "guidelines_when_running": (
                    skill_instance.guidelines_when_running()
                    if hasattr(skill_instance, "guidelines_when_running")
                    else ""
                ),
                "inputs": inputs,
            }
            self.get_logger().info(f"Code skill '{name}' has inputs: {inputs}")
            all_skills.append(skill_info)

        # Add physical skills
        for name, physical_data in self._physical_skills.items():
            metadata = physical_data["metadata"]
            # Fetch episode count dynamically (not cached)
            episode_count = self.skill_loader._get_episode_count(physical_data["directory"])
            skill_info = {
                "name": metadata.get("name", name),
                "type": metadata.get("type", "physical"),
                "guidelines": metadata.get("guidelines", ""),
                "guidelines_when_running": metadata.get("guidelines_when_running", ""),
                "inputs": metadata.get("inputs", {}),
                "in_training": False,
                "episode_count": episode_count,
            }
            self.get_logger().info(
                f"Physical skill '{name}' has inputs: {metadata.get('inputs', {})}, episodes: {episode_count}"
            )
            all_skills.append(skill_info)

        # Add in-training skills if requested
        if include_in_training:
            for name, physical_data in self._in_training_skills.items():
                metadata = physical_data["metadata"]
                # Fetch episode count dynamically (not cached)
                episode_count = self.skill_loader._get_episode_count(physical_data["directory"])
                skill_info = {
                    "name": metadata.get("name", name),
                    "type": metadata.get("type", "physical"),
                    "guidelines": metadata.get("guidelines", ""),
                    "guidelines_when_running": metadata.get("guidelines_when_running", ""),
                    "inputs": metadata.get("inputs", {}),
                    "in_training": True,
                    "episode_count": episode_count,
                }
                self.get_logger().info(
                    f"In-training skill '{name}' has inputs: {metadata.get('inputs', {})}, episodes: {episode_count}"
                )
                all_skills.append(skill_info)

        response.skills_json = json.dumps(all_skills)
        self.get_logger().info(f"Returned {len(all_skills)} skills to service caller")
        return response

    def goal_callback(self, goal_request):
        self.get_logger().debug(f"Received goal for skill: '{goal_request.skill_type}'")
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        try:
            # Get the skill type from the goal handle
            skill_type = goal_handle.request.skill_type

            # Find and cancel the code skill
            if skill_type in self._code_skills:
                skill = self._code_skills[skill_type]
                self.get_logger().debug(f"Canceling code skill: {skill_type}")
                skill.cancel()
            elif skill_type in self._physical_skills:
                # Physical skills are handled by behavior_server
                # Cancellation will be forwarded by the action client
                self.get_logger().debug(f"Canceling physical skill: {skill_type}")
            else:
                self.get_logger().warning(f"Unknown skill type: {skill_type}")
        except Exception as e:
            self.get_logger().error(f"Error in cancel_callback: {str(e)}")

            # If we couldn't determine the skill type, try to cancel all code skills
            self.get_logger().debug("Attempting to cancel all code skills")
            for name, skill in self._code_skills.items():
                try:
                    skill.cancel()
                except Exception as cancel_error:
                    err_msg = f"Error canceling {name}: {str(cancel_error)}"
                    self.get_logger().error(err_msg)

        return CancelResponse.ACCEPT

    def execute_callback(self, goal_handle):
        # Trace entry into the execute callback for debugging
        self.get_logger().debug(f"[SAS] execute_callback ENTER for skill: '{goal_handle.request.skill_type}'")
        # Decode the inputs (assumed to be JSON)
        try:
            inputs = json.loads(goal_handle.request.inputs)
        except Exception as e:
            self.get_logger().error(f"Invalid JSON for inputs: {str(e)}")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message="Invalid inputs JSON",
                success_type=SkillResult.FAILURE.value,
            )

        skill_type = goal_handle.request.skill_type

        # Check if it's a code skill
        if skill_type in self._code_skills:
            return self._execute_code_skill(goal_handle, skill_type, inputs)

        # Check if it's a physical skill
        elif skill_type in self._physical_skills:
            return self._execute_physical_skill(goal_handle, skill_type, inputs)

        # Skill not found
        else:
            all_skills = list(self._code_skills.keys()) + list(self._physical_skills.keys())
            self.get_logger().error(f"Skill '{skill_type}' not available")
            self.get_logger().error(f"Available skills: {all_skills}")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message="Skill not available",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )

    def _update_skill_robot_state(self, skill):
        """Helper to update a skill's robot state from current sensor data."""
        required_states = skill.get_required_robot_states()
        if not required_states:
            return

        robot_state_to_inject = {}

        if RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64 in required_states:
            if self.last_main_camera_image is not None:
                try:
                    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
                    success, encoded_img_bytes = cv2.imencode(".jpg", self.last_main_camera_image, encode_params)
                    if success:
                        robot_state_to_inject[RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value] = base64.b64encode(
                            encoded_img_bytes.tobytes()
                        ).decode("utf-8")
                    else:
                        self.get_logger().error("Failed to encode last_main_camera_image for skill state")
                except Exception as e_img:
                    self.get_logger().error(f"Error encoding last_main_camera_image for skill: {e_img}")
            else:
                self.get_logger().warn("Skill requires LAST_MAIN_CAMERA_IMAGE_B64 but none available.")

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
                self.get_logger().warn("Skill requires LAST_ODOM but none available.")

        if RobotStateType.LAST_MAP in required_states:
            if self.last_map is not None:
                map_data_bytes = np.array(self.last_map.data, dtype=np.int8).tobytes()
                ori_map = self.last_map.info.origin.orientation
                siny_cosp_map = 2.0 * (ori_map.w * ori_map.z + ori_map.x * ori_map.y)
                cosy_cosp_map = 1.0 - 2.0 * (ori_map.y * ori_map.y + ori_map.z * ori_map.z)
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
                self.get_logger().warn("Skill requires LAST_MAP but none available.")

        if RobotStateType.LAST_HEAD_POSITION in required_states:
            if self.last_head_position is not None:
                robot_state_to_inject[RobotStateType.LAST_HEAD_POSITION.value] = self.last_head_position
            else:
                self.get_logger().warn("Skill requires LAST_HEAD_POSITION but none available.")

        if robot_state_to_inject:  # Only call if there is state to update
            skill.update_robot_state(**robot_state_to_inject)

    def _inject_required_interfaces(self, skill):
        """Inject only the interfaces declared by the skill via Interface descriptors."""
        required_interfaces = skill.get_required_interfaces()
        for interface_type in required_interfaces:
            if interface_type == InterfaceType.MANIPULATION:
                skill.inject_interface(interface_type, self.manipulation)
            elif interface_type == InterfaceType.MOBILITY:
                skill.inject_interface(interface_type, self.mobility)
            elif interface_type == InterfaceType.HEAD:
                skill.inject_interface(interface_type, self.head)

    def _state_update_thread_func(self):
        """Background thread that continuously updates robot state for running skill."""
        while not self._state_update_stop_event.is_set():
            with self._current_skill_lock:
                if self._current_skill is not None:
                    try:
                        self._update_skill_robot_state(self._current_skill)
                    except Exception as e:
                        self.get_logger().error(f"Error in continuous state update: {e}")
            # Sleep for ~50Hz updates (20ms)
            self._state_update_stop_event.wait(0.02)

    def _execute_code_skill(self, goal_handle, skill_type, inputs):
        skill = self._code_skills[skill_type]

        # Define a feedback publisher for the skill
        def _publish_feedback(update_message: str):
            feedback_msg = ExecuteSkill.Feedback()
            feedback_msg.feedback = update_message
            goal_handle.publish_feedback(feedback_msg)
            self.get_logger().debug(f"Published feedback for '{skill_type}': {update_message}")

        # Pass the feedback callback to the skill if it supports it
        skill.set_feedback_callback(_publish_feedback)

        try:
            # Initial robot state injection
            self._update_skill_robot_state(skill)

            # Start continuous state updates if skill needs real-time data
            required_states = skill.get_required_robot_states()
            if required_states:
                with self._current_skill_lock:
                    self._current_skill = skill
                # Start background thread for state updates (runs independently of executor)
                self._state_update_stop_event.clear()
                self._state_update_thread = threading.Thread(target=self._state_update_thread_func, daemon=True)
                self._state_update_thread.start()
                self.get_logger().info(f"Started continuous state updates for '{skill_type}' at 50Hz")

            # Execute the skill with its direct inputs
            result_message, result_status = skill.execute(**inputs)

            # Stop continuous state updates
            if self._state_update_thread is not None:
                self._state_update_stop_event.set()
                self._state_update_thread.join(timeout=1.0)
                self._state_update_thread = None
            with self._current_skill_lock:
                self._current_skill = None

            # Handle the result based on the SkillResult enum
            if result_status == SkillResult.SUCCESS:
                self.get_logger().info(f"Skill '{skill_type}' succeeded: {result_message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True,
                    message=result_message,
                    skill_type=skill_type,
                    success_type=SkillResult.SUCCESS.value,
                )
            elif result_status == SkillResult.CANCELLED:
                self.get_logger().info(f"Skill '{skill_type}' cancelled: {result_message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True,
                    message=result_message,
                    skill_type=skill_type,
                    success_type=SkillResult.CANCELLED.value,
                )
            else:  # SkillResult.FAILURE
                self.get_logger().info(f"Skill '{skill_type}' failed: {result_message}")
                goal_handle.abort()
                return ExecuteSkill.Result(
                    success=False,
                    message=result_message,
                    skill_type=skill_type,
                    success_type=SkillResult.FAILURE.value,
                )

        except Exception as e:
            self.get_logger().error(f"Error executing skill: {str(e)}")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message=str(e),
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )

    def _execute_physical_skill(self, goal_handle, skill_type, inputs):
        self.get_logger().info(f"Delegating physical skill '{skill_type}' to behavior_server")

        # Get the physical skill metadata
        physical_data = self._physical_skills[skill_type]
        metadata = physical_data["metadata"]

        # Wait for behavior server to be available
        if not self._behavior_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error("Behavior server not available!")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message="Behavior server not available",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )

        # Create behavior goal with config from metadata
        behavior_goal = ExecuteBehavior.Goal()
        behavior_goal.behavior_name = skill_type
        behavior_goal.behavior_config = json.dumps(metadata)  # Pass entire metadata as config

        # Send goal and wait for result
        self.get_logger().info(f"Sending behavior goal to behavior_server: {skill_type}")
        send_goal_future = self._behavior_client.send_goal_async(behavior_goal)

        # Wait for goal acceptance
        rclpy.spin_until_future_complete(self, send_goal_future, timeout_sec=5.0)

        if not send_goal_future.done():
            self.get_logger().error("Timeout waiting for behavior goal acceptance")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message="Timeout waiting for behavior goal acceptance",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )

        behavior_goal_handle = send_goal_future.result()
        if not behavior_goal_handle.accepted:
            self.get_logger().error("Behavior goal rejected by behavior_server")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message="Behavior goal rejected by behavior_server",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )

        self.get_logger().info("Behavior goal accepted, waiting for result...")

        # Wait for result
        result_future = behavior_goal_handle.get_result_async()

        # Spin until complete - this blocks but that's okay in an action callback
        rclpy.spin_until_future_complete(self, result_future)

        behavior_result = result_future.result().result

        # Convert behavior result to skill result
        if behavior_result.success:
            self.get_logger().info(f"Physical skill '{skill_type}' succeeded: {behavior_result.message}")
            goal_handle.succeed()
            return ExecuteSkill.Result(
                success=True,
                message=behavior_result.message,
                skill_type=skill_type,
                success_type=SkillResult.SUCCESS.value,
            )
        else:
            # Check if it was cancelled
            if "cancel" in behavior_result.message.lower():
                self.get_logger().info(f"Physical skill '{skill_type}' cancelled: {behavior_result.message}")
                goal_handle.succeed()
                return ExecuteSkill.Result(
                    success=True,
                    message=behavior_result.message,
                    skill_type=skill_type,
                    success_type=SkillResult.CANCELLED.value,
                )
            else:
                self.get_logger().error(f"Physical skill '{skill_type}' failed: {behavior_result.message}")
                goal_handle.abort()
                return ExecuteSkill.Result(
                    success=False,
                    message=behavior_result.message,
                    skill_type=skill_type,
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
            self.get_logger().debug("Received and decoded new image for skills.")
        except Exception as e:
            self.get_logger().error(f"Failed to decode compressed image for skill state: {e}")

    def odom_callback(self, msg: Odometry):
        self.last_odom = msg
        # self.get_logger().debug('Received new odometry for skills.')

    def map_callback(self, msg: OccupancyGrid):
        self.last_map = msg
        # self.get_logger().debug('Received new map for skills.')

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
    action_server = SkillsActionServer()
    rclpy.spin(action_server)
    action_server.destroy()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
