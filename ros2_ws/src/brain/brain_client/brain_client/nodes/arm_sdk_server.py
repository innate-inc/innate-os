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

The controller app's phone teleop rides the same node: /armsdk/stream_pose
carries a 6-DoF end-effector delta (JSON) from where the operator's hand was
when they pressed the button, in the arm's heading at that moment: the phone's
forward is wherever the arm pointed, not base_link +x. The follower answers
on /armsdk/stream_pose/status. The arm follows from its pose at the first
sample applied, after moving to zero unless it is still where the previous
stream left it: from the rest fold, IK refuses almost every target.

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
from collections.abc import Sequence
from dataclasses import dataclass

import rclpy
from brain_messages.action import ExecuteArmCommand
from rclpy.action import ActionServer
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import Float64MultiArray, String

from brain_client.common.enums import StrEnum
from brain_client.common.geometry import Quat, apply_pose_delta, rebase_anchor, unyaw_delta
from brain_client.robot.exceptions import ArmFailed, ArmUnhealthy
from brain_client.robot.manipulation import Manipulation

IDLE_PARK_S = 60.0  # park the state feeds this long after the last command
STATE_WAKE_S = 1.5  # how long a waking command waits for the first /mars/arm/state

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
    """Mark activity and wake the state feeds. Waking waits for the first
    joint state: commands read the measured arm, and would otherwise refuse
    the very command that woke the feeds."""
    global last_request
    last_request = time.monotonic()
    if manip._executor is not None:
        return
    node.get_logger().info("command while parked — starting arm-state feeds")
    manip.start()
    deadline = time.monotonic() + STATE_WAKE_S
    while manip._arm_state is None and time.monotonic() < deadline:
        time.sleep(0.02)


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
    # A pose-stream step holds the lock for one IK solve; wait that out so a
    # gripper tap mid-teleop lands instead of bouncing off as busy.
    if needs_lock and not motion_lock.acquire(timeout=Manipulation.STREAM_IK_TIMEOUT_S * 2):
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


@dataclass(frozen=True)
class PoseSample:
    """One /armsdk/stream_pose message: the operator's hand motion since
    ``session`` began, in the arm's heading frame, and an optional gripper opening
    (0 closed … 1 open). A new session re-anchors on the live pose."""

    session: str
    position: tuple[float, float, float]
    rotation: Quat
    grip: float | None


@dataclass(frozen=True)
class Anchor:
    """The pose a session's deltas apply to, rebased so its first applied
    sample lands on the arm where it stood. ``heading`` is j1 at anchoring,
    fixed for the session: following the live j1 would turn the frame as the
    arm swings and bend a straight push into a curve."""

    session: str
    heading: float
    position: tuple[float, float, float]
    rotation: Quat

    def target(self, sample: PoseSample) -> tuple[float, float, float, float, float, float]:
        return apply_pose_delta(
            self.position, self.rotation, *unyaw_delta(sample.position, sample.rotation, self.heading)
        )


class FollowState(StrEnum):
    IDLE = "idle"
    ZEROING = "zeroing"
    FOLLOWING = "following"
    UNREACHABLE = "unreachable"
    REFUSED = "refused"


