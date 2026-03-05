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
    ArmCmd,
    ArmGotoCmd,
    ArmStateMsg,
    DirectiveCmd,
    ResetRobotCmd,
    BrainActiveCmd,
    NavigationPathMsg,
    NavigationWaypoint,
    NavigationCancelMsg,
    NavigationStatusMsg,
    NavigationFeedbackMsg,
)
from src.shared_queues import ChatMessage, ChatSignal, AgentInfo
from src.agent.navigation_controller import NavigationController


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


def rosbridge_advertise_service(service: str, srv_type: str) -> dict:
    return {"op": "advertise_service", "service": service, "type": srv_type}


def rosbridge_service_response(service: str, result: dict, call_id: str = None) -> dict:
    resp = {
        "op": "service_response",
        "service": service,
        "values": result,
        "result": True,
    }
    if call_id:
        resp["id"] = call_id
    return resp


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

            # 1b) /mars/arm/commands
            elif topic == "/mars/arm/commands":
                arm_cmd = parse_arm_command(msg_data)
                if arm_cmd:
                    print(
                        f"[ROSBridge] Received arm command: {arm_cmd.joint_positions}"
                    )
                    try:
                        shared_queues.agent_to_sim.put_nowait(arm_cmd)
                    except queue.Full:
                        pass

            # 2) /chat_out
            elif topic == "/brain/chat_out":
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

            # 3) /sim_navigation/global_plan
            elif topic == "/sim_navigation/global_plan":
                frame_id = msg_data.get("header", {}).get("frame_id", "map")
                poses = msg_data.get("poses", [])

                if not poses:
                    print("[ROSBridge] Received empty navigation path")
                    continue

                # Convert poses to waypoints and recalculate yaw for forward movement
                waypoints = []
                positions = []

                # First pass: extract positions
                for pose_stamped in poses:
                    pose = pose_stamped.get("pose", {})
                    position = pose.get("position", {})
                    x = float(position.get("x", 0.0))
                    y = float(position.get("y", 0.0))
                    positions.append((x, y))

                # Get original final orientation from Nav2 (this is the target orientation)
                final_pose = poses[-1].get("pose", {})
                final_orientation = final_pose.get("orientation", {})
                final_qz = float(final_orientation.get("z", 0.0))
                final_qw = float(final_orientation.get("w", 1.0))
                target_final_yaw = 2 * math.atan2(final_qz, final_qw)

                print(
                    f"[ROSBridge] Target final orientation: {math.degrees(target_final_yaw):.1f}°"
                )

                # Second pass: calculate yaw based on direction of movement
                for i, (x, y) in enumerate(positions):
                    if i < len(positions) - 1:
                        # For all waypoints except the last: face towards next waypoint
                        next_x, next_y = positions[i + 1]
                        dx = next_x - x
                        dy = next_y - y
                        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                            # Handle case where consecutive waypoints are very close
                            if i > 0:
                                # Use previous segment direction
                                prev_x, prev_y = positions[i - 1]
                                dx = x - prev_x
                                dy = y - prev_y
                            else:
                                dx, dy = 1.0, 0.0  # Default to facing east
                        yaw = math.atan2(dy, dx)  # Direction to next waypoint
                    else:
                        # For the final waypoint: use the original Nav2 target orientation
                        yaw = target_final_yaw

                    waypoint = NavigationWaypoint(x=x, y=y, yaw=yaw)
                    waypoints.append(waypoint)

                    waypoint_type = (
                        "FINAL" if i == len(positions) - 1 else "intermediate"
                    )
                    print(
                        f"Waypoint {i} ({waypoint_type}): pos=({x:.2f}, {y:.2f}), yaw={math.degrees(yaw):.1f}°"
                    )

                nav_path = NavigationPathMsg(frame_id=frame_id, waypoints=waypoints)

                print(
                    f"[ROSBridge] Received navigation path with {len(waypoints)} waypoints"
                )

                # Forward to navigation controller
                if hasattr(shared_queues, "nav_controller"):
                    shared_queues.nav_controller.set_navigation_path(nav_path)

            # 4) /sim_navigation/cancel
            elif topic == "/sim_navigation/cancel":
                cancel_data = msg_data.get("data", False)
                if cancel_data:
                    nav_cancel = NavigationCancelMsg(cancel=True)
                    # Forward to navigation controller
                    if hasattr(shared_queues, "nav_controller"):
                        shared_queues.nav_controller.cancel_navigation()

        # Process incoming service calls (services we provide)
        elif op_type == "call_service":
            service_name = inbound_data.get("service", "")
            call_id = inbound_data.get("id", None)
            args = inbound_data.get("args", {})

            # Handle /mars/arm/goto_js service
            if service_name == "/mars/arm/goto_js":
                print(f"[ROSBridge] Received goto_js service call: {args}")
                data = args.get("data", {})
                joint_data = data.get("data", [])
                duration = args.get("time", 1)

                if len(joint_data) >= 6:
                    joint_positions = [float(d) for d in joint_data[:6]]
                    arm_goto_cmd = ArmGotoCmd(
                        joint_positions=joint_positions,
                        duration=float(duration),
                        service_id=call_id,
                    )
                    try:
                        shared_queues.agent_to_sim.put_nowait(arm_goto_cmd)
                    except queue.Full:
                        pass

                    # Send immediate success response (motion started)
                    response = rosbridge_service_response(
                        service_name, {"success": True}, call_id
                    )
                    await ws.send(json.dumps(response))

        # Process service responses (responses to service calls that we initiated)
        elif op_type == "service_response":
            service_name = inbound_data.get("service", "")
            if service_name == "/brain/get_chat_history":
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
                        try:
                            shared_queues.chat_from_bridge.put_nowait(chat_msg)
                        except queue.Full:
                            print(
                                "[ROSBridge] chat_from_bridge queue is full; dropping chat message."
                            )
                    except Exception as e:
                        print(f"[ROSBridge] Error processing chat history entry: {e}")

            elif service_name == "/brain/get_available_directives":
                # Check if the service call was successful
                if not inbound_data.get("result", False):
                    print(
                        "[ROSBridge] get_available_directives service call failed or brain not ready"
                    )
                    continue

                values = inbound_data.get("values", {})
                directives_raw = values.get("directives", "[]")
                current_directive = values.get("current_directive", "")
                startup_directive = values.get("startup_directive", "")

                # The service returns a list with one element: a JSON string containing all agents
                # e.g., ['[{"id": "agent1", ...}, {"id": "agent2", ...}]']
                agents = []

                try:
                    # Get the JSON string (first element of the list)
                    if isinstance(directives_raw, list) and len(directives_raw) > 0:
                        json_string = directives_raw[0]
                    elif isinstance(directives_raw, str):
                        json_string = directives_raw
                    else:
                        json_string = "[]"

                    # Parse the JSON string to get list of agent dicts
                    agents_list = json.loads(json_string)

                    # Convert each dict to AgentInfo
                    for directive in agents_list:
                        if isinstance(directive, dict):
                            agent = AgentInfo(
                                id=directive.get("id", ""),
                                display_name=directive.get(
                                    "display_name", directive.get("id", "")
                                ),
                                display_icon=directive.get("display_icon"),
                                prompt=directive.get("prompt", ""),
                                skills=directive.get("skills", []),
                            )
                            agents.append(agent)
                            print(
                                f"[ROSBridge] Parsed agent: {agent.id} - {agent.display_name}"
                            )
                except json.JSONDecodeError as e:
                    print(f"[ROSBridge] Failed to parse directives JSON: {e}")
                except Exception as e:
                    print(f"[ROSBridge] Error processing directives: {e}")

                # Update shared_queues with available agents
                shared_queues.update_available_agents(
                    agents=agents,
                    current_agent_id=current_directive,
                    startup_agent_id=startup_directive,
                )
                print(f"[ROSBridge] Loaded {len(agents)} available agents from brain")

        await asyncio.sleep(0.0001)

    print("[ROSBridge] inbound_loop stopped.")


