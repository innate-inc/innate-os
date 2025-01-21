import asyncio
import json
import base64
import time
import cv2
import numpy as np
import websockets
import threading
import queue


async def agent_loop_ws(shared_queues, server_uri="ws://localhost:8765"):
    print("AgentNode (WebSocket) started.")

    try:
        # Connect to the WebSocket server
        async with websockets.connect(server_uri) as websocket:
            print(f"Connected to WebSocket server at {server_uri}")

            # 1) Send authentication token immediately after connection
            await websocket.send("MY_HARDCODED_TOKEN")
            print("Client -> Server: token sent")

            # Default velocity if no command arrives
            current_command = [0.0, 0.0]

            # MAIN LOOP: wait for messages from the server and respond accordingly
            while not shared_queues.exit_event.is_set():
                try:
                    # Wait for a message from the server
                    incoming_msg = await websocket.recv()
                except websockets.exceptions.ConnectionClosed:
                    print("Server closed the connection.")
                    break

                data = {}
                try:
                    data = json.loads(incoming_msg)
                except json.JSONDecodeError:
                    print("Received non-JSON data from server. Ignoring.")
                    continue

                msg_type = data.get("type")

                if msg_type == "ready_for_image":
                    # Server wants an image
                    print("Client received 'ready_for_image'")

                    # Fetch latest frame from sim_to_agent queue (non-blocking or with small timeout)
                    try:
                        rgb_frame, depth_frame = shared_queues.sim_to_agent.get(
                            timeout=0.1
                        )
                    except queue.Empty:
                        # If we don't have a new image, you could either skip or send a placeholder
                        print("No new image available in queue, sending a placeholder.")
                        rgb_frame = np.zeros((240, 320, 3), dtype=np.uint8)

                    # Send the image
                    await send_image_over_ws(websocket, rgb_frame)

                elif msg_type == "well_received":
                    # Server just acknowledged the image
                    print("Client received 'well_received'")

                elif msg_type == "vision_agent_output":
                    # Server is sending vision agent results
                    print(f"Client received vision_agent_output: {data.get('payload')}")
                    # You can process the vision agent output here if needed

                elif msg_type == "action_to_do":
                    # Server is telling us the action to do
                    print("Client received 'action_to_do'")
                    cmd = data.get("cmd")
                    values = data.get("values", [0.0, 0.0])
                    if cmd == "set_velocity":
                        current_command = values
                        print(f"Applying velocity command: {current_command}")

                        # Send the velocity commands to the simulation
                        try:
                            shared_queues.agent_to_sim.put_nowait(current_command)
                        except queue.Full:
                            print("agent_to_sim queue is full. Dropping command.")

                else:
                    print(f"Client received an unknown type: {msg_type}")

                # Small sleep so we don't spin super fast
                await asyncio.sleep(0.01)

    except Exception as e:
        print(f"WebSocket connection error: {e}")

    print("AgentNode (WebSocket) stopped.")


async def send_image_over_ws(websocket, rgb_frame):
    """
    Encodes the RGB frame as JPEG in memory, then base64-encodes it
    and sends it via WebSockets as a JSON message.
    """
    _, encoded_img = cv2.imencode(".jpg", rgb_frame)
    b64_img = base64.b64encode(encoded_img.tobytes()).decode("utf-8")

    # Create a JSON message
    message = {
        "type": "image",
        "image_b64": b64_img,
    }

    await websocket.send(json.dumps(message))
    print("Client -> Server: image sent")


def run_agent_async(shared_queues, server_uri="ws://localhost:8765"):
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