class PoseFollower:
    """Latest-wins follower for /armsdk/stream_pose: the subscription only keeps
    the newest sample and a tick thread solves it, so messages never queue
    behind a slow solve. Each new session zeroes the arm unless it is still
    where the last session left it, then anchors on the live FK pose."""

    STATUS_HZ = 5.0
    ZERO_S = 3.0
    # j1-j4 this far from a pose: the arm is elsewhere, not settling after the
    # operator let go or finishing a zero.
    MOVED_RAD = 0.25

    def __init__(self):
        self._lock = threading.Lock()
        self._sample: PoseSample | None = None
        self._stamp = 0.0
        self._anchor: Anchor | None = None
        self._left_at: list[float] | None = None  # j1-j4 as the last session had them
        # The stream being answered: a refused one never gets an anchor.
        self._session: str | None = None
        self._expired: str | None = None
        self._state = FollowState.IDLE
        self._detail = ""
        self._status_pub = node.create_publisher(String, "/armsdk/stream_pose/status", 10)
        self._last_status = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def on_message(self, msg: String) -> None:
        try:
            body = json.loads(msg.data)
            sample = PoseSample(
                session=str(body["session"]),
                position=(float(body["x"]), float(body["y"]), float(body["z"])),
                rotation=(float(body["qx"]), float(body["qy"]), float(body["qz"]), float(body["qw"])),
                grip=None if body.get("grip") is None else min(1.0, max(0.0, float(body["grip"]))),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            node.get_logger().warning(f"bad stream_pose message: {e}", throttle_duration_sec=2.0)
            return
        with self._lock:
            self._sample = sample
            self._stamp = time.monotonic()

    def _run(self) -> None:
        dt = 1.0 / Manipulation.STREAM_RATE_HZ
        while rclpy.ok():
            time.sleep(dt)
            with self._lock:
                sample, fresh = self._sample, time.monotonic() - self._stamp < Manipulation.STREAM_IDLE_S
            if sample is None or not fresh or sample.session == self._expired:
                # A stream that went quiet stays dropped; the operator presses again.
                if sample is not None:
                    self._expired = sample.session
                self._session = None
                self._set(FollowState.IDLE)
                self._anchor = None
                continue
            self._session = sample.session
            touch()  # here, not in the subscription callback: waking the feeds can block
            self._step(sample)
            self._publish_status()

    def _step(self, sample: PoseSample) -> None:
        if not motion_lock.acquire(blocking=False):
            return  # a discrete motion owns the arm; the next tick retries
        try:
            if manip.torque_enabled is False:
                self._anchor = None  # the arm falls; with torque back the session starts over
                raise ArmFailed("arm torque is disabled")
            anchor = self._anchor
            if anchor is None or anchor.session != sample.session:
                if self._arm_away_from(self._left_at):
                    self._zero()
                    return  # the hand moved during the zero: anchor on the next, fresh sample
                anchor = self._anchor_on(sample)
            target = anchor.target(sample)
            grip = None if sample.grip is None else sample.grip * Manipulation.GRIPPER_OPEN
            reached = manip.stream_pose(*target, grip=grip)
            self._note_arm()
            self._set(FollowState.FOLLOWING if reached else FollowState.UNREACHABLE)
        except (ArmFailed, ArmUnhealthy) as e:
            self._set(FollowState.REFUSED, str(e))
        finally:
            motion_lock.release()

    def _arm_away_from(self, joints: Sequence[float] | None) -> bool:
        state = manip._arm_state
        if state is None or joints is None:
            return True
        return any(abs(q - j) > self.MOVED_RAD for q, j in zip(state.position[:4], joints[:4], strict=True))

    def _note_arm(self) -> None:
        state = manip._arm_state
        if state is not None:
            self._left_at = [float(q) for q in state.position[:4]]

    def _zero(self) -> None:
        node.get_logger().info(f"pose stream {self._session}: the arm is not where the last stream left it — zeroing")
        self._set(FollowState.ZEROING)
        manip.move_joints(Manipulation.ZERO[:5], duration=self.ZERO_S)
        if self._arm_away_from(Manipulation.ZERO):
            raise ArmFailed("the arm did not reach zero")  # a limp arm "completes" a goto without moving
        self._note_arm()

    def _anchor_on(self, sample: PoseSample) -> Anchor:
        state = manip._arm_state
        if state is None:
            raise ArmFailed("no arm joint state yet")
        heading = float(state.position[0])
        pose = manip.pose
        delta = unyaw_delta(sample.position, sample.rotation, heading)
        anchor = Anchor(sample.session, heading, *rebase_anchor(pose.position, pose.orientation, *delta))
        self._anchor = anchor
        node.get_logger().info(
            f"pose stream {sample.session}: anchored at ({pose.x:.3f}, {pose.y:.3f}, {pose.z:.3f}), heading {heading:.2f} rad"
        )
        return anchor

    def _set(self, state: FollowState, detail: str = "") -> None:
        changed = state != self._state or detail != self._detail
        self._state, self._detail = state, detail
        if changed:
            self._publish_status(force=True)

    def _publish_status(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_status < 1.0 / self.STATUS_HZ:
            return
        self._last_status = now
        body = {"session": self._session, "state": self._state.value, "detail": self._detail}
        self._status_pub.publish(String(data=json.dumps(body)))


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
    follower = PoseFollower()
    node.create_subscription(
        String,
        "/armsdk/stream_pose",
        follower.on_message,
        QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT),
        callback_group=group,
    )
    threading.Thread(target=parker, daemon=True).start()
    node.get_logger().info(
        "Arm SDK server: action /armsdk/command, streams /armsdk/stream_joints and /armsdk/stream_pose"
    )
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
