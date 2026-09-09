#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Arm SDK server — the backend behind the webapp's /armsdk page.

Drives the brain_client Manipulation SDK against the live arm, exposed the
same way every other page-facing robot feature is: an action
(/armsdk/command, ExecuteArmCommand) plus a slider-stream topic
(/armsdk/stream_joints), driven from the browser over rosbridge. The page
reads pose/joints/torque straight from the driver topics, so this node costs
nothing while the page just looks at the arm.

The Manipulation feeds cost ~600 msg/s of callback dispatch while live, so
they only run around commands: constructed lazy, started on the first goal,
and parked again after IDLE_PARK_S without one.

Not skill code — plain time.sleep and blocking calls are fine here.
"""

import json
import math
import sys
import threading
import time
import traceback

import rclpy
from brain_messages.action import ExecuteArmCommand
from rclpy.action import ActionServer
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import Float64MultiArray

from brain_client.robot.exceptions import ArmFailed, ArmUnhealthy
from brain_client.robot.manipulation import Manipulation

IDLE_PARK_S = 60.0  # park the state feeds this long after the last command

rclpy.init(args=sys.argv)  # honor the launch file's --ros-args
node = rclpy.create_node("arm_sdk_server")
manip = Manipulation(node, node.get_logger(), lazy=True)
manip.safety.max_ee_speed = 0.20  # m/s — stretches cartesian move durations

# One motion at a time; a concurrent goal is refused instead of queueing up.
# The joint stream takes it too (briefly): during a blocking move a stream
# step is dropped, so sliders can't fight a discrete motion.
motion_lock = threading.Lock()

# Written on every command, read by the parker thread. Plain float — a torn
# read is impossible under the GIL and a stale one just delays the park a tick.
last_request = time.monotonic()


def touch():
    """Mark activity and make sure the state feeds are live (idempotent)."""
    global last_request
    last_request = time.monotonic()
    if manip._executor is None:  # log the resume, not every stream step
        node.get_logger().info("command while parked — starting arm-state feeds")
    manip.start()


def parker():
    """Park the Manipulation feeds once the page has gone quiet."""
    while rclpy.ok():
        time.sleep(5.0)
        if manip._executor is None:  # already parked
            continue
        if time.monotonic() - last_request < IDLE_PARK_S:
            continue
        # Skip while a motion holds the lock — stop() would clear its state
        # out from under it. The lock also serializes us against new commands.
        if motion_lock.acquire(blocking=False):
            try:
                if time.monotonic() - last_request >= IDLE_PARK_S:
                    node.get_logger().info("page idle — parking arm-state feeds")
                    # stop() resets safety (skill-lifecycle semantics); the
                    # page's speed cap must survive the park.
                    cap = manip.safety.max_ee_speed
                    manip.stop()
                    manip.safety.max_ee_speed = cap
            finally:
                motion_lock.release()


def arm_to_dict(arm):
    r, p, y = arm.rpy
    return {
        "x": arm.x,
        "y": arm.y,
        "z": arm.z,
        "roll": r,
        "pitch": p,
        "yaw": y,
        "gripper": arm.gripper,
    }


def state():
    msg = manip.last_fk_pose
    pose = arm_to_dict(manip._arm_from_fk(msg)) if msg is not None else None
    js = manip._arm_state
    joints = list(js.position)[:6] if js is not None else None
    return {
        "pose": pose,
        "joints": joints,
        "torque": manip.torque_enabled,
        "moving": manip.moving,
        "grip_target": manip._grip_target,
        "max_ee_speed": manip.safety.max_ee_speed,
    }


def tolerances(body):
    """Verified moves FK-check and auto-recover (servo reboot) on a miss;
    default off for a jog console — the UI shows the settled error instead."""
    if body.get("verify"):
        return {}  # move_to defaults: tolerance_xy=0.05, tolerance_z=0.10
    return {"tolerance_xy": None, "tolerance_z": None}


def do_command(cmd, body):
    if cmd == "move_to":
        x, y, z = float(body["x"]), float(body["y"]), float(body["z"])
        settled = manip.move_to(
            x,
            y,
            z,
            roll=float(body.get("roll", 0.0)),
            pitch=float(body.get("pitch", 0.0)),
            yaw=float(body.get("yaw", 0.0)),
            duration=float(body.get("duration", 1.5)),
            **tolerances(body),
        )
        return {
            "settled": arm_to_dict(settled),
            "target": {"x": x, "y": y, "z": z},
            "err_xy": math.hypot(settled.x - x, settled.y - y),
            "err_z": abs(settled.z - z),
        }

    if cmd == "move_by":
        # One pose sample feeds both the command and the reported target —
        # move_by would re-read the pose and could aim somewhere the reported
        # err_xy/err_z were never measured against.
        cur = manip.pose
        roll, pitch, yaw = cur.rpy
        x = cur.x + float(body.get("dx", 0.0))
        y = cur.y + float(body.get("dy", 0.0))
        z = cur.z + float(body.get("dz", 0.0))
        settled = manip.move_to(
            x,
            y,
            z,
            roll=roll + float(body.get("droll", 0.0)),
            pitch=pitch + float(body.get("dpitch", 0.0)),
            yaw=yaw + float(body.get("dyaw", 0.0)),
            duration=float(body.get("duration", 0.8)),
            **tolerances(body),
        )
        return {
            "settled": arm_to_dict(settled),
            "target": {"x": x, "y": y, "z": z},
            "err_xy": math.hypot(settled.x - x, settled.y - y),
            "err_z": abs(settled.z - z),
        }

    if cmd == "gripper_open":
        manip.gripper_open(float(body.get("percent", 100.0)))
        return {}
    if cmd == "gripper_close":
        manip.gripper_close(float(body.get("strength", 0.0)))
        return {}
    if cmd == "rest":
        manip.rest()
        return {}
    if cmd == "zero":
        manip.move_joints(manip.ZERO, duration=3.0)
        return {}
    if cmd == "torque_on":
        return {"ok": manip.torque_on()}
    if cmd == "torque_off":
        return {"ok": manip.torque_off()}
    if cmd == "recover":
        manip.recover()
        return {}
    if cmd == "speed":
        v = body.get("max_ee_speed")
        manip.safety.max_ee_speed = None if v in (None, "", 0) else float(v)
        return {"max_ee_speed": manip.safety.max_ee_speed}

    raise ArmFailed(f"unknown command {cmd!r}")


def result(success, message="", extra=None):
    out = dict(extra or {})
    out["state"] = state()
    return ExecuteArmCommand.Result(success=success, message=message, state=json.dumps(out))


def execute_goal(goal_handle):
    cmd = goal_handle.request.command
    body = json.loads(goal_handle.request.params or "{}")
    touch()

    # torque_off is the abort path: it must work WHILE a motion holds the
    # lock (the motion then fails fast on the de-energized arm).
    needs_lock = cmd != "torque_off"
    if needs_lock and not motion_lock.acquire(blocking=False):
        goal_handle.succeed()
        return result(False, "arm busy — wait for the current motion")
    try:
        extra = do_command(cmd, body)
        goal_handle.succeed()
        return result(True, extra=extra)
    except (ArmFailed, ArmUnhealthy) as e:
        goal_handle.succeed()
        return result(False, f"{type(e).__name__}: {e}")
    except Exception as e:
        traceback.print_exc()
        goal_handle.succeed()
        return ExecuteArmCommand.Result(success=False, message=f"{type(e).__name__}: {e}", state="{}")
    finally:
        if needs_lock:
            motion_lock.release()


def on_stream(msg):
    """Live sliders stream through the SDK's stream_joints() — the teleop
    pass-through with a velocity-clamped slew, smooth unlike goto calls.
    A step landing during a discrete motion is dropped, not queued."""
    touch()
    if motion_lock.acquire(blocking=False):
        try:
            manip.stream_joints(list(msg.data))
        except (ArmFailed, ArmUnhealthy) as e:
            # Raised into an executor task nobody joins — log it or slider
            # drags die silently (torque off, no measured state yet).
            node.get_logger().warning(f"stream step refused: {e}", throttle_duration_sec=2.0)
        finally:
            motion_lock.release()


def main():
    group = ReentrantCallbackGroup()  # torque_off must land during a blocking motion
    server = ActionServer(node, ExecuteArmCommand, "/armsdk/command", execute_goal, callback_group=group)
    node.create_subscription(
        Float64MultiArray,
        "/armsdk/stream_joints",
        on_stream,
        QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT),
        callback_group=group,
    )
    threading.Thread(target=parker, daemon=True).start()
    node.get_logger().info("Arm SDK server: action /armsdk/command, stream /armsdk/stream_joints")
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        server.destroy()
        manip.shutdown()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