async def outbound_loop(ws, shared_queues):
    """
    Continuously process messages from sim_to_agent and publish them
    to rosbridge (camera images, odometry, map, etc.).
    Also advertise relevant topics/services once at start.

    IMPORTANT: Chat messages are processed FIRST to avoid latency from heavy sensor data.
    """
    print("[ROSBridge] outbound_loop started.")

    # Queue monitoring - log periodically if queues are backing up
    last_queue_log_time = time.time()
    QUEUE_LOG_INTERVAL = 5.0  # Log every 5 seconds if there's a problem

    # First, advertise standard topics once
    adv_color = rosbridge_advertise(
        "/mars/main_camera/left/image_raw/compressed", "sensor_msgs/msg/CompressedImage"
    )
    adv_arm_camera = rosbridge_advertise(
        "/mars/arm/image_raw/compressed", "sensor_msgs/msg/CompressedImage"
    )
    adv_depth = rosbridge_advertise("/camera/depth/image_raw", "sensor_msgs/msg/Image")
    adv_cinfo = rosbridge_advertise(
        "/camera/color/camera_info", "sensor_msgs/msg/CameraInfo"
    )
    adv_odom = rosbridge_advertise("/odom", "nav_msgs/msg/Odometry")
    adv_map = rosbridge_advertise("/map", "nav_msgs/msg/OccupancyGrid")
    adv_chat_in = rosbridge_advertise("/brain/chat_in", "std_msgs/msg/String")
    adv_set_directive = rosbridge_advertise(
        "/brain/set_directive", "std_msgs/msg/String"
    )
    # Add a new topic for logging configuration
    adv_logging_config = rosbridge_advertise("/logging_config", "std_msgs/msg/Bool")
    adv_clock = rosbridge_advertise("/clock", "rosgraph_msgs/Clock")

    # Navigation topics
    adv_nav_status = rosbridge_advertise(
        "/sim_navigation/status", "std_msgs/msg/String"
    )
    adv_nav_feedback = rosbridge_advertise(
        "/sim_navigation/feedback", "geometry_msgs/msg/Point"
    )
    adv_nav_mode = rosbridge_advertise("/nav/current_mode", "std_msgs/msg/String")

    # Arm topics and services
    adv_arm_state = rosbridge_advertise("/mars/arm/state", "sensor_msgs/msg/JointState")
    adv_arm_goto_service = rosbridge_advertise_service(
        "/mars/arm/goto_js", "maurice_msgs/srv/GotoJS"
    )

    await ws.send(json.dumps(adv_color))
    await ws.send(json.dumps(adv_depth))
    await ws.send(json.dumps(adv_cinfo))
    await ws.send(json.dumps(adv_odom))
    await ws.send(json.dumps(adv_map))
    await ws.send(json.dumps(adv_chat_in))
    await ws.send(json.dumps(adv_set_directive))
    await ws.send(json.dumps(adv_logging_config))
    await ws.send(json.dumps(adv_clock))
    await ws.send(json.dumps(adv_nav_status))
    await ws.send(json.dumps(adv_nav_feedback))
    await ws.send(json.dumps(adv_nav_mode))
    await ws.send(json.dumps(adv_arm_state))
    await ws.send(json.dumps(adv_arm_camera))
    # await ws.send(json.dumps(adv_arm_goto_service))
    print(
        "[ROSBridge] Advertised camera-related topics, /odom, /map, /chat_in, /logging_config, navigation topics, and arm interfaces"
    )

    # Publish initial navigation mode (simulator always uses mapfree)
    nav_mode_msg = {"data": "mapfree"}
    outbound = rosbridge_publish("/nav/current_mode", nav_mode_msg)
    await ws.send(json.dumps(outbound))
    print("[ROSBridge] Published initial navigation mode: mapfree")

    # Also subscribe to /cmd_vel, /chat_out, and navigation topics
    sub_cmd_vel = rosbridge_subscribe("/cmd_vel", "geometry_msgs/msg/Twist")
    sub_chat_out = rosbridge_subscribe("/brain/chat_out", "std_msgs/msg/String")
    sub_nav_path = rosbridge_subscribe(
        "/sim_navigation/global_plan", "nav_msgs/msg/Path"
    )
    sub_nav_cancel = rosbridge_subscribe("/sim_navigation/cancel", "std_msgs/msg/Bool")
    sub_arm_cmd = rosbridge_subscribe(
        "/mars/arm/commands", "std_msgs/msg/Float64MultiArray"
    )
    await ws.send(json.dumps(sub_cmd_vel))
    await ws.send(json.dumps(sub_chat_out))
    await ws.send(json.dumps(sub_nav_path))
    await ws.send(json.dumps(sub_nav_cancel))
    await ws.send(json.dumps(sub_arm_cmd))
    print(
        "[ROSBridge] Subscribed to /cmd_vel, /brain/chat_out, /mars/arm/commands, and navigation topics"
    )

    # Initialize navigation controller
    nav_controller = NavigationController(shared_queues)
    shared_queues.nav_controller = nav_controller  # Store reference for inbound_loop
    print("[ROSBridge] Navigation controller initialized")

    # Send the logging configuration immediately after connection
    if hasattr(shared_queues, "log_everything"):
        # Method 1: Publish to a topic
        logging_msg = {"data": shared_queues.log_everything}
        outbound = rosbridge_publish("/logging_config", logging_msg)
        await ws.send(json.dumps(outbound))

        # Method 2: Call a service (more reliable for initialization)
        srv_set_logging = rosbridge_call_service(
            "/brain/set_logging_config", "std_srvs/srv/SetBool"
        )
        # Add the data parameter for the service call
        srv_set_logging["args"] = {"data": shared_queues.log_everything}
        await ws.send(json.dumps(srv_set_logging))

        print(
            f"[ROSBridge] Set logging configuration: log_everything={shared_queues.log_everything}"
        )

    # Request available agents/directives from the brain
    srv_get_agents = rosbridge_call_service(
        "/brain/get_available_directives", "brain_messages/srv/GetAvailableDirectives"
    )
    await ws.send(json.dumps(srv_get_agents))
    print("[ROSBridge] Requested available agents from brain")

    while not shared_queues.exit_event.is_set():
        # Monitor queue sizes periodically
        now = time.time()
        if now - last_queue_log_time > QUEUE_LOG_INTERVAL:
            chat_qsize = shared_queues.chat_to_bridge.qsize()
            sim_qsize = shared_queues.sim_to_agent.qsize()
            if chat_qsize > 5 or sim_qsize > 50:
                print(
                    f"[ROSBridge] Queue status: chat={chat_qsize}, sim_to_agent={sim_qsize}"
                )
            last_queue_log_time = now

        # PRIORITY 1: Process ALL pending chat messages first (low latency path)
        # This ensures chat doesn't wait behind heavy sensor data
        chat_processed = 0
        while chat_processed < 10:  # Process up to 10 chat messages per iteration
            try:
                msg = shared_queues.chat_to_bridge.get_nowait()

                if isinstance(msg, ChatMessage):
                    latency = (
                        time.time() - msg.timestamp_put_in_queue
                        if hasattr(msg, "timestamp_put_in_queue")
                        else 0
                    )
                    if latency > 0.5:
                        print(f"[ROSBridge] Chat message latency: {latency:.2f}s")
                    print(f"[ROSBridge] Publishing chat message: {msg.text}")
                    chat_payload = json.dumps(
                        {
                            "text": msg.text,
                            "sender": msg.sender,
                            "timestamp": msg.timestamp,
                        }
                    )
                    outbound_text = {"data": chat_payload}
                    outbound = rosbridge_publish("/brain/chat_in", outbound_text)
                    await ws.send(json.dumps(outbound))

                    # Echo back to UI
                    try:
                        shared_queues.chat_from_bridge.put_nowait(msg)
                    except queue.Full:
                        pass

                elif isinstance(msg, ChatSignal):
                    print(f"[ROSBridge] Publishing chat signal: {msg.signal}")
                    if msg.signal == "ready":
                        srv_chat_history = rosbridge_call_service(
                            "/brain/get_chat_history",
                            "brain_messages/srv/GetChatHistory",
                        )
                        await ws.send(json.dumps(srv_chat_history))

                chat_processed += 1
            except queue.Empty:
                break

        # PRIORITY 2: Process sensor data from dedicated size-1 queue (camera frames)
        # This queue only ever has 0 or 1 item - old frames are dropped at the source
        try:
            sensor_msg = shared_queues.sensor_to_agent.get_nowait()
            if isinstance(sensor_msg, RobotStateMsg):
                await publish_robot_state(ws, sensor_msg, shared_queues)
        except queue.Empty:
            pass

        # PRIORITY 3: Process sim_to_agent messages (commands, map, arm state, etc.)
        try:
            msg = shared_queues.sim_to_agent.get_nowait()

            if isinstance(msg, OccupancyGridMsg):
                await publish_occupancy_grid(ws, msg, shared_queues)
            elif isinstance(msg, ArmStateMsg):
                await publish_arm_state(ws, msg)
            elif isinstance(msg, DirectiveCmd):
                directive_msg = {"data": msg.directive}
                outbound = rosbridge_publish("/brain/set_directive", directive_msg)
                await ws.send(json.dumps(outbound))
                print(f"[ROSBridge] Published directive: {msg.directive}")
            elif isinstance(msg, ResetRobotCmd):
                reset_srv = rosbridge_call_service(
                    "/brain/reset_brain", "brain_messages/srv/ResetBrain"
                )

                # Set the memory_state parameter directly (empty string if none provided)
                # Using dict for clarity when working with the service call params
                memory_state = ""
                if msg.memory_state:
                    memory_state = msg.memory_state

                # This is the correct way to format args for the service call in rosbridge
                reset_srv["args"] = {"memory_state": memory_state}

                if memory_state:
                    print(f"[ROSBridge] Reset with memory state: {memory_state}")
                else:
                    print("[ROSBridge] Reset without memory state")

                await ws.send(json.dumps(reset_srv))

            elif isinstance(msg, BrainActiveCmd):
                brain_active_srv = rosbridge_call_service(
                    "/brain/set_brain_active", "std_srvs/srv/SetBool"
                )
                brain_active_srv["args"] = {"data": msg.active}

                print(f"[ROSBridge] Setting brain active: {msg.active}")
                await ws.send(json.dumps(brain_active_srv))

            elif isinstance(msg, dict) and "clock" in msg:
                outbound = rosbridge_publish("/clock", msg)
                await ws.send(json.dumps(outbound))

            elif isinstance(msg, NavigationStatusMsg):
                status_msg = {"data": msg.status}
                outbound = rosbridge_publish("/sim_navigation/status", status_msg)
                await ws.send(json.dumps(outbound))

            elif isinstance(msg, NavigationFeedbackMsg):
                feedback_msg = {
                    "x": msg.distance_to_goal,
                    "y": msg.unused_y,
                    "z": msg.unused_z,
                }
                outbound = rosbridge_publish("/sim_navigation/feedback", feedback_msg)
                await ws.send(json.dumps(outbound))

        except queue.Empty:
            # no messages to publish right now
            pass

        await asyncio.sleep(0.0001)

    print("[ROSBridge] outbound_loop stopped.")


