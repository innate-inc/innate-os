#!/usr/bin/env python3

import asyncio
import base64
import json
import math
import queue
import threading
import time
import traceback

import cv2
import numpy as np
import websockets

from src.agent.types import (
    OccupancyGridMsg,
    RobotStateMsg,
    VelocityCmd,
)
from src.shared_queues import SharedQueues, ChatMessage


def np_encoder(obj):
    """JSON serializer that converts numpy.* types to native Python types."""
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


#
# Rosbridge Utility Methods
#
def rosbridge_subscribe(topic: str, msg_type: str) -> dict:
    return {"op": "subscribe", "topic": topic, "type": msg_type}


def rosbridge_advertise(topic: str, msg_type: str) -> dict:
    return {"op": "advertise", "topic": topic, "type": msg_type}


def rosbridge_publish(topic: str, msg_dict: dict) -> dict:
    return {"op": "publish", "topic": topic, "msg": msg_dict}


async def inbound_loop(ws, shared_queues):
    """
    Continuously receive inbound messages on the WebSocket (e.g. /cmd_vel)
    and push them to shared_queues.agent_to_sim.
    """
    print("[ROSBridge] inbound_loop started.")
    while not shared_queues.exit_event.is_set():
        try:
            # Wait for incoming message
            inbound_raw = await asyncio.wait_for(ws.recv(), timeout=0.01)
        except asyncio.TimeoutError:
            # No message arrived in that time slice
            await asyncio.sleep(0.001)
            continue
        except websockets.exceptions.ConnectionClosed:
            print("[ROSBridge] Connection closed in inbound_loop.")
            break

        # parse inbound JSON
        try:
            inbound_data = json.loads(inbound_raw)
        except json.JSONDecodeError:
            continue

        # If inbound data is a published message, might be /cmd_vel
        if inbound_data.get("op") == "publish":
            topic = inbound_data.get("topic", "")
            msg_data = inbound_data.get("msg", {})
            if topic == "/cmd_vel":
                vel_cmd = parse_twist(msg_data)
                if vel_cmd:
                    try:
                        shared_queues.agent_to_sim.put_nowait(vel_cmd)
                    except queue.Full:
                        pass

        await asyncio.sleep(0.0001)

    print("[ROSBridge] inbound_loop stopped.")


async def outbound_loop(ws, shared_queues):
    """
    Continuously process messages from sim_to_agent and publish them
    to rosbridge (camera images, odometry, map, etc.).
    """
    print("[ROSBridge] outbound_loop started.")

    # First, advertise topics once
    adv_color = rosbridge_advertise(
        "/camera/color/image_raw/compressed", "sensor_msgs/msg/CompressedImage"
    )
    adv_depth = rosbridge_advertise("/camera/depth/image_raw", "sensor_msgs/msg/Image")
    adv_cinfo = rosbridge_advertise(
        "/camera/color/camera_info", "sensor_msgs/msg/CameraInfo"
    )
    adv_odom = rosbridge_advertise("/odom", "nav_msgs/msg/Odometry")
    adv_map = rosbridge_advertise("/map", "nav_msgs/msg/OccupancyGrid")

    await ws.send(json.dumps(adv_color))
    await ws.send(json.dumps(adv_depth))
    await ws.send(json.dumps(adv_cinfo))
    await ws.send(json.dumps(adv_odom))
    await ws.send(json.dumps(adv_map))

    print(
        "[ROSBridge] Advertised /camera/color/image_raw/compressed, /camera/depth/image_raw, /camera/color/camera_info, /odom, /map"
    )

    # Also subscribe to /cmd_vel
    sub_cmd_vel = rosbridge_subscribe("/cmd_vel", "geometry_msgs/msg/Twist")
    await ws.send(json.dumps(sub_cmd_vel))
    print("[ROSBridge] Subscribed to /cmd_vel")

    while not shared_queues.exit_event.is_set():
        # a) Check if there's a message from the sim
        try:
            msg = shared_queues.sim_to_agent.get_nowait()
        except queue.Empty:
            # no messages to publish right now
            await asyncio.sleep(0.001)
            continue

        # We have a message
        if isinstance(msg, RobotStateMsg):
            await publish_robot_state(ws, msg)
        elif isinstance(msg, OccupancyGridMsg):
            await publish_occupancy_grid(ws, msg)
        else:
            # unknown or unhandled
            pass

        await asyncio.sleep(0.0001)

    print("[ROSBridge] outbound_loop stopped.")


