#!/usr/bin/env python3

import asyncio
import base64
import json
import queue
import threading
import time
import websockets

import cv2
import numpy as np
from src.agent.types import ImageMsg, VelocityCmd, CommentMsg


#
# Rosbridge Utility Methods
#
def rosbridge_subscribe(topic: str, msg_type: str) -> dict:
    """
    Build a JSON message to tell rosbridge: "subscribe to a given topic"
    E.g.:
        {
          "op": "subscribe",
          "topic": "/cmd_vel",
          "type": "geometry_msgs/msg/Twist"
        }
    """
    return {"op": "subscribe", "topic": topic, "type": msg_type}


def rosbridge_publish(topic: str, msg_dict: dict) -> dict:
    """
    Build a JSON message to publish to `topic` with `msg_dict` content.
    E.g.:
        {
          "op": "publish",
          "topic": "/camera/color/image_raw",
          "msg": {...}
        }
    """
    return {"op": "publish", "topic": topic, "msg": msg_dict}


#
# Primary async task that connects to rosbridge and does two-way communication
#
async def rosbridge_loop(shared_queues, rosbridge_uri: str):
    """
    Connect to rosbridge at rosbridge_uri (e.g., ws://localhost:9090), then:
      - Subscribe to /cmd_vel (geometry_msgs/msg/Twist)
      - Subscribe to /comment_bell (std_msgs/msg/String)
      - Periodically publish images from `shared_queues.sim_to_agent`.
      - Forward any received velocity commands into `shared_queues.agent_to_sim`.
    """
    print(f"[ROSBridge] Connecting to {rosbridge_uri} ...")
    try:
        async with websockets.connect(rosbridge_uri) as ws:
            print(f"[ROSBridge] Connected to {rosbridge_uri}")

            # Subscribe to /cmd_vel (geometry_msgs/msg/Twist)
            sub_cmd_vel = rosbridge_subscribe("/cmd_vel", "geometry_msgs/msg/Twist")
            await ws.send(json.dumps(sub_cmd_vel))

            # Subscribe to /comment_bell (std_msgs/msg/String) if desired
            sub_comment = rosbridge_subscribe("/comment_bell", "std_msgs/msg/String")
            await ws.send(json.dumps(sub_comment))

            # Main loop
            while not shared_queues.exit_event.is_set():
                # 1) Publish images if available
                try:
                    # Non-blocking or short-timeout get
                    img_msg = shared_queues.sim_to_agent.get_nowait()
                    if isinstance(img_msg, ImageMsg):
                        # Publish the image to rosbridge as sensor_msgs/Image
                        await publish_rgb_image(ws, img_msg.rgb_frame)
                        # Potentially also publish depth image if desired
                        # e.g., await publish_depth_image(ws, img_msg.depth_frame)
                except queue.Empty:
                    pass

                # 2) Process inbound rosbridge messages
                #    We do a non-blocking read (with small timeout)
                #    If there's no data, we skip
                try:
                    incoming_raw = await asyncio.wait_for(ws.recv(), timeout=0.01)
                except asyncio.TimeoutError:
                    # Normal: no message
                    await asyncio.sleep(0.01)
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print("[ROSBridge] Connection closed.")
                    break

                # Parse inbound
                try:
                    inbound_data = json.loads(incoming_raw)
                except json.JSONDecodeError:
                    # Not valid JSON
                    continue

                # We only care about "op" = "publish"
                if inbound_data.get("op") == "publish":
                    topic = inbound_data.get("topic", "")
                    msg = inbound_data.get("msg", {})

                    # If it's /cmd_vel, parse the twist and forward to the sim
                    if topic == "/cmd_vel":
                        vel_cmd = parse_twist(msg)
                        if vel_cmd:
                            # Put into agent_to_sim queue for your sim
                            try:
                                shared_queues.agent_to_sim.put_nowait(vel_cmd)
                            except queue.Full:
                                print(
                                    "[ROSBridge] agent_to_sim queue is full. Dropping cmd_vel."
                                )

                    # If it's /comment_bell, parse the string data
                    elif topic == "/comment_bell":
                        comment = msg.get("data", "")
                        comment_msg = CommentMsg(text=comment)
                        # For example, store in agent_to_sim or a separate queue
                        try:
                            shared_queues.comment_queue.put_nowait(comment_msg)
                        except queue.Full:
                            print(
                                "[ROSBridge] comment_queue is full. Dropping comment."
                            )

                # Just short pause so we don't spin the CPU
                await asyncio.sleep(0.001)

    except Exception as e:
        print(f"[ROSBridge] Connection error: {e}")

    print("[ROSBridge] Stopped rosbridge_loop.")


#
# Helper: Publish an RGB image as sensor_msgs/Image (Base64-encoded data)
#
async def publish_rgb_image(
    websocket, rgb_frame: np.ndarray, topic: str = "/camera/color/image_raw"
):
    """
    Convert `rgb_frame` (H x W x 3) into a ROS sensor_msgs/Image structure,
    then send via rosbridge.
    """
    if rgb_frame is None:
        return

    # Convert to JPEG
    ret, encoded_jpeg = cv2.imencode(".jpg", rgb_frame)
    if not ret:
        print("[ROSBridge] Could not encode image.")
        return

    b64_img = base64.b64encode(encoded_jpeg.tobytes()).decode("utf-8")

    # Fake a header stamp
    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": "camera_color_frame",
        },
        "height": rgb_frame.shape[0],
        "width": rgb_frame.shape[1],
        "encoding": "jpeg",  # For rosbridge, "jpeg" is often used, or "rgb8"
        "is_bigendian": 0,
        "step": len(encoded_jpeg),  # not strictly correct for "jpeg," but works
        "data": b64_img,
    }

    outbound = rosbridge_publish(topic, msg)
    await websocket.send(json.dumps(outbound))
    # Debug
    print(f"[ROSBridge] Published image on {topic}")


#
# Helper: parse geometry_msgs/Twist from inbound rosbridge JSON
#
def parse_twist(msg: dict) -> VelocityCmd | None:
    """
    Example geometry_msgs/Twist:
    {
      "linear": {"x": 0.1, "y": 0, "z": 0},
      "angular": {"x": 0, "y": 0, "z": 0.5}
    }
    """
    lin = msg.get("linear", {})
    ang = msg.get("angular", {})
    try:
        vx = float(lin.get("x", 0.0))
        vz = float(ang.get("z", 0.0))
        return VelocityCmd(linear_x=vx, angular_z=vz)
    except (TypeError, ValueError):
        return None


#
# Main entry point that spawns the rosbridge loop on a separate thread
#
def run_agent_async(shared_queues, rosbridge_uri="ws://localhost:9090"):
    """
    Launch the asynchronous rosbridge_loop in a dedicated thread.
    This mimics your 'agent_loop_ws' pattern, but for rosbridge.
    """
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(rosbridge_loop(shared_queues, rosbridge_uri))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