async def rosbridge_loop(
    shared_queues, rosbridge_uri: str, retry_interval: float = 2.0
):
    """
    High-level function that connects to rosbridge and starts
    two concurrent tasks: inbound_loop & outbound_loop & chat_bridge_loop.
    Automatically retries connection if it drops.
    """
    while not shared_queues.exit_event.is_set():
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

        if shared_queues.exit_event.is_set():
            break

        print(f"[ROSBridge] Connection lost. Retrying in {retry_interval}s...")
        await asyncio.sleep(retry_interval)

    print("[ROSBridge] Stopped rosbridge_loop.")


#
# Publish RobotStateMsg =>
#   /camera/color/image_raw (sensor_msgs/Image)
#   /camera/depth/image_raw (sensor_msgs/Image)
#   /camera/color/camera_info (sensor_msgs/CameraInfo)
#   /odom (nav_msgs/Odometry)
#
async def publish_robot_state(ws, rsm: RobotStateMsg, shared_queues):
    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    # Update the shared robot position for direct access by other components
    shared_queues.update_robot_position(rsm.px, rsm.py, rsm.pz, now)

    # -- 1) COMPRESS COLOR IMAGE --
    if rsm.rgb_frame is not None:
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]  # 70% quality
        ret, encoded_img = cv2.imencode(".jpg", rsm.rgb_frame, encode_params)

        if ret:
            jpg_bytes = encoded_img.tobytes()

            # Build a sensor_msgs/CompressedImage message
            # RWS expects uint8[] as a JSON int array, not base64
            compressed_msg = {
                "header": {
                    "stamp": {"sec": sec, "nanosec": nsec},
                    "frame_id": rsm.frame_id,
                },
                "format": "jpeg",
                "data": list(jpg_bytes),
            }
            outbound = rosbridge_publish(
                "/mars/main_camera/left/image_raw/compressed", compressed_msg
            )
            await ws.send(json.dumps(outbound))

    # -- 1b) COMPRESS ARM WRIST CAMERA IMAGE --
    if rsm.arm_rgb_frame is not None:
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]  # 70% quality
        ret, encoded_img = cv2.imencode(".jpg", rsm.arm_rgb_frame, encode_params)

        if ret:
            jpg_bytes = encoded_img.tobytes()

            # Build a sensor_msgs/CompressedImage message
            # RWS expects uint8[] as a JSON int array, not base64
            compressed_msg = {
                "header": {
                    "stamp": {"sec": sec, "nanosec": nsec},
                    "frame_id": "arm_wrist_camera_frame",
                },
                "format": "jpeg",
                "data": list(jpg_bytes),
            }
            outbound = rosbridge_publish(
                "/mars/arm/image_raw/compressed", compressed_msg
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
    print("[ROSBridge] Published odometry")


async def publish_occupancy_grid(ws, og: OccupancyGridMsg, shared_queues):
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


async def publish_arm_state(ws, arm_state: ArmStateMsg):
    """Publish arm joint state to /mars/arm/state (sensor_msgs/JointState)."""
    now = time.time()
    sec = int(now)
    nsec = int((now - sec) * 1e9)

    joint_state_msg = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nsec},
            "frame_id": "base_link",
        },
        "name": arm_state.joint_names,
        "position": arm_state.joint_positions,
        "velocity": [0.0] * len(arm_state.joint_positions),
        "effort": [0.0] * len(arm_state.joint_positions),
    }

    outbound = rosbridge_publish("/mars/arm/state", joint_state_msg)
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


def parse_arm_command(msg: dict) -> ArmCmd | None:
    """Parse std_msgs/Float64MultiArray for arm joint commands."""
    try:
        data = msg.get("data", [])
        if len(data) >= 6:
            joint_positions = [float(d) for d in data[:6]]
            return ArmCmd(joint_positions=joint_positions)
        else:
            print(
                f"[ROSBridge] Arm command has {len(data)} values, need 6. Data: {data}"
            )
        return None
    except (TypeError, ValueError) as e:
        print(f"[ROSBridge] Failed to parse arm command: {e}, msg={msg}")
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
