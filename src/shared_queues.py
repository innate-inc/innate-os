import threading
import queue


class SharedQueues:
    """
    Minimal message broker:
    - sim_to_agent: images (and optionally robot poses)
    - agent_to_sim: control commands
    """

    def __init__(self):
        self.sim_to_agent = queue.Queue(maxsize=1)
        self.agent_to_sim = queue.Queue(maxsize=1)
        self.exit_event = threading.Event()