async def rosbridge_loop(shared_queues, rosbridge_uri: str):
    """
    High-level function that connects to rosbridge and starts
    two concurrent tasks: inbound_loop & outbound_loop.
    """
    print(f"[ROSBridge] Connecting to {rosbridge_uri} ...")
    try:
        async with websockets.connect(rosbridge_uri) as ws:
            print(f"[ROSBridge] Connected to {rosbridge_uri}")

            # Run inbound & outbound in parallel
            tasks = []
            tasks.append(asyncio.create_task(inbound_loop(ws, shared_queues)))
            tasks.append(asyncio.create_task(outbound_loop(ws, shared_queues)))
            tasks.append(asyncio.create_task(chat_bridge_loop(shared_queues)))

            # Wait for both tasks to complete (they'll stop when exit_event is set or ws closes)
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
    except Exception as e:
        print(f"[ROSBridge] Connection error: {e}")
        print(f"Stack trace: {traceback.format_exc()}")

    print("[ROSBridge] Stopped rosbridge_loop.")


#
# Publish RobotStateMsg =>
#   /camera/color/image_raw (sensor_msgs/Image)
#   /camera/depth/image_raw (sensor_msgs/Image)
#   /camera/color/camera_info (sensor_msgs/CameraInfo)
#   /odom (nav_msgs/Odometry)
#
async def publish_robot_state(ws, rsm: RobotStateMsg):
    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    # If you want to keep publishing the occupancy grid, odometry, etc. as usual, no changes needed there.

    # -- 1) COMPRESS COLOR IMAGE --
    if rsm.rgb_frame is not None:
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]  # 70% quality
        ret, encoded_img = cv2.imencode(".jpg", rsm.rgb_frame, encode_params)

        if ret:
            jpg_bytes = encoded_img.tobytes()
            base64_jpg = base64.b64encode(jpg_bytes).decode("utf-8")

            # Build a sensor_msgs/CompressedImage message
            compressed_msg = {
                "header": {
                    "stamp": {"sec": sec, "nanosec": nsec},
                    "frame_id": rsm.frame_id,
                },
                "format": "jpeg",  # official field in CompressedImage
                "data": base64_jpg,  # base64-encoded JPEG data
            }
            outbound = rosbridge_publish(
                "/camera/color/image_raw/compressed", compressed_msg
            )
            await ws.send(json.dumps(outbound))

    # 2) Depth image
    if rsm.depth_frame is not None:
        dframe = rsm.depth_frame
        depth_data = dframe.tobytes()
        # If dframe.dtype is uint16 => "16UC1"; if float32 => "32FC1"
        encoding = "16UC1" if dframe.dtype == np.uint16 else "32FC1"
        bytes_per_pixel = 2 if dframe.dtype == np.uint16 else 4

        depth_msg = {
            "header": {
                "stamp": {"sec": sec, "nanosec": nsec},
                "frame_id": "camera_depth_frame",
            },
            "height": dframe.shape[0],
            "width": dframe.shape[1],
            "encoding": encoding,
            "is_bigendian": 0,
            "step": dframe.shape[1] * bytes_per_pixel,
            "data": base64.b64encode(depth_data).decode("utf-8"),
        }
        outbound = rosbridge_publish("/camera/depth/image_raw", depth_msg)
        await ws.send(json.dumps(outbound))

    # 3) Camera Info
    ci_msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": rsm.frame_id,
        },
        "height": rsm.height,
        "width": rsm.width,
        "distortion_model": rsm.distortion_model,
        "d": rsm.D if rsm.D else [0.0, 0.0, 0.0, 0.0, 0.0],
        "k": [rsm.fx, 0.0, rsm.cx, 0.0, rsm.fy, rsm.cy, 0.0, 0.0, 1.0],
        "r": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "p": [rsm.fx, 0.0, rsm.cx, 0.0, 0.0, rsm.fy, rsm.cy, 0.0, 0.0, 0.0, 1.0, 0.0],
        "binning_x": 1,
        "binning_y": 1,
    }
    outbound = rosbridge_publish("/camera/color/camera_info", ci_msg)
    # await ws.send(json.dumps(outbound)) # Not necessary for now

    # 4) Odometry => /odom (nav_msgs/Odometry)
    odom_msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": "odom",
        },
        "child_frame_id": "base_footprint",
        "pose": {
            "pose": {
                "position": {"x": rsm.px, "y": rsm.py, "z": rsm.pz},
                "orientation": {"x": rsm.ox, "y": rsm.oy, "z": rsm.oz, "w": rsm.ow},
            },
            "covariance": [0.0] * 36,
        },
        "twist": {
            "twist": {
                "linear": {"x": rsm.vx, "y": rsm.vy, "z": rsm.vz},
                "angular": {"x": rsm.wx, "y": rsm.wy, "z": rsm.wz},
            },
            "covariance": [0.0] * 36,
        },
    }
    outbound = rosbridge_publish("/odom", odom_msg)
    await ws.send(json.dumps(outbound, default=np_encoder))


