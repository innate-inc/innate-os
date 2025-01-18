import argparse
import time
import threading

import genesis as gs

from shared_queues import SharedQueues
from simulation.simulation_node import SimulationNode
from agent.agent_node_ws import run_agent_async


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v", "--vis", action="store_true", default=True, help="Enable visualization"
    )
    args = parser.parse_args()

    # Create the shared queues structure
    shared_queues = SharedQueues()

    # Initialize the simulation node
    sim_node = SimulationNode(shared_queues, enable_vis=args.vis)

    # Use the async version of the agent instead of threading
    agent_thread = run_agent_async(shared_queues, server_uri="ws://localhost:8765")

    # Run the simulation node in another thread (Genesis convenience)
    gs.tools.run_in_another_thread(fn=sim_node.run, args=())

    # If visualization is requested, we drive the viewer in the main thread
    if args.vis:
        sim_node.scene.viewer.start()

    try:
        # Keep main thread alive until user interrupts
        while not shared_queues.exit_event.is_set():
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("Keyboard interrupt, shutting down...")
    finally:
        # Signal both threads to stop
        shared_queues.exit_event.set()
        agent_thread.join()
        print("Main finished.")


if __name__ == "__main__":
    main()
