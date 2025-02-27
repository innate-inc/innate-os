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

from src.agent.types import OccupancyGridMsg, RobotStateMsg, VelocityCmd, DirectiveCmd
from src.shared_queues import ChatMessage, ChatSignal


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


def rosbridge_call_service(service: str, srv_type: str) -> dict:
    return {"op": "call_service", "service": service, "type": srv_type}


async def inbound_loop(ws, shared_queues):
    """
    Continuously receive inbound messages on the WebSocket (e.g. /cmd_vel, /chat_in, service calls)
    and push them to shared_queues if needed.
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

        op_type = inbound_data.get("op", "")

        # Process publish messages
        if op_type == "publish":
            topic = inbound_data.get("topic", "")
            msg_data = inbound_data.get("msg", {})

            # 1) /cmd_vel
            if topic == "/cmd_vel":
                vel_cmd = parse_twist(msg_data)
                if vel_cmd:
                    try:
                        shared_queues.agent_to_sim.put_nowait(vel_cmd)
                    except queue.Full:
                        pass

            # 2) /chat_out
            elif topic == "/chat_out":
                payload = json.loads(msg_data.get("data", ""))
                sender = payload.get("sender", "")
                text = payload.get("text", "")
                timestamp = payload.get("timestamp", time.time())
                if sender and text:
                    chat_msg = ChatMessage(
                        sender=sender,
                        text=text,
                        timestamp=timestamp,
                        timestamp_put_in_queue=time.time(),
                    )
                    # Forward to sim
                    shared_queues.chat_from_bridge.put_nowait(chat_msg)

        # Process service responses (responses to service calls that we initiated)
        elif op_type == "service_response":
            service_name = inbound_data.get("service", "")
            if service_name == "/get_chat_history":
                # Check if inbound_data result field is True, otherwise the brain is not ready yet
                if not inbound_data.get("result", False):
                    continue

                # Extract the history data from the response
                history_data_raw = inbound_data.get("values", {}).get("history", "")

                # If history_data is a JSON string, parse it; otherwise assume it's already a list.
                if isinstance(history_data_raw, str):
                    try:
                        history_data = json.loads(history_data_raw)
                    except json.JSONDecodeError:
                        print("[ROSBridge] Failed to parse chat history JSON string.")
                        history_data = []
                else:
                    history_data = history_data_raw

                # Iterate through the history and forward each message to the simulation queue.
                for chat in history_data:
                    try:
                        chat_msg = ChatMessage(
                            sender=chat.get("sender", ""),
                            text=chat.get("text", ""),
                            timestamp=chat.get("timestamp", time.time()),
                            timestamp_put_in_queue=time.time(),
                        )
                        print(f"[ROSBridge] Chat message added to queue: {chat_msg}")
                        try:
                            shared_queues.chat_from_bridge.put_nowait(chat_msg)
                        except queue.Full:
                            print(
                                "[ROSBridge] chat_from_bridge queue is full; dropping chat message."
                            )
                    except Exception as e:
                        print(f"[ROSBridge] Error processing chat history entry: {e}")

        await asyncio.sleep(0.0001)

    print("[ROSBridge] inbound_loop stopped.")


async def outbound_loop(ws, shared_queues):
    """
    Continuously process messages from sim_to_agent and publish them
    to rosbridge (camera images, odometry, map, etc.).
    Also advertise relevant topics/services once at start.
    """
    print("[ROSBridge] outbound_loop started.")

    # First, advertise standard topics once
    adv_color = rosbridge_advertise(
        "/camera/color/image_raw/compressed", "sensor_msgs/msg/CompressedImage"
    )
    adv_depth = rosbridge_advertise("/camera/depth/image_raw", "sensor_msgs/msg/Image")
    adv_cinfo = rosbridge_advertise(
        "/camera/color/camera_info", "sensor_msgs/msg/CameraInfo"
    )
    adv_odom = rosbridge_advertise("/odom", "nav_msgs/msg/Odometry")
    adv_map = rosbridge_advertise("/map", "nav_msgs/msg/OccupancyGrid")
    adv_chat_in = rosbridge_advertise("/chat_in", "std_msgs/msg/String")
    adv_set_directive = rosbridge_advertise("/set_directive", "std_msgs/msg/String")

    await ws.send(json.dumps(adv_color))
    await ws.send(json.dumps(adv_depth))
    await ws.send(json.dumps(adv_cinfo))
    await ws.send(json.dumps(adv_odom))
    await ws.send(json.dumps(adv_map))
    await ws.send(json.dumps(adv_chat_in))
    await ws.send(json.dumps(adv_set_directive))

    print("[ROSBridge] Advertised camera-related topics, /odom, /map, and /chat_in")

    # Also subscribe to /cmd_vel, /chat_out
    sub_cmd_vel = rosbridge_subscribe("/cmd_vel", "geometry_msgs/msg/Twist")
    sub_chat_out = rosbridge_subscribe("/chat_out", "std_msgs/msg/String")
    await ws.send(json.dumps(sub_cmd_vel))
    await ws.send(json.dumps(sub_chat_out))
    print("[ROSBridge] Subscribed to /cmd_vel and /chat_out")

    while not shared_queues.exit_event.is_set():
        # a) Check if there's a message from the sim
        try:
            msg = shared_queues.sim_to_agent.get_nowait()

            # We have a message
            if isinstance(msg, RobotStateMsg):
                await publish_robot_state(ws, msg)
            elif isinstance(msg, OccupancyGridMsg):
                await publish_occupancy_grid(ws, msg)
            elif isinstance(msg, DirectiveCmd):
                directive_msg = {"data": msg.directive}
                outbound = rosbridge_publish("/set_directive", directive_msg)
                await ws.send(json.dumps(outbound))
                print(f"[ROSBridge] Published directive: {msg.directive}")
        except queue.Empty:
            # no messages to publish right now
            pass

        try:
            msg = shared_queues.chat_to_bridge.get_nowait()

            if isinstance(msg, ChatMessage):
                print(f"[ROSBridge] Publishing chat message: {msg.text}")
                outbound_text = {"data": msg.text}
                outbound = rosbridge_publish("/chat_in", outbound_text)
                await ws.send(json.dumps(outbound))

                # Also, add the chat message to the simulation queue as confirmation
                try:
                    shared_queues.chat_from_bridge.put_nowait(msg)
                except queue.Full:
                    pass

            elif isinstance(msg, ChatSignal):
                print(f"[ROSBridge] Publishing chat signal: {msg.signal}")
                # Check what the signal is
                if msg.signal == "ready":
                    # Use the service to get the chat history
                    srv_chat_history = rosbridge_call_service(
                        "/get_chat_history", "brain_messages/srv/GetChatHistory"
                    )
                    await ws.send(json.dumps(srv_chat_history))
        except queue.Empty:
            # no messages to publish right now
            pass

        await asyncio.sleep(0.0001)

    print("[ROSBridge] outbound_loop stopped.")


async def rosbridge_loop(shared_queues, rosbridge_uri: str):
    """
    High-level function that connects to rosbridge and starts
    two concurrent tasks: inbound_loop & outbound_loop & chat_bridge_loop.
    """
    print(f"[ROSBridge] Connecting to {rosbridge_uri} ...")
    try:
        async with websockets.connect(rosbridge_uri) as ws:
            print(f"[ROSBridge] Connected to {rosbridge_uri}")

            # Run inbound & outbound in parallel
            tasks = []
            tasks.append(asyncio.create_task(inbound_loop(ws, shared_queues)))
            tasks.append(asyncio.create_task(outbound_loop(ws, shared_queues)))
            # Wait for them to complete
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

    if len(data_3d.shape) == 3:
        data_3d = np.mean(data_3d, axis=-1).astype(np.uint8)

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
    await ws.send(json.dumps(outbound, default=np_encoder))


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
