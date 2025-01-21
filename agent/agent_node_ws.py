import asyncio
import queue
import time
import json
import base64
import cv2  # optional if you want to encode images as JPEG in-memory
import numpy as np
import websockets
import threading


def run_agent_async(shared_queues, server_uri="ws://localhost:8765/"):
    """
    Helper that runs the async agent loop in an asyncio event loop on a separate thread.
    """
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(agent_loop_ws(shared_queues, server_uri))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


async def agent_loop_ws(shared_queues, server_uri="ws://localhost:8765"):
    """
    Example agent loop that:
    1. Connects to a WebSocket server at `server_uri`.
    2. Sends an authentication token immediately after connection.
    3. Sends an image (rgb) to the server periodically.
    4. Receives velocity commands from the server and updates the simulation.

    :param shared_queues: The SharedQueues instance for exchanging data with the simulation.
    :param server_uri: The WebSocket URI to connect to (ws://host:port).
    """
    print("AgentNode (WebSocket) started.")

    # Default velocity if no command has arrived from server
    current_command = [2.0, 2.0]
    last_send_time = 0
    send_interval = 2.0  # send an image every 2 seconds

    try:
        # Connect to the WebSocket server
        async with websockets.connect(server_uri) as websocket:
            print(f"Connected to WebSocket server at {server_uri}")

            # Send authentication token immediately after connection
            await websocket.send("MY_HARDCODED_TOKEN")

            while not shared_queues.exit_event.is_set():
                # 1) Grab latest frame from sim_to_agent queue
                #    If no frame is available, continue
                try:
                    rgb, depth = shared_queues.sim_to_agent.get(timeout=0.05)
                except queue.Empty:
                    # If no new frame, just see if there is a new cmd from the server
                    pass
                else:
                    # Optionally, do some local processing of `rgb` or `depth` here

                    # 2) Send the image to the server if enough time has passed
                    now = time.time()
                    if now - last_send_time > send_interval:
                        last_send_time = now
                        await send_image_over_ws(websocket, rgb)

                # 3) Check if there's a new message from the server
                #    Use a short timeout so we don't block forever.
                await asyncio.sleep(0.01)
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=0.01)
                except asyncio.TimeoutError:
                    # No new message arrived; keep going
                    pass
                else:
                    # Parse the incoming message (assume JSON)
                    try:
                        data = json.loads(message)
                        if data.get("cmd") == "set_velocity":
                            # data["values"] should be [left_speed, right_speed]
                            current_command = data.get("values", [2.0, 2.0])
                            print(f"Received velocity command: {current_command}")
                    except Exception as e:
                        print(f"Could not parse server message: {e}")

                # 4) Send the velocity commands to the simulation
                try:
                    shared_queues.agent_to_sim.put_nowait(current_command)
                except queue.Full:
                    # If the queue is full, we simply skip this update
                    pass

    except Exception as e:
        print(f"WebSocket connection error: {e}")

    print("AgentNode (WebSocket) stopped.")


async def send_image_over_ws(websocket, rgb_frame):
    """
    Encodes the RGB frame as JPEG in memory, then base64-encodes it
    and sends it via WebSockets as a JSON message.
    """
    # Convert BGR or RGB array to JPEG bytes in memory
    # `rgb_frame` might be 3-channel [H,W,3] (0..255)
    _, encoded_img = cv2.imencode(".jpg", rgb_frame)
    b64_img = base64.b64encode(encoded_img.tobytes()).decode("utf-8")

    # Create a JSON message
    message = {
        "type": "image",
        "image_b64": b64_img,
    }

    # Send it
    await websocket.send(json.dumps(message))
