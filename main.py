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

    # Run the simulation node
    sim_node.run()

    # If visualization is requested, keep main thread alive for viewer
    if args.vis:
        try:
            # Keep the simulation running and responsive
            while not shared_queues.exit_event.is_set():
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        print("Keyboard interrupt, shutting down...")
    else:
        print("No viewer requested. Press Ctrl+C to stop.")
        try:
            while not shared_queues.exit_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass

    # Signal both threads to stop
    shared_queues.exit_event.set()
    agent_thread.join()
    print("Main finished.")


if __name__ == "__main__":
    main()
