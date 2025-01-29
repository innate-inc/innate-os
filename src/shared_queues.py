# src/shared_queues.py

import threading
import queue


class SharedQueues:
    """
    Minimal message broker:
    - sim_to_agent: images (and optionally robot poses)
    - agent_to_sim: control commands
    - sim_to_web: dictionary of named images for web streaming
    """

    def __init__(self):
        self.sim_to_agent = queue.Queue(maxsize=10)
        self.agent_to_sim = queue.Queue(maxsize=10)
        self.sim_to_web = queue.Queue(
            maxsize=10
        )  # Will now contain dicts of named images
        self.exit_event = threading.Event()
