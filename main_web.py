# main_server.py

import argparse
import time
import threading
import queue
import cv2
import platform

import genesis as gs
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from src.shared_queues import SharedQueues
from src.simulation.simulation_node import SimulationNode
from src.agent.agent_websocket_bridge import run_agent_async


# -------------------------------------------------------------------------
# FASTAPI APP
# -------------------------------------------------------------------------
app = FastAPI()
SHARED_QUEUES: SharedQueues = None  # we'll populate this later


@app.get("/")
def index():
    """Simple HTML page showing the MJPEG video feed in an <img> tag."""
    html_content = """
    <html>
      <head>
        <title>Simulation Viewer</title>
      </head>
      <body>
        <h1>Simulation MJPEG Feed</h1>
        <img src="/video_feed" style="width:640px; height:auto; border:1px solid #ccc;" />
        <p>Open this page in multiple tabs if you like. Press Ctrl+C in the server console to stop.</p>
      </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/video_feed")
def video_feed():
    """Returns a multipart/x-mixed-replace streaming response of JPEG frames."""
    return StreamingResponse(
        mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


def mjpeg_generator():
    """Continuously yields frames from SHARED_QUEUES.sim_to_web as JPEG."""
    global SHARED_QUEUES
    while True:
        if SHARED_QUEUES is None:
            time.sleep(0.1)
            continue
        frame = SHARED_QUEUES.sim_to_web.get()  # block until a frame is available
        success, encoded_image = cv2.imencode(".jpg", frame)
        if not success:
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + encoded_image.tobytes() + b"\r\n"
        )


# -------------------------------------------------------------------------
# MAIN ENTRY POINT
# -------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v", "--vis", action="store_true", help="Enable Genesis viewer in main thread."
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Use local agent server (ws://localhost:8765).",
    )
    args = parser.parse_args()

    # 1) Create shared queues
    global SHARED_QUEUES
    SHARED_QUEUES = SharedQueues()

    # 2) Start the simulation node
    sim_node = SimulationNode(SHARED_QUEUES, enable_vis=args.vis)

    # 3) Start the agent (async) in a separate thread
    agent_thread = run_agent_async(
        SHARED_QUEUES,
        server_uri=(
            "ws://localhost:8765"
            if args.local
            else "wss://innate-agent-websocket-service-533276562345.us-central1.run.app"
        ),
    )

    # 4) Start Uvicorn in another thread (so if you close the Genesis viewer, everything can shut down)
    def run_uvicorn():
        config = uvicorn.Config(
            app=app, host="0.0.0.0", port=8000, log_level="info", reload=False
        )
        server = uvicorn.Server(config)
        server.run()  # this is a blocking call

    uvicorn_thread = threading.Thread(target=run_uvicorn, daemon=True)
    uvicorn_thread.start()

    # 5) Launch simulation run() in its own thread (macOS) or directly (other platforms)
    if platform.system() == "Darwin":  # macOS
        gs.tools.run_in_another_thread(fn=sim_node.run, args=())
    else:
        sim_node.run()  # run directly on non-macOS platforms

    # 6) If visualization is requested, do the viewer in the MAIN thread
    #    Because typically rendering loops want to run in main. So we'll block here
    #    until the viewer is closed, or until user hits Ctrl+C
    if args.vis:
        try:
            sim_node.scene.viewer.start()  # blocks until the viewer closes
        except KeyboardInterrupt:
            pass
        print("[Main] Viewer closed or keyboard interrupt. Shutting down...")

    else:
        print("[Main] No viewer requested. Press Ctrl+C to stop.")
        # We can just wait in a loop for Ctrl+C
        try:
            while not SHARED_QUEUES.exit_event.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass

    # 7) On exit, signal everything to stop
    SHARED_QUEUES.exit_event.set()

    # Wait for threads to finish
    agent_thread.join()
    # uvicorn_thread is daemon=True so it should die with the process
    print("[Main] All threads finished. Goodbye.")


if __name__ == "__main__":
    main()
