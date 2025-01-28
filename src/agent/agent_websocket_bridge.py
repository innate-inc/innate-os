#!/usr/bin/env python3

import asyncio
import base64
import json
import queue
import threading
import time

import numpy as np
import websockets

from src.agent.types import CameraInfoMsg, ImageMsg, VelocityCmd


#
# Rosbridge Utility Methods
#
def rosbridge_subscribe(topic: str, msg_type: str) -> dict:
    """
    Build a JSON message to tell rosbridge: "subscribe to a given topic".
    Example:
        {
          "op": "subscribe",
          "topic": "/cmd_vel",
          "type": "geometry_msgs/msg/Twist"
        }
    """
    return {"op": "subscribe", "topic": topic, "type": msg_type}


def rosbridge_advertise(topic: str, msg_type: str) -> dict:
    """
    Build a JSON message to advertise a topic for publishing.
    Example:
        {
          "op": "advertise",
          "topic": "/camera/color/image_raw",
          "type": "sensor_msgs/msg/Image"
        }
    """
    return {"op": "advertise", "topic": topic, "type": msg_type}


def rosbridge_publish(topic: str, msg_dict: dict) -> dict:
    """
    Build a JSON message to publish to `topic` with `msg_dict` content.
    Example:
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
      - Advertise /camera/color/image_raw (sensor_msgs/msg/Image)
      - Periodically publish images and camera info from `shared_queues.sim_to_agent`.
      - Forward any received velocity commands into `shared_queues.agent_to_sim`.
    """
    print(f"[ROSBridge] Connecting to {rosbridge_uri} ...")
    try:
        async with websockets.connect(rosbridge_uri) as ws:
            print(f"[ROSBridge] Connected to {rosbridge_uri}")

            #
            # 1. Advertise any topics we intend to publish
            #
            # For example, we'll publish color images on /camera/color/image_raw
            advertise_color_image = rosbridge_advertise(
                "/camera/color/image_raw", "sensor_msgs/msg/Image"
            )

            await ws.send(json.dumps(advertise_color_image))
            print("[ROSBridge] Advertised /camera/color/image_raw")

            advertise_depth_image = rosbridge_advertise(
                "/camera/depth/image_raw", "sensor_msgs/msg/Image"
            )
            await ws.send(json.dumps(advertise_depth_image))
            print("[ROSBridge] Advertised /camera/depth/image_raw")

            advertise_camera_info = rosbridge_advertise(
                "/camera/color/camera_info", "sensor_msgs/msg/CameraInfo"
            )
            await ws.send(json.dumps(advertise_camera_info))
            print("[ROSBridge] Advertised /camera/color/camera_info")

            #
            # 2. Subscribe to /cmd_vel (geometry_msgs/msg/Twist)
            #
            sub_cmd_vel = rosbridge_subscribe("/cmd_vel", "geometry_msgs/msg/Twist")
            await ws.send(json.dumps(sub_cmd_vel))
            print("[ROSBridge] Subscribed to /cmd_vel")

            #
            # Main loop
            #
            while not shared_queues.exit_event.is_set():
                # a) Publish images if available
                try:
                    msg = shared_queues.sim_to_agent.get_nowait()
                    if isinstance(msg, ImageMsg):
                        await publish_rgb_image(ws, msg.rgb_frame)
                        await publish_depth_image(ws, msg.depth_frame)
                    elif isinstance(msg, CameraInfoMsg):
                        await publish_camera_info(ws, msg)
                except queue.Empty:
                    pass

                # b) Process inbound rosbridge messages
                try:
                    incoming_raw = await asyncio.wait_for(ws.recv(), timeout=0.01)
                except asyncio.TimeoutError:
                    # No message this cycle
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
                            try:
                                shared_queues.agent_to_sim.put_nowait(vel_cmd)
                            except queue.Full:
                                print(
                                    "[ROSBridge] agent_to_sim queue is full. Dropping cmd_vel."
                                )

                # Small pause
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

    # Fake a header stamp
    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    raw_data = rgb_frame.tobytes()  # or .flatten().tobytes()

    # Build sensor_msgs/Image fields
    msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": "camera_color_frame",
        },
        "height": rgb_frame.shape[0],
        "width": rgb_frame.shape[1],
        "encoding": "bgr8",
        "is_bigendian": 0,
        "step": rgb_frame.shape[1] * 3,  # For BGR8, 3 bytes per pixel
        "data": base64.b64encode(raw_data).decode(
            "utf-8"
        ),  # but typically you'd just send raw bytes over rosbridge
    }

    outbound = rosbridge_publish(topic, msg)
    await websocket.send(json.dumps(outbound))
    # Debug
    # print(f"[ROSBridge] Published image on {topic}")


async def publish_depth_image(
    websocket, depth_frame: np.ndarray, topic="/camera/depth/image_raw"
):
    """
    depth_frame: a 2D numpy array (H x W) with dtype=np.uint16 or np.float32
                 representing the depth in mm or meters.
    """
    if depth_frame is None:
        return

    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    # Convert to bytes
    raw_data = (
        depth_frame.tobytes()
    )  # If dtype=np.uint16 => 2 bytes/pixel; if float32 => 4 bytes/pixel

    height, width = depth_frame.shape[:2]

    encoding = "16UC1"  # or "32FC1" if using float32
    bytes_per_pixel = 2  # or 4 if float32

    msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": "camera_depth_frame",
        },
        "height": height,
        "width": width,
        "encoding": encoding,
        "is_bigendian": 0,
        "step": width * bytes_per_pixel,
        "data": base64.b64encode(raw_data).decode("utf-8"),
    }

    outbound = rosbridge_publish(topic, msg)
    await websocket.send(json.dumps(outbound))


async def publish_camera_info(ws, ci: CameraInfoMsg, topic="/camera/color/camera_info"):
    """
    Convert our CameraInfoMsg into sensor_msgs/CameraInfo fields, then publish via rosbridge.
    """
    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    # Minimal sensor_msgs/CameraInfo
    msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": ci.frame_id,
        },
        "height": ci.height,
        "width": ci.width,
        "distortion_model": ci.distortion_model,
        "d": ci.D if ci.D else [0.0, 0.0, 0.0, 0.0, 0.0],
        "k": [ci.fx, 0.0, ci.cx, 0.0, ci.fy, ci.cy, 0.0, 0.0, 1.0],
        "r": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "p": [ci.fx, 0.0, ci.cx, 0.0, 0.0, ci.fy, ci.cy, 0.0, 0.0, 0.0, 1.0, 0.0],
        "binning_x": 1,
        "binning_y": 1,
        # ... more fields if you like, but these are the main ones
    }

    outbound = rosbridge_publish(topic, msg)
    await ws.send(json.dumps(outbound))


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
