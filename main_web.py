import argparse
import time
import threading
import platform
import os
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
LOCAL_ROSBRIDGE_URI = "ws://localhost:9090"
CLOUD_ROSBRIDGE_URI = (
    "wss://innate-agent-websocket-service-533276562345.us-central1.run.app"
)

app = FastAPI()

# Enable CORS with support for Authorization header
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", "Authorization"],
)

# Mount the React build directory
frontend_build_path = os.path.join(os.path.dirname(__file__), "frontend", "dist")
app.mount("/static", StaticFiles(directory=frontend_build_path), name="static")

# Include the routers
# Note: We're not adding authentication to the video_api_router yet
# to keep things simple
app.include_router(video_api_router)

# Add authentication to the chat_api_router
# This will require a valid Auth0 token for all endpoints in this router
# except for the WebSocket endpoint which handles authentication separately
# The WebSocket endpoint will handle authentication on its own
app.include_router(chat_api_router)

# Add the config_api router with authentication
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
        "--local",
        action="store_true",
        default=False,
        help="Connect to local agent server instead of cloud",
    )
    # Add Auth0 configuration arguments
    parser.add_argument(
        "--auth0-domain",
        type=str,
        default="",
        help="Auth0 domain (e.g., your-tenant.auth0.com)",
    )
    parser.add_argument(
        "--auth0-audience",
        type=str,
        default="",
        help="Auth0 API identifier",
    )
    # Add logging flag argument
    parser.add_argument(
        "--log-everything",
        action="store_true",
        default=False,
        help="Enable logging of all model outputs",
    )
    # Add authentication control argument
    parser.add_argument(
        "--need-oauth",
        type=str,
        choices=["true", "false"],
        default="true",
        help="Require OAuth authentication for chat API (default: true)",
    )
    args = parser.parse_args()

    # Set Auth0 environment variables
    if args.auth0_domain:
        os.environ["AUTH0_DOMAIN"] = args.auth0_domain
    if args.auth0_audience:
        os.environ["AUTH0_AUDIENCE"] = args.auth0_audience

    # Set authentication requirement flag
    os.environ["NEED_OAUTH"] = args.need_oauth

    # 1) Create shared queues
    global SHARED_QUEUES
    SHARED_QUEUES = SharedQueues(log_everything=args.log_everything)
    app.state.SHARED_QUEUES = SHARED_QUEUES

    # 1b) Start the background frame collector
    collector_thread = threading.Thread(
        target=frame_collector, args=(SHARED_QUEUES,), daemon=True
    )
    collector_thread.start()

    # 2) Start the simulation node
    sim_node = SimulationNode(
        SHARED_QUEUES,
        enable_vis=args.vis,
        env_config_path="data/environments/lying_man_corner.json",
    )

    # 3) Start the agent (async) in a separate thread
    agent_thread = run_agent_async(
        SHARED_QUEUES,
        rosbridge_uri=(LOCAL_ROSBRIDGE_URI if args.local else CLOUD_ROSBRIDGE_URI),
    )

    # 4) Start Uvicorn in another thread so the Genesis viewer and FastAPI
    # server run concurrently
    def run_uvicorn():
        config = uvicorn.Config(
            app=app, host="0.0.0.0", port=8000, log_level="info", reload=False
        )
        server = uvicorn.Server(config)
        server.run()

    uvicorn_thread = threading.Thread(target=run_uvicorn, daemon=True)
    uvicorn_thread.start()

    # 5) Launch simulation run() in its own thread (macOS) or directly
    # (other platforms)
    if platform.system() == "Darwin":
        gs.tools.run_in_another_thread(fn=sim_node.run, args=())
    else:
        sim_node.run()

    # 6) If visualization is requested, drive the viewer in the main thread
    if args.vis:
        try:
            sim_node.scene.viewer.start()
        except KeyboardInterrupt:
            pass
        print("[Main] Viewer closed or keyboard interrupt. Shutting down...")
    else:
        print("[Main] No viewer requested. Press Ctrl+C to stop.")
        try:
            while not SHARED_QUEUES.exit_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass

    # 7) On exit, signal all threads to stop
    SHARED_QUEUES.exit_event.set()
    agent_thread.join()
    print("[Main] All threads finished. Goodbye.")


if __name__ == "__main__":
    main()
