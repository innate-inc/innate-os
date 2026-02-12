# src/shared_queues.py

from typing import NamedTuple, Optional, List, Tuple, Dict, Any
import threading
import queue


class ChatMessage(NamedTuple):
    sender: str
    text: str
    timestamp: float
    timestamp_put_in_queue: Optional[float]  # Used to check if the message was lost


class ChatSignal(NamedTuple):
    signal: str
    timestamp: float


class AgentInfo(NamedTuple):
    """Information about an available agent/directive."""

    id: str
    display_name: str
    display_icon: Optional[str]  # Base64-encoded icon data or None
    prompt: str
    skills: List[str]


class SharedQueues:
    """
    Minimal message broker:
    - sim_to_agent: images (and optionally robot poses)
    - agent_to_sim: control commands
    - sim_to_web: dictionary of named images for web streaming
    """

    def __init__(self, log_everything=False):
        self.sim_to_agent = queue.Queue(maxsize=100)  # Commands, nav status, etc.
        self.agent_to_sim = queue.Queue(maxsize=100)
        self.sim_to_web = queue.Queue(
            maxsize=100
        )  # Will now contain dicts of named images
        self.latest_frames = {}
        self.exit_event = threading.Event()

        # Separate size-1 queue for camera/sensor data - always keeps only latest frame
        # This prevents image data from backing up and causing latency
        self.sensor_to_agent = queue.Queue(maxsize=1)

        # Flag to indicate if all model outputs should be logged
        self.log_everything = log_everything

        # Queues specifically for chat messages
        self.chat_to_bridge = queue.Queue(maxsize=5000)
        self.chat_from_bridge = queue.Queue(maxsize=5000)

        # Store the latest robot position and orientation for direct access
        # Format: [x, y, z]
        self.latest_robot_position: List[float] = [0.0, 0.0, 0.0]
        # Format: [ox, oy, oz, ow] (quaternion)
        self.latest_robot_orientation: List[float] = [0.0, 0.0, 0.0, 1.0]
        self.robot_position_timestamp: float = 0.0
        self.robot_position_lock = threading.Lock()  # For thread-safe updates

        # Store available agents/directives from the robot
        self.available_agents: List[AgentInfo] = []
        self.current_agent_id: Optional[str] = None
        self.startup_agent_id: Optional[str] = None
        self.agents_lock = threading.Lock()  # For thread-safe updates

        # One-shot status map for /set_environment request/response.
        # Key: request_id, Value: {"success": bool, "error": Optional[str]}
        self.environment_apply_results: Dict[str, Dict[str, Any]] = {}
        self.environment_apply_lock = threading.Lock()

    def update_robot_position(
        self, x: float, y: float, z: float, timestamp: float = None
    ):
        """Thread-safe method to update the robot's position"""
        if timestamp is None:
            import time

            timestamp = time.time()

        with self.robot_position_lock:
            self.latest_robot_position = [x, y, z]
            self.robot_position_timestamp = timestamp

    def update_robot_pose(
        self,
        x: float,
        y: float,
        z: float,
        ox: float,
        oy: float,
        oz: float,
        ow: float,
        timestamp: float = None,
    ):
        """Thread-safe method to update the robot's position and orientation"""
        if timestamp is None:
            import time

            timestamp = time.time()

        with self.robot_position_lock:
            self.latest_robot_position = [x, y, z]
            self.latest_robot_orientation = [ox, oy, oz, ow]
            self.robot_position_timestamp = timestamp

    def get_robot_position(self) -> Tuple[List[float], float]:
        """Thread-safe method to get the robot's position and timestamp"""
        with self.robot_position_lock:
            return self.latest_robot_position.copy(), self.robot_position_timestamp

    def get_robot_pose(self) -> Tuple[List[float], List[float], float]:
        """Thread-safe method to get the robot's position, orientation and timestamp"""
        with self.robot_position_lock:
            return (
                self.latest_robot_position.copy(),
                self.latest_robot_orientation.copy(),
                self.robot_position_timestamp,
            )

    def update_available_agents(
        self,
        agents: List[AgentInfo],
        current_agent_id: Optional[str] = None,
        startup_agent_id: Optional[str] = None,
    ):
        """Thread-safe method to update available agents from the robot"""
        with self.agents_lock:
            self.available_agents = agents
            self.current_agent_id = current_agent_id
            self.startup_agent_id = startup_agent_id

    def get_available_agents(
        self,
    ) -> Tuple[List[AgentInfo], Optional[str], Optional[str]]:
        """Thread-safe method to get available agents, current agent, and startup agent"""
        with self.agents_lock:
            return (
                self.available_agents.copy(),
                self.current_agent_id,
                self.startup_agent_id,
            )

    def set_environment_apply_result(
        self, request_id: Optional[str], success: bool, error: Optional[str] = None
    ) -> None:
        """Store result for a set_environment request ID."""
        if not request_id:
            return
        with self.environment_apply_lock:
            self.environment_apply_results[request_id] = {
                "success": success,
                "error": error,
            }

    def pop_environment_apply_result(
        self, request_id: str
    ) -> Optional[Dict[str, Any]]:
        """Pop and return a set_environment result if available."""
        with self.environment_apply_lock:
            return self.environment_apply_results.pop(request_id, None)
