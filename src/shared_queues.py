# src/shared_queues.py

from typing import NamedTuple, Optional
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
        self.sim_to_agent = queue.Queue(maxsize=10)
        self.agent_to_sim = queue.Queue(maxsize=10)
        self.sim_to_web = queue.Queue(
            maxsize=10
        )  # Will now contain dicts of named images
        self.latest_frames = {}
        self.exit_event = threading.Event()

        # Flag to indicate if all model outputs should be logged
        self.log_everything = log_everything

        # Queues specifically for chat messages
        self.chat_to_bridge = queue.Queue(maxsize=500)
        self.chat_from_bridge = queue.Queue(maxsize=500)
