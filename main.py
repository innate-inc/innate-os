import argparse
import time
import threading
import os
import json
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import genesis as gs
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.shared_queues import SharedQueues
from src.simulation.simulation_node import SimulationNode
from src.agent.agent_websocket_bridge import run_agent_async

# Import the new video & reset endpoints router
from src.routes.video_api import router as video_api_router
from src.routes.chat_api import router as chat_api_router
from src.routes.config_api import router as config_api_router

# Load environment variables from .env file
load_dotenv()

# Define constants
ROSBRIDGE_URI = "ws://localhost:9090"  # Connects to Innate OS running locally in Docker

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the React build directory
frontend_build_path = os.path.join(os.path.dirname(__file__), "frontend", "dist")
app.mount("/static", StaticFiles(directory=frontend_build_path), name="static")

# Include the routers
app.include_router(video_api_router)
app.include_router(chat_api_router)
app.include_router(config_api_router)

# Initialize a placeholder on the application's state so that downstream
# routers can retrieve SHARED_QUEUES.
app.state.SHARED_QUEUES = None

# -------------------------------------------------------------------------
# SHARED QUEUES
# -------------------------------------------------------------------------
SHARED_QUEUES: SharedQueues = None  # We'll populate this later


def frame_collector(shared_queues: SharedQueues):
    """
    Continuously read from sim_to_web and update the latest_frames.
    """
    while not shared_queues.exit_event.is_set():
        frames_dict = shared_queues.sim_to_web.get()  # blocks until new frames
        if frames_dict is None:
            break
        # Update the latest_frames dictionary with newly arrived frames
        for cam_name, frame in frames_dict.items():
            shared_queues.latest_frames[cam_name] = frame


def load_initial_environment_config(
    config_name: Optional[str], config_path: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Load initial environment config from name in data/environments or explicit path."""
    if config_name and config_path:
        raise ValueError(
            "Provide only one of --initial-environment or --initial-environment-path."
        )

    if not config_name and not config_path:
        return None

    project_root = os.path.dirname(os.path.abspath(__file__))

    if config_name:
        resolved_path = os.path.join(
            project_root, "data", "environments", f"{config_name}.json"
        )
    else:
        resolved_path = (
            config_path
            if os.path.isabs(config_path)
            else os.path.join(project_root, config_path)
        )

    with open(resolved_path, "r") as f:
        config = json.load(f)

    print(f"[Main] Loaded initial environment config from {resolved_path}")
    return config


# Additional endpoints and functions (if any) can be placed here.


# -------------------------------------------------------------------------
# MAIN ENTRY POINT
# -------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v", "--vis", action="store_true", default=False, help="Enable visualization"
    )
    parser.add_argument(
        "--log-everything",
        action="store_true",
        default=False,
        help="Enable logging of all model outputs",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        default=False,
        help="Run without the web server (headless mode)",
    )
    parser.add_argument(
        "--no-agent",
        action="store_true",
        default=False,
        help="Run without connecting to rosbridge/brain agent",
    )
    parser.add_argument(
        "--initial-environment",
        type=str,
        default=None,
        help="Initial environment config name from data/environments (without .json)",
    )
    parser.add_argument(
        "--initial-environment-path",
        type=str,
        default=None,
        help="Path to initial environment JSON (absolute or workspace-relative)",
    )
    parser.add_argument(
        "--disable-robot-collision",
        action="store_true",
        default=False,
        help="Disable robot collisions with scene geometry (temporary debug mode).",
    )
    args = parser.parse_args()

    # 1) Create shared queues
    global SHARED_QUEUES
    SHARED_QUEUES = SharedQueues(log_everything=args.log_everything)
    app.state.SHARED_QUEUES = SHARED_QUEUES

    # 1b) Start the background frame collector
    collector_thread = threading.Thread(
        target=frame_collector, args=(SHARED_QUEUES,), daemon=True
    )
    collector_thread.start()

    try:
        initial_env_config = load_initial_environment_config(
            config_name=args.initial_environment,
            config_path=args.initial_environment_path,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to load initial environment config: {e}") from e

    # 2) Start the simulation node
    sim_node = SimulationNode(
        SHARED_QUEUES,
        enable_vis=args.vis,
        initial_env_config=initial_env_config,
        robot_collision_enabled=not args.disable_robot_collision,
    )

    # 3) Start the agent (async) in a separate thread unless disabled
    if args.no_agent:
        print("[Main] --no-agent enabled. Skipping rosbridge/brain connection.")
    else:
        run_agent_async(
            SHARED_QUEUES,
            rosbridge_uri=ROSBRIDGE_URI,
        )

    # 4) Start Uvicorn in another thread (unless --no-web)
    if not args.no_web:

        def run_uvicorn():
            config = uvicorn.Config(
                app=app, host="0.0.0.0", port=8000, log_level="info", reload=False
            )
            server = uvicorn.Server(config)
            server.run()

        uvicorn_thread = threading.Thread(target=run_uvicorn, daemon=True)
        uvicorn_thread.start()

    # 5) Run simulation on main thread (keeps viewer + camera rendering in same OpenGL context)
    if args.vis:
        print("[Main] Visualization enabled. Close viewer window or press Q to stop.")
    else:
        print("[Main] No viewer requested. Press Ctrl+C to stop.")

    # Run simulation - this blocks until exit_event is set or viewer closes
    sim_node.run()
    print("[Main] Simulation ended.")

    # 6) Cleanup and exit
    SHARED_QUEUES.exit_event.set()
    print("[Main] All threads finished. Goodbye.")
    os._exit(0)  # Force exit to avoid hanging on stubborn threads


if __name__ == "__main__":
    main()
