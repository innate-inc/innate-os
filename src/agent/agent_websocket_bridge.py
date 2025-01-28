#!/usr/bin/env python3

import asyncio
import base64
import json
import queue
import threading
import time

import numpy as np
import websockets

from src.agent.types import (
    OccupancyGridMsg,
    RobotStateMsg,
    VelocityCmd,
)


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
# Main rosbridge loop
#
async def rosbridge_loop(shared_queues, rosbridge_uri: str):
    print(f"[ROSBridge] Connecting to {rosbridge_uri} ...")
    try:
        async with websockets.connect(rosbridge_uri) as ws:
            print(f"[ROSBridge] Connected to {rosbridge_uri}")

            # 1) Advertise topics
            adv_color = rosbridge_advertise(
                "/camera/color/image_raw", "sensor_msgs/msg/Image"
            )
            adv_depth = rosbridge_advertise(
                "/camera/depth/image_raw", "sensor_msgs/msg/Image"
            )
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
                "[ROSBridge] Advertised /camera/color/image_raw, /camera/depth/image_raw, /camera/color/camera_info, /odom, /map"
            )

            # 2) Subscribe to /cmd_vel
            sub_cmd_vel = rosbridge_subscribe("/cmd_vel", "geometry_msgs/msg/Twist")
            await ws.send(json.dumps(sub_cmd_vel))
            print("[ROSBridge] Subscribed to /cmd_vel")

            # 3) Main loop: forward sim->agent data, handle inbound
            while not shared_queues.exit_event.is_set():
                # a) Check if there's a message from the sim
                try:
                    msg = shared_queues.sim_to_agent.get_nowait()

                    if isinstance(msg, RobotStateMsg):
                        await publish_robot_state(ws, msg)

                    elif isinstance(msg, OccupancyGridMsg):
                        await publish_occupancy_grid(ws, msg)

                    else:
                        # Unknown or unhandled message
                        pass

                except queue.Empty:
                    pass

                # b) Check inbound from rosbridge (cmd_vel, etc.)
                try:
                    inbound_raw = await asyncio.wait_for(ws.recv(), timeout=0.01)
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.01)
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print("[ROSBridge] Connection closed.")
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

                # tiny sleep
                await asyncio.sleep(0.001)

    except Exception as e:
        print(f"[ROSBridge] Connection error: {e}")

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

    # 1) Color image
    if rsm.rgb_frame is not None:
        color_data = rsm.rgb_frame.tobytes()  # BGR or RGB?
        color_msg = {
            "header": {
                "stamp": {"sec": sec, "nanosec": nsec},
                "frame_id": rsm.frame_id,
            },
            "height": rsm.rgb_frame.shape[0],
            "width": rsm.rgb_frame.shape[1],
            "encoding": "bgr8",  # or "rgb8" if your data is in that format
            "is_bigendian": 0,
            "step": rsm.rgb_frame.shape[1] * 3,
            "data": base64.b64encode(color_data).decode("utf-8"),
        }
        outbound = rosbridge_publish("/camera/color/image_raw", color_msg)
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
    await ws.send(json.dumps(outbound))

    # 4) Odometry => /odom (nav_msgs/Odometry)
    odom_msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": "odom",  # or "map", depending on your TF tree
        },
        "child_frame_id": "base_link",  # typical
        "pose": {
            "pose": {
                "position": {
                    "x": rsm.px,
                    "y": rsm.py,
                    "z": rsm.pz,
                },
                "orientation": {
                    "x": rsm.ox,
                    "y": rsm.oy,
                    "z": rsm.oz,
                    "w": rsm.ow,
                },
            },
            "covariance": [0.0] * 36,
        },
        "twist": {
            "twist": {
                "linear": {
                    "x": rsm.vx,
                    "y": rsm.vy,
                    "z": rsm.vz,
                },
                "angular": {
                    "x": rsm.wx,
                    "y": rsm.wy,
                    "z": rsm.wz,
                },
            },
            "covariance": [0.0] * 36,
        },
    }
    outbound = rosbridge_publish("/odom", odom_msg)
    await ws.send(json.dumps(outbound))


#
# Publish OccupancyGridMsg => /map (nav_msgs/OccupancyGrid)
#
async def publish_occupancy_grid(ws, og: OccupancyGridMsg):
    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    # Flatten data if 2D
    if len(og.data.shape) == 2:
        flat_data = og.data.flatten()
    else:
        flat_data = og.data

    # Convert to Python list for JSON
    grid_list = flat_data.tolist()

    # Convert yaw -> quaternion for origin
    import math

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
        "data": grid_list,
    }

    outbound = rosbridge_publish("/map", map_msg)
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