async def publish_occupancy_grid(ws, og: OccupancyGridMsg):
    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    data_3d = og.data

    # Convert 3D (H,W,3) to grayscale if needed:
    # but let's assume you've already made it 2D
    if len(data_3d.shape) == 3:
        # E.g. average across last dimension:
        data_3d = np.mean(data_3d, axis=-1).astype(np.uint8)

    # Now 'data_3d' should be shape (H,W).
    # CLAMP to [-1..100], cast to int8
    data_clamped = np.clip(data_3d, -1, 100).astype(np.int8)

    if len(data_clamped.shape) == 2:
        flat_data = data_clamped.flatten()
    else:
        flat_data = data_clamped

    # Convert yaw -> quaternion
    half_yaw = og.origin_yaw * 0.5
    qz = math.sin(half_yaw)
    qw = math.cos(half_yaw)

    map_msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": og.frame_id,
        },
        "info": {
            "map_load_time": {"sec": sec, "nanosec": nsec},
            "resolution": og.resolution,
            "width": og.width,
            "height": og.height,
            "origin": {
                "position": {
                    "x": og.origin_x,
                    "y": og.origin_y,
                    "z": og.origin_z,
                },
                "orientation": {
                    "x": 0.0,
                    "y": 0.0,
                    "z": qz,
                    "w": qw,
                },
            },
        },
        "data": flat_data.tolist(),
    }

    outbound = rosbridge_publish("/map", map_msg)
    await ws.send(json.dumps(outbound))


#
# Helper: parse geometry_msgs/Twist from inbound rosbridge JSON
#
def parse_twist(msg: dict) -> VelocityCmd | None:
    lin = msg.get("linear", {})
    ang = msg.get("angular", {})
    try:
        vx = float(lin.get("x", 0.0))
        vz = float(ang.get("z", 0.0))
        return VelocityCmd(linear_x=vx, angular_z=vz)
    except (TypeError, ValueError):
        return None


def run_agent_async(shared_queues, rosbridge_uri="ws://localhost:9090"):
    """
    Launch the asynchronous rosbridge_loop in a dedicated thread.
    """
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(rosbridge_loop(shared_queues, rosbridge_uri))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


bridge_chat_history = []


async def chat_bridge_loop(shared_queues: SharedQueues):
    """
    Continuously checks for new messages from the web (via chat_to_bridge)
    and appends them to 'bridge_chat_history'. Also pushes them into
    chat_from_bridge if you want to broadcast them further.
    """
    print("[ChatBridge] chat_bridge_loop started.")
    while not shared_queues.exit_event.is_set():
        try:
            new_msg: ChatMessage = shared_queues.chat_to_bridge.get_nowait()
            # Store these messages in local chat history
            bridge_chat_history.append(new_msg)

            # Optionally push out to 'chat_from_bridge' for broadcast
            shared_queues.chat_from_bridge.put_nowait(new_msg)
        except queue.Empty:
            pass

        await asyncio.sleep(0.01)

    print("[ChatBridge] chat_bridge_loop stopped.")
