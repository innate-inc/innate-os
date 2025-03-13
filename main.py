import argparse
import time
import platform

import genesis as gs

from src.shared_queues import SharedQueues
from src.simulation.simulation_node import SimulationNode
from src.agent.agent_websocket_bridge import run_agent_async


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v", "--vis", action="store_true", default=False, help="Enable visualization"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        default=False,
        help="Connect to local agent server instead of cloud",
    )
    parser.add_argument(
        "--log-everything",
        action="store_true",
        default=False,
        help="Enable logging of all model outputs",
    )
    args = parser.parse_args()

    # Create the shared queues structure
    shared_queues = SharedQueues(log_everything=args.log_everything)

    # Initialize the simulation node
    sim_node = SimulationNode(shared_queues, enable_vis=args.vis)

    # Use the async version of the agent instead of threading
    agent_thread = run_agent_async(
        shared_queues,
        rosbridge_uri=(
            "ws://localhost:9090"
            if args.local
            else "wss://innate-agent-websocket-service-533276562345.us-central1.run.app"
        ),
    )

    # Run the simulation node in another thread (Genesis convenience)
    if platform.system() == "Darwin":  # macOS
        gs.tools.run_in_another_thread(fn=sim_node.run, args=())
    else:
        sim_node.run()  # run directly on non-macOS platforms

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
