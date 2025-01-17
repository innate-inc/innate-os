import time
import queue
import numpy as np


def agent_loop(shared_queues):
    """
    Subscribes to images from the simulation; publishes velocity commands.
    This example loop always sends [2.0, 2.0] as the forward velocity.
    """
    print("AgentNode started.")

    while not shared_queues.exit_event.is_set():
        try:
            # Wait for a new frame from the simulation
            rgb, depth = shared_queues.sim_to_agent.get(timeout=0.1)

            # TODO: Insert your agent’s logic for processing `rgb`, `depth`, etc.

            # Send new command
            new_command = [2.0, 2.0]
            try:
                shared_queues.agent_to_sim.put_nowait(new_command)
            except queue.Full:
                pass

        except queue.Empty:
            continue

    print("AgentNode stopped.")
