# src/shared_queues.py

from typing import NamedTuple, Optional, List, Tuple
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


class SharedQueues:
    """
    Minimal message broker:
    - sim_to_agent: images (and optionally robot poses)
    - agent_to_sim: control commands
    - sim_to_web: dictionary of named images for web streaming
    """

    def __init__(self, log_everything=False):
        self.sim_to_agent = queue.Queue(maxsize=100)
        self.agent_to_sim = queue.Queue(maxsize=100)
        self.sim_to_web = queue.Queue(
            maxsize=100
        )  # Will now contain dicts of named images
        self.latest_frames = {}
        self.exit_event = threading.Event()

        # Flag to indicate if all model outputs should be logged
        self.log_everything = log_everything

        # Queues specifically for chat messages
        self.chat_to_bridge = queue.Queue(maxsize=5000)
        self.chat_from_bridge = queue.Queue(maxsize=5000)

        # Store the latest robot position for direct access
        # Format: [x, y, z]
        self.latest_robot_position: List[float] = [0.0, 0.0, 0.0]
        self.robot_position_timestamp: float = 0.0
        self.robot_position_lock = threading.Lock()  # For thread-safe updates

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

    def get_robot_position(self) -> Tuple[List[float], float]:
        """Thread-safe method to get the robot's position and timestamp"""
        with self.robot_position_lock:
            return self.latest_robot_position.copy(), self.robot_position_timestamp
