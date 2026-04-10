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
from pathlib import Path

import numpy as np  # For map data
import rclpy

# Import the action definition – ensure that it is built and available.
from brain_messages.action import ExecuteBehavior, ExecuteSkill
from brain_messages.msg import AvailableSkills, SkillInfo
from brain_messages.srv import CreatePhysicalSkill
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

# Import ROS message types for subscriptions
from std_msgs.msg import String
from std_srvs.srv import Trigger

from brain_client.camera_provider import CameraProvider
from brain_client.head_interface import HeadInterface
from brain_client.manipulation_interface import ManipulationInterface
from brain_client.mobility_interface import MobilityInterface

# Import skill loader and types
from brain_client.hot_reload_watcher import HotReloadWatcher
from brain_client.skill_loader import SkillLoader
from brain_client.skill_types import (
    InterfaceType,
    RobotStateType,
    Skill,
    SkillResult,
)


class SkillsActionServer(Node):
    def __init__(self):
        super().__init__("skills_action_server")

        # Camera images handled by a dedicated lightweight node (own thread)
        self._camera_node = CameraProvider()

        # Robot state storage
        self.last_odom = None  # Stores Odometry message
        self.last_map = None  # Stores OccupancyGrid message
        self.last_head_position = None  # Stores head position dict (parsed JSON)

        # Track currently executing skill for continuous state updates
        self._current_skill = None
        self._current_skill_lock = threading.Lock()
        self._state_update_thread = None
        self._state_update_stop_event = threading.Event()

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

        # Subscribers created on-demand via _start_state_subscriptions()
        self._odom_sub = None
        self._map_sub = None
        self._head_position_sub = None

        # Create manipulation interface for skills to use (subs also lazy)
        self.manipulation = ManipulationInterface(self, self.get_logger(), lazy=True)
        # Create mobility interface for base/wheel motion
        self.mobility = MobilityInterface(self, self.get_logger(), self.cmd_vel_topic)
        # Create head interface for head tilt control
        self.head = HeadInterface(self, self.get_logger(), self.head_position_topic)

        # Dynamic skill loading
        self.skill_loader = SkillLoader(self.get_logger())

        # Define and store directories for code/physical skill discovery
        self._skills_directories = self._resolve_skills_directories()

        # Load all code skills dynamically from all configured directories
        discovered_skills = self.skill_loader.load_skills_from_directories(self._skills_directories)

        self.get_logger().info(
            f"Discovered skills: {list(discovered_skills.keys())} in directories {self._skills_directories}"
        )

        # Build ID-keyed dict: {skill_id: (name, skill_class, source_path)}
        id_keyed: dict[str, tuple[str, type, Path]] = {}
        for display_name, (cls, src_path) in discovered_skills.items():
            sid = self._compute_skill_id(src_path)
            id_keyed[sid] = (display_name, cls, src_path)

        # Handle special case for navigation skill based on simulator mode
        self._apply_sim_swap(id_keyed)

        # Lock that guards _code_skills, _physical_skills, and _in_training_skills
        # against concurrent access from the HotReloadWatcher background thread.
        self._skills_lock = threading.Lock()

        # Create code skill instances keyed by ID
        self._code_skills: dict[str, tuple[str, Skill]] = {}  # {id: (display_name, instance)}
        for skill_id, (display_name, skill_class, _src) in id_keyed.items():
            try:
                skill_instance = skill_class(self.get_logger())
                skill_instance.node = self  # Inject the node
                self._inject_required_interfaces(skill_instance)
                self._code_skills[skill_id] = (display_name, skill_instance)
                self.get_logger().info(f"Loaded code skill: {skill_id} ({display_name})")
            except Exception as e:
                self.get_logger().error(f"Error instantiating skill {skill_id}: {e}")

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

        # Create unified latched topic for broadcasting available skills
        self._skills_qos = QoSProfile(
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE,
        )
        self._skills_publisher = self.create_publisher(
            AvailableSkills, "/brain/available_skills", self._skills_qos
        )

        # Create service for reloading skills
        self._reload_srv = self.create_service(Trigger, "/brain/reload_primitives", self._handle_reload_skills)

        # Create service for creating new physical skills
        self._create_physical_skill_srv = self.create_service(
            CreatePhysicalSkill,
            "/brain/create_physical_skill",
            self._handle_create_physical_skill,
        )

        # Create service for selective skill reload (PEAS-only, called by brain_client)
        from brain_messages.srv import ReloadSkillsAgents
        self._reload_skills_srv = self.create_service(
            ReloadSkillsAgents, "/brain/reload_skills", self._handle_reload_skills_agents
        )

        self.get_logger().debug("Skills Action Server has started.")
        self.get_logger().info(
            f"Total skills available: {len(self._code_skills) + len(self._physical_skills)}"
        )

        # Publish initial skills list on the latched topic
        self._publish_skills_list()

        # Start hot reload watcher for skills directories
        self._hot_reload_watcher = HotReloadWatcher(
            logger=self.get_logger(),
            skills_directories=self._skills_directories,
            agents_directories=[],  # SAS doesn't handle agents
            on_reload=self._on_skills_file_changed,
            debounce_seconds=1.0,
        )
        self._hot_reload_watcher.start()

    def _on_skills_file_changed(self, skill_names: list, _agent_names: list):
        """Called by HotReloadWatcher when skill files change.
        
        Note: skill_names here are file stems from the watcher, not IDs.
        """
        self.get_logger().info(f"Hot reload triggered for skills: {skill_names}")
        if skill_names:
            # Convert file stems to skill IDs for selective reload
            skill_ids = []
            for stem in skill_names:
                for d in self._skills_directories:
                    py_file = Path(d) / f"{stem}.py"
                    subdir = Path(d) / stem
                    if py_file.exists():
                        skill_ids.append(self._compute_skill_id(py_file))
                        break
                    elif subdir.is_dir():
                        skill_ids.append(self._compute_skill_id(subdir))
                        break
            self._reload_skills_selective(skill_ids)
        else:
            self._reload_skills()

    def _build_skill_info(self, skill_id: str, name: str, skill_type: str, guidelines: str,
                          guidelines_when_running: str, inputs_json: str,
                          in_training: bool = False, episode_count: int = 0,
                          directory: str = "") -> SkillInfo:
        """Create a SkillInfo message from skill data."""
        msg = SkillInfo()
        msg.id = skill_id or ""
        msg.name = name or ""
        msg.type = skill_type or ""
        msg.guidelines = guidelines or ""
        msg.guidelines_when_running = guidelines_when_running or ""
        msg.inputs_json = inputs_json or ""
        msg.in_training = bool(in_training)
        msg.episode_count = int(episode_count or 0)
        msg.directory = directory or ""
        return msg

    def _publish_skills_list(self):
        """Build and publish the full AvailableSkills message on the latched topic."""
        msg = AvailableSkills()
        skills = []

        # Snapshot the skill dicts under lock to avoid iteration during reload.
        with self._skills_lock:
            code_skills_snapshot = dict(self._code_skills)
            physical_skills_snapshot = dict(self._physical_skills)
            in_training_skills_snapshot = dict(self._in_training_skills)

        # Add code skills (dict is {id: (display_name, instance)})
        for skill_id, (display_name, skill_instance) in code_skills_snapshot.items():
            inputs = {}
            if hasattr(skill_instance, "execute"):
                signature = inspect.signature(skill_instance.execute)
                for param_name, param in signature.parameters.items():
                    if param_name == "self":
                        continue
                    param_type = "any"
                    if param.annotation != inspect.Parameter.empty:
                        if (
                            isinstance(param.annotation, (types.UnionType, types.GenericAlias))
                            or hasattr(param.annotation, "_name")
                            and param.annotation._name in ["List", "Optional", "Dict", "Tuple", "Union"]
                        ):
                            param_type = str(param.annotation)
                        elif hasattr(param.annotation, "__name__"):
                            param_type = param.annotation.__name__
                        else:
                            param_type = str(param.annotation)
                        param_type = param_type.replace("typing.", "")
                    inputs[param_name] = param_type

            skills.append(self._build_skill_info(
                skill_id=skill_id,
                name=display_name,
                skill_type="code",
                guidelines=(skill_instance.guidelines() if hasattr(skill_instance, "guidelines") else ""),
                guidelines_when_running=(
                    skill_instance.guidelines_when_running()
                    if hasattr(skill_instance, "guidelines_when_running")
                    else ""
                ),
                inputs_json=json.dumps(inputs),
            ))

        # Add physical skills (ready) — dict is {id: skill_data}
        for skill_id, physical_data in physical_skills_snapshot.items():
            metadata = physical_data["metadata"]
            episode_count = self.skill_loader._get_episode_count(physical_data["directory"])
            skills.append(self._build_skill_info(
                skill_id=skill_id,
                name=metadata.get("name", skill_id),
                skill_type=metadata.get("type", "physical"),
                guidelines=metadata.get("guidelines", ""),
                guidelines_when_running=metadata.get("guidelines_when_running", ""),
                inputs_json=json.dumps(metadata.get("inputs", {})),
                in_training=False,
                episode_count=episode_count,
                directory=physical_data["directory"],
            ))

        # Add in-training skills — dict is {id: skill_data}
        for skill_id, physical_data in in_training_skills_snapshot.items():
            metadata = physical_data["metadata"]
            episode_count = self.skill_loader._get_episode_count(physical_data["directory"])
            skills.append(self._build_skill_info(
                skill_id=skill_id,
                name=metadata.get("name", skill_id),
                skill_type=metadata.get("type", "physical"),
                guidelines=metadata.get("guidelines", ""),
                guidelines_when_running=metadata.get("guidelines_when_running", ""),
                inputs_json=json.dumps(metadata.get("inputs", {})),
                in_training=True,
                episode_count=episode_count,
                directory=physical_data["directory"],
            ))

        # Enforce unique display names (LLM can't disambiguate duplicates)
        filtered_skills = []
        seen_names: dict[str, str] = {}  # {name: first_id}
        for s in skills:
            if s.name in seen_names:
                self.get_logger().error(
                    f"DUPLICATE skill name '{s.name}' between {seen_names[s.name]} and {s.id}. "
                    f"Skipping '{s.id}' — rename the skill to fix this."
                )
                continue  # skip the duplicate, keep the rest
            seen_names[s.name] = s.id
            filtered_skills.append(s)

        msg.skills = filtered_skills
        self._skills_publisher.publish(msg)
        self.get_logger().info(f"Published {len(skills)} skills on /brain/available_skills")

    def _reload_skills(self):
        """Reload all skills from disk."""
        self.get_logger().info("Reloading skills...")

        # Refresh directories list (root skills + user skills)
        self._skills_directories = self._resolve_skills_directories()

        # Reload code skills from all configured directories
        discovered_skills = self.skill_loader.load_skills_from_directories(self._skills_directories)

        # Build ID-keyed dict
        id_keyed: dict[str, tuple[str, type, Path]] = {}
        for display_name, (cls, src_path) in discovered_skills.items():
            sid = self._compute_skill_id(src_path)
            id_keyed[sid] = (display_name, cls, src_path)

        # Handle simulator mode nav swap
        self._apply_sim_swap(id_keyed)

        # Build new dicts locally, then swap under lock to avoid a window
        # where _code_skills is empty while still being populated.
        new_code_skills: dict[str, tuple[str, Skill]] = {}
        for skill_id, (display_name, cls, _src) in id_keyed.items():
            try:
                instance = cls(self.get_logger())
                instance.node = self
                self._inject_required_interfaces(instance)
                new_code_skills[skill_id] = (display_name, instance)
                self.get_logger().info(f"Reloaded code skill: {skill_id}")
            except Exception as e:
                self.get_logger().error(f"Error instantiating {skill_id}: {e}")

        # Reload physical skills (from all skill directories)
        new_physical, new_in_training = self._load_physical_skills(self._skills_directories)

        with self._skills_lock:
            self._code_skills = new_code_skills
            self._physical_skills = new_physical
            self._in_training_skills = new_in_training

        self.get_logger().info(
            f"Reloaded {len(new_code_skills)} code + {len(new_physical)} physical skills"
        )
        self._publish_skills_list()

    def _resolve_skills_directories(self) -> list[str]:
        """Build the ordered list of skill directories to scan."""
        maurice_root = os.environ.get(
            "INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")
        )
        self._innate_os_skills_dir = os.path.join(maurice_root, "skills")
        user_skills_directory = os.path.join(os.path.expanduser("~"), "skills")

        if not os.path.exists(self._innate_os_skills_dir):
            self.get_logger().fatal(f"Skills directory not found: {self._innate_os_skills_dir}")
            raise FileNotFoundError(f"Skills directory must exist at {self._innate_os_skills_dir}")

        # Match initializer behavior: ensure ~/skills is always available.
        os.makedirs(user_skills_directory, exist_ok=True)

        self.get_logger().info(f"Scanning root skills directory: {self._innate_os_skills_dir}")
        self.get_logger().info(f"Scanning user skills directory: {user_skills_directory}")
        return [self._innate_os_skills_dir, user_skills_directory]

    def _apply_sim_swap(self, id_keyed: dict[str, tuple[str, type, Path]]) -> None:
        """Swap navigate_to_position_sim → navigate_to_position in sim mode, or remove it.

        Mutates *id_keyed* in place.
        """
        sim_id = "innate-os/navigate_to_position_sim"
        real_id = "innate-os/navigate_to_position"
        if self.simulator_mode and sim_id in id_keyed:
            self.get_logger().info("Simulator mode: using NavigateToPositionSim for navigate_to_position")
            _name, cls, src = id_keyed.pop(sim_id)
            id_keyed[real_id] = ("navigate_to_position", cls, src)
        elif sim_id in id_keyed:
            self.get_logger().info("Real robot mode: removing sim navigation skill")
            del id_keyed[sim_id]

    def _compute_skill_id(self, path: str | Path) -> str:
        """Compute a deterministic skill ID from a file or directory path.

        Returns 'innate-os/<basename>' for paths under $INNATE_OS_ROOT/skills,
        'local/<basename>' for paths under ~/skills.
        """
        path = str(Path(path).resolve())
        root = str(Path(self._innate_os_skills_dir).resolve())
        basename = Path(path).stem if path.endswith(".py") else Path(path).name
        if path.startswith(root):
            return f"innate-os/{basename}"
        return f"local/{basename}"

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

    def _handle_create_physical_skill(self, request, response):
        """Create a new physical (learned) skill directory with metadata.json.

        Creates the skill under the user skills directory (~/skills/) with a
        kebab-case directory name derived from the display name.
        """
        try:
            display_name = request.name.strip()
            if not display_name:
                response.success = False
                response.message = "Skill name cannot be empty."
                response.skill_directory = ""
                response.skill_id = ""
                return response

            # Convert display name to kebab-case directory name
            import re
            dir_name = re.sub(r'[^a-zA-Z0-9\s-]', '', display_name)
            dir_name = re.sub(r'\s+', '-', dir_name).strip('-').lower()
            if not dir_name:
                response.success = False
                response.message = f"Cannot derive valid directory name from '{display_name}'."
                response.skill_directory = ""
                response.skill_id = ""
                return response

            # Create under user skills directory (last in the list, ~/skills)
            user_skills_dir = self._skills_directories[-1]
            skill_dir = os.path.join(user_skills_dir, dir_name)

            if os.path.exists(os.path.join(skill_dir, "metadata.json")):
                self.get_logger().info(f"Skill '{display_name}' already exists at {skill_dir}. Returning existing directory.")
                response.success = True
                response.message = f"Skill already exists at {skill_dir}."
                response.skill_directory = skill_dir
                response.skill_id = self._compute_skill_id(skill_dir)
                return response

            os.makedirs(skill_dir, exist_ok=True)

            metadata = {
                "name": display_name,
                "type": "learned",
                "guidelines": "",
                "guidelines_when_running": "",
                "inputs": {},
                "execution": {},
            }
            metadata_path = os.path.join(skill_dir, "metadata.json")
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=4)

            self.get_logger().info(f"Created physical skill '{display_name}' at {skill_dir}")

            # Reload so skills_action_server picks it up immediately
            self._reload_skills()

            response.success = True
            response.message = f"Created skill '{display_name}' at {skill_dir}."
            response.skill_directory = skill_dir
            response.skill_id = self._compute_skill_id(skill_dir)
        except Exception as e:
            self.get_logger().error(f"Error creating physical skill: {e}")
            response.success = False
            response.message = f"Failed to create skill: {e}"
            response.skill_directory = ""
            response.skill_id = ""
        return response

    def _handle_reload_skills_agents(self, request, response):
        """Service handler for selectively reloading specific skills (by ID)."""
        try:
            skill_ids = list(request.skills) if request.skills else []
            reloaded = self._reload_skills_selective(skill_ids)
            response.success = True
            response.reloaded_skills = reloaded
            response.reloaded_agents = []  # Skills server doesn't handle agents
            response.message = f"Reloaded {len(reloaded)} skills: {reloaded}"
        except Exception as e:
            response.success = False
            response.message = f"Failed to reload skills: {e}"
            response.reloaded_skills = []
            response.reloaded_agents = []
        return response

    def _reload_skills_selective(self, skill_ids: list[str]) -> list[str]:
        """Reload specific skills by ID. Empty list means reload all.

        Returns list of skill IDs that were successfully reloaded.
        """
        if not skill_ids:
            self._reload_skills()
            with self._skills_lock:
                return list(self._code_skills.keys()) + list(self._physical_skills.keys())

        self.get_logger().info(f"Selectively reloading skills: {skill_ids}")
        reloaded = []

        for skill_id in skill_ids:
            # Extract the basename from the ID (e.g. 'innate-os/foo' -> 'foo')
            basename = skill_id.split("/", 1)[-1] if "/" in skill_id else skill_id

            # Determine skill category under lock.
            with self._skills_lock:
                is_code = skill_id in self._code_skills or self._is_code_skill_id(skill_id)
                is_physical = (not is_code) and (
                    skill_id in self._physical_skills or skill_id in self._in_training_skills
                )

            # Check if it's a code skill
            if is_code:
                result = self.skill_loader.reload_skill_by_file_stem(basename, self._skills_directories)
                if result is not None:
                    cls, src_path = result
                    display_name = self.skill_loader._get_skill_name(cls)
                    try:
                        instance = cls(self.get_logger())
                        instance.node = self
                        self._inject_required_interfaces(instance)
                        with self._skills_lock:
                            self._code_skills[skill_id] = (display_name, instance)
                        reloaded.append(skill_id)
                        self.get_logger().info(f"Reloaded code skill: {skill_id}")
                    except Exception as e:
                        self.get_logger().error(f"Error instantiating {skill_id}: {e}")

            # Check if it's a physical skill
            elif is_physical:
                if self._reload_physical_skill(skill_id):
                    reloaded.append(skill_id)

        self.get_logger().info(f"Selectively reloaded {len(reloaded)} skills")
        self._publish_skills_list()
        return reloaded

    def _is_code_skill_id(self, skill_id: str) -> bool:
        """Check if a skill ID corresponds to a code skill file."""
        basename = skill_id.split("/", 1)[-1] if "/" in skill_id else skill_id
        for directory in self._skills_directories:
            if (Path(directory) / f"{basename}.py").exists():
                return True
        return False

    def _reload_physical_skill(self, skill_id: str) -> bool:
        """Reload a single physical skill's metadata by ID."""
        basename = skill_id.split("/", 1)[-1] if "/" in skill_id else skill_id
        for skills_directory in self._skills_directories:
            skill_path = os.path.join(skills_directory, basename)
            metadata_path = os.path.join(skill_path, "metadata.json")
            
            if os.path.exists(metadata_path):
                try:
                    with open(metadata_path) as f:
                        metadata = json.load(f)
                    
                    is_valid, is_in_training, episode_count = self.skill_loader.validate_physical_skill(
                        skill_path, metadata
                    )
                    
                    if is_valid:
                        skill_data = {
                            "metadata": metadata,
                            "directory": skill_path,
                            "in_training": is_in_training,
                            "episode_count": episode_count,
                        }
                        
                        with self._skills_lock:
                            if is_in_training:
                                self._in_training_skills[skill_id] = skill_data
                                self._physical_skills.pop(skill_id, None)
                            else:
                                self._physical_skills[skill_id] = skill_data
                                self._in_training_skills.pop(skill_id, None)
                        
                        self.get_logger().info(f"Reloaded physical skill: {skill_id}")
                        return True
                except Exception as e:
                    self.get_logger().error(f"Error reloading physical skill {skill_id}: {e}")
        
        return False

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
                            skill_id = self._compute_skill_id(Path(item_path))

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
                                    in_training_skills[skill_id] = skill_data
                                    self.get_logger().info(
                                        f"Loaded in-training skill: {skill_id} (type: {metadata.get('type', 'unknown')})"
                                    )
                                else:
                                    physical_skills[skill_id] = skill_data
                                    self.get_logger().info(
                                        f"Loaded physical skill: {skill_id} (type: {metadata.get('type', 'unknown')})"
                                    )
                            else:
                                self.get_logger().warn(f"Skipped invalid physical skill: {skill_id}")

        return physical_skills, in_training_skills

    def goal_callback(self, goal_request):
        self.get_logger().debug(f"Received goal for skill: '{goal_request.skill_type}'")
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        try:
            # Get the skill type (ID) from the goal handle
            skill_type = goal_handle.request.skill_type

            # Snapshot under lock to make check-then-access atomic.
            with self._skills_lock:
                code_entry = self._code_skills.get(skill_type)
                is_physical = skill_type in self._physical_skills
                all_code = list(self._code_skills.items()) if code_entry is None and not is_physical else []

            # Find and cancel the code skill
            if code_entry is not None:
                _name, instance = code_entry
                self.get_logger().debug(f"Canceling code skill: {skill_type}")
                instance.cancel()
            elif is_physical:
                self.get_logger().debug(f"Canceling physical skill: {skill_type}")
            else:
                self.get_logger().warning(f"Unknown skill type: {skill_type}")
        except Exception as e:
            self.get_logger().error(f"Error in cancel_callback: {str(e)}")

            # If we couldn't determine the skill type, try to cancel all code skills
            self.get_logger().debug("Attempting to cancel all code skills")
            with self._skills_lock:
                all_code = list(self._code_skills.items())
            for sid, (_name, instance) in all_code:
                try:
                    instance.cancel()
                except Exception as cancel_error:
                    err_msg = f"Error canceling {sid}: {str(cancel_error)}"
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

        # Snapshot the skill entry under lock so check-then-access is atomic.
        with self._skills_lock:
            code_entry = self._code_skills.get(skill_type)
            physical_entry = self._physical_skills.get(skill_type)

        # Check if it's a code skill
        if code_entry is not None:
            return self._execute_code_skill(goal_handle, skill_type, inputs)

        # Check if it's a physical skill
        elif physical_entry is not None:
            return self._execute_physical_skill(goal_handle, skill_type, inputs)

        # Skill not found
        else:
            with self._skills_lock:
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
            b64 = self._camera_node.last_main_camera_b64
            if b64 is not None:
                robot_state_to_inject[RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value] = b64
            else:
                self.get_logger().warn("Skill requires LAST_MAIN_CAMERA_IMAGE_B64 but none available.")

        if RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64 in required_states:
            b64 = self._camera_node.last_wrist_camera_b64
            if b64 is not None:
                robot_state_to_inject[RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64.value] = b64
            else:
                self.get_logger().warn("Skill requires LAST_WRIST_CAMERA_IMAGE_B64 but none available.")

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
        with self._skills_lock:
            entry = self._code_skills.get(skill_type)
        if entry is None:
            self.get_logger().error(f"Code skill '{skill_type}' disappeared during reload")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message=f"Skill '{skill_type}' was removed during a concurrent reload",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )
        _name, skill = entry

        # Define a feedback publisher for the skill
        def _publish_feedback(update_message: str, image_b64: str = None):
            feedback_msg = ExecuteSkill.Feedback()
            feedback_msg.feedback = update_message
            feedback_msg.image_b64 = image_b64 or ""
            goal_handle.publish_feedback(feedback_msg)
            self.get_logger().debug(f"Published feedback for '{skill_type}': {update_message}")

        # Pass the feedback callback to the skill if it supports it
        skill.set_feedback_callback(_publish_feedback)

        required_states = skill.get_required_robot_states()
        needs_camera = required_states and (
            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64 in required_states
            or RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64 in required_states
        )

        try:
            self._start_state_subscriptions()

            if needs_camera:
                self._camera_node.start()

            # Initial robot state injection
            self._update_skill_robot_state(skill)

            # Start continuous state updates if skill needs real-time data
            if required_states:
                with self._current_skill_lock:
                    self._current_skill = skill
                self._state_update_stop_event.clear()
                self._state_update_thread = threading.Thread(target=self._state_update_thread_func, daemon=True)
                self._state_update_thread.start()
                self.get_logger().info(f"Started continuous state updates for '{skill_type}' at 50Hz")

            # Execute the skill with its direct inputs
            result_message, result_status = skill.execute(**inputs)

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
        finally:
            if self._state_update_thread is not None:
                self._state_update_stop_event.set()
                self._state_update_thread.join(timeout=1.0)
                self._state_update_thread = None
            with self._current_skill_lock:
                self._current_skill = None
            if needs_camera:
                self._camera_node.stop()
            self._stop_state_subscriptions()

    def _execute_physical_skill(self, goal_handle, skill_type, inputs):
        self.get_logger().info(f"Delegating physical skill '{skill_type}' to behavior_server")

        # Get the physical skill metadata under lock
        with self._skills_lock:
            physical_data = self._physical_skills.get(skill_type)
        if physical_data is None:
            self.get_logger().error(f"Physical skill '{skill_type}' disappeared during reload")
            goal_handle.abort()
            return ExecuteSkill.Result(
                success=False,
                message=f"Skill '{skill_type}' was removed during a concurrent reload",
                skill_type=skill_type,
                success_type=SkillResult.FAILURE.value,
            )
        metadata = physical_data["metadata"]

        self._start_state_subscriptions()
        try:
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
            behavior_goal.skill_dir = physical_data["directory"]
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
        finally:
            self._stop_state_subscriptions()

    def destroy(self):
        if hasattr(self, '_hot_reload_watcher'):
            self._hot_reload_watcher.stop()
        self._camera_node.shutdown()
        self._action_server.destroy()
        super().destroy_node()

    def _start_state_subscriptions(self):
        """Create robot-state subscriptions needed during skill execution."""
        if self._odom_sub is not None:
            return
        self._odom_sub = self.create_subscription(Odometry, "/odom", self.odom_callback, 10)
        self._map_sub = self.create_subscription(OccupancyGrid, "/map", self.map_callback, 1)
        self._head_position_sub = self.create_subscription(
            String, self.head_current_position_topic, self.head_position_callback, 10
        )
        self.manipulation.start()

    def _stop_state_subscriptions(self):
        """Destroy robot-state subscriptions when no skill is running."""
        for sub in (self._odom_sub, self._map_sub, self._head_position_sub):
            if sub is not None:
                self.destroy_subscription(sub)
        self._odom_sub = None
        self._map_sub = None
        self._head_position_sub = None
        self.manipulation.stop()
        self.last_odom = None
        self.last_map = None
        self.last_head_position = None

    # Callbacks for state subscriptions
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
    try:
        while rclpy.ok():
            rclpy.spin_once(action_server, timeout_sec=1.0)
    except KeyboardInterrupt:
        pass
    action_server.destroy()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
