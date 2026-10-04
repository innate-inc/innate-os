# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The expression layer's ROS edge, inside the brain client node: one Animator ticked at 30 Hz.

Each tick's ActuatorPose goes to the head servo (when its integer degree changes), the arm's
streaming pass-through, and the base through a stance tracker on /odom. Whenever the arm stream
(re)starts, the animator enters from the measured pose, so expression never jumps from wherever
a skill or an operator left the body; the animator also rate-limits every joint. The body is yielded
whenever something else owns it: a running skill or a Nav2 goal masks everything, a foreign
/mars/arm/commands stream (teleop, the arm SDK page) the arm, a foreign head command the head,
the joystick and the gaze tracker the base. Outside a clip or speech the arm streams only while an
agent is running and idle breathing is on, so a robot nobody talks to keeps its arm wherever skills
and auto-rest put it.

Prompts arrive on topics, from the express skill and from the agent's emote tags, and become clips
down the fallback chain in ``sources``.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from action_msgs.msg import GoalStatus, GoalStatusArray
from geometry_msgs.msg import Twist, Vector3
from nav_msgs.msg import Odometry
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import JointState
from std_msgs.msg import Empty, Float64MultiArray, Int32, String

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.expressive.animator import Animator
from brain_client.expressive.basis import ActuatorPose
from brain_client.expressive.breathing import Breathing
from brain_client.expressive.motion import Clip
from brain_client.expressive_driver.sources import ClipMaker, ClipSource, Made
from brain_client.expressive_driver.stance import StanceTracker

if TYPE_CHECKING:
    from collections.abc import Callable

    from innate_llm import Provider
    from rclpy.node import Node
    from rclpy.publisher import Publisher

    from brain_client.core.state import BrainState
    from brain_client.expressive.animator import AnimatorState
    from brain_client.perception.pose import Pose

FPS = 30.0
STATE_EVERY = 6  # ticks: 5 Hz
ENTER_S = 1.5  # crossfade from the measured pose into the animation when the arm stream starts
LINGER_S = 0.8  # keep streaming past a clip's end: the animator crossfades back to idle meanwhile
ODOM_STALE_S = 0.5
FOREIGN_HOLD_S = 5.0  # a teleop operator pausing must not have the arm or head yanked back
JOYSTICK_HOLD_S = 2.0
SKILL_TAIL_S = 0.5  # a skill's last head/arm command can trail its terminal status
# The activation fold (robot/rest_pose.py) is a 3 s goto, and a stream preempts gotos.
ACTIVATION_HOLD_S = 3.5
GRIP_BLOCKED_RAD = 0.08  # measured claw this far above its commanded close: an object is in it
JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head")
TERMINAL = {"completed", "failed", "interrupted"}
NAV_STATUS_TOPIC = "/navigate_to_pose/_action/status"  # the app's map goals bypass the skill slot
NAV_LIVE = {GoalStatus.STATUS_ACCEPTED, GoalStatus.STATUS_EXECUTING, GoalStatus.STATUS_CANCELING}
_ACTION_STATUS_QOS = QoSProfile(
    depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL, reliability=QoSReliabilityPolicy.RELIABLE
)
EXPRESS_SKILL = "express"

PROMPT_TOPIC = "/brain/express/prompt"
PLAY_TOPIC = "/brain/express/play"
STOP_TOPIC = "/brain/express/stop"
STATE_TOPIC = "/brain/express/state"
GENERATE_REQ_TOPIC = "/brain/express/generate_req"
GENERATE_RES_TOPIC = "/brain/express/generate_res"
STILL = Breathing(rise_amplitude=0.0, approach_amplitude=0.0, attend_amplitudes=(0.0, 0.0), grip_amplitude=0.0)


@dataclass(frozen=True)
class ExpressiveConfig:
    idle_breathing: bool
    server_url: str
    on_skill_completed: str  # prompt played when a skill completes; "" = none
    on_skill_failed: str

    @classmethod
    def load(cls, node: Node) -> ExpressiveConfig | None:
        """None when ``expressive.enabled`` is false: then nothing of the layer exists."""
        enabled = node.declare_parameter("expressive.enabled", True).value
        config = cls(
            idle_breathing=bool(node.declare_parameter("expressive.idle_breathing", True).value),
            server_url=str(node.declare_parameter("expressive.server_url", "http://innate52.local:8000").value),
            on_skill_completed=str(node.declare_parameter("expressive.on_skill_completed", "pleased, small nod").value),
            on_skill_failed=str(node.declare_parameter("expressive.on_skill_failed", "deflated").value),
        )
        return config if enabled else None


@dataclass(frozen=True)
class _Label:
    """What the state topic adds about a clip once it is on stage."""

    name: str
    source: ClipSource
    request_id: str | None


class ExpressionDriver:
    def __init__(
        self,
        node: Node,
        state: BrainState,
        config: ExpressiveConfig,
        *,
        cmd_vel_pub: Publisher,
        provider: Callable[[], Provider | None],
        standing_grip: Callable[[], float | None],
    ) -> None:
        """``standing_grip`` is the arm's last commanded j6 (hardware only); ``provider`` the brain's LLM."""
        self._logger = node.get_logger()
        self._state = state
        self._config = config
        self._cmd_vel_pub = cmd_vel_pub
        self._standing_grip = standing_grip
        self._maker = ClipMaker(config.server_url, provider, self._logger)
        self._animator = Animator(fps=FPS, idle=Breathing() if config.idle_breathing else STILL)
        self._stance = StanceTracker()
        self._lock = threading.Lock()  # the request bookkeeping below; hooks arrive on many threads

        self._seq = 0
        self._label: _Label | None = None
        self._pcm_carry = b""

        self._odom: Pose | None = None
        self._odom_at = 0.0
        self._joints: list[float] | None = None  # measured j1..j6 rad, head rad
        self._running_skills: set[str] = set()
        self._navigating = False
        self._was_masked = False
        self._unmasked_at = 0.0
        self._gaze = False
        self._streaming = False
        self._grip_lock: float | None = None
        self._pose: ActuatorPose | None = None
        self._head_cmd: int | None = None
        self._head_quiet_until = 0.0
        self._ours: deque[tuple[float, ...]] = deque(maxlen=64)  # recent arm commands: our own echoes
        self._ours_head: deque[int] = deque(maxlen=8)
        self._arm_quiet_until = 0.0
        self._base_quiet_until = 0.0
        self._live_until = 0.0
        self._was_active = state.is_brain_active
        self._last_tick = 0.0
        self._ticks = 0
        self._last_error_at = -1e9

        self._head_pub = node.create_publisher(Int32, "/mars/head/set_position", 10)
        self._arm_pub = node.create_publisher(Float64MultiArray, "/mars/arm/commands", 10)
        self._state_pub = node.create_publisher(String, STATE_TOPIC, 10)
        self._generated_pub = node.create_publisher(String, GENERATE_RES_TOPIC, 10)
        node.create_subscription(String, PROMPT_TOPIC, self._on_prompt, 10)
        node.create_subscription(String, PLAY_TOPIC, self._on_play, 10)
        node.create_subscription(Empty, STOP_TOPIC, lambda _msg: self.stop(), 10)
        node.create_subscription(String, GENERATE_REQ_TOPIC, self._on_generate_request, 10)
        node.create_subscription(String, "/brain/skill_status_update", self._on_skill_status, 10)
        node.create_subscription(Odometry, "/odom", self._on_odom, 10)
        node.create_subscription(JointState, "/joint_states", self._on_joint_states, qos_profile_sensor_data)
        node.create_subscription(Float64MultiArray, "/mars/arm/commands", self._on_arm_command, qos_profile_sensor_data)
        node.create_subscription(Int32, "/mars/head/set_position", self._on_head_command, 10)
        node.create_subscription(Vector3, "/joystick", self._on_joystick, 10)
        node.create_subscription(GoalStatusArray, NAV_STATUS_TOPIC, self._on_nav_status, _ACTION_STATUS_QOS)
        node.create_timer(1.0 / FPS, self._tick)
        self._logger.info(
            f"[Expressive] driver up: {FPS:.0f} Hz, planner server {config.server_url or '(none)'}, "
            f"idle breathing {'on' if config.idle_breathing else 'off'}"
        )

    # ================= hooks (any thread) =================
    def express(self, prompt: str, request_id: str | None = None) -> None:
        """Generate a clip for ``prompt`` in the background and play it; motion keeps flowing meanwhile."""
        prompt = " ".join(prompt.split())
        if not prompt:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq

        def make() -> Clip:
            made = self._maker.make(prompt)
            self._logger.info(
                f"[Expressive] '{prompt}' -> {made.clip.name} ({made.source}, {made.clip.duration:.1f} s)"
            )
            self._stage(seq, made, request_id)
            return made.clip

        self._animator.play(make)

    def play(self, clip: Clip, request_id: str | None = None) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._stage(seq, Made(clip, ClipSource.PLAYED), request_id)
        self._animator.play(clip)

    def stop(self) -> None:
        with self._lock:
            self._seq += 1
            self._label = None
        self._animator.stop()

    def set_gaze(self, head_deg: float | None) -> None:
        """The gaze tracker's tilt target; the expression rides on it, and leaves the base to it."""
        self._gaze = head_deg is not None
        self._animator.set_gaze(head_deg)

    def feed_audio(self, pcm: bytes, sample_rate: int) -> None:
        """The robot's own voice, PCM s16le mono, as it reaches the speaker (TTS thread)."""
        try:
            data = self._pcm_carry + pcm
            whole = len(data) - len(data) % 2
            self._pcm_carry = data[whole:]
            self._animator.feed_speech(np.frombuffer(data[:whole], dtype="<i2"), sample_rate)
        except Exception as error:  # noqa: BLE001 — a motion bug must not silence the robot
            self._logger.error(f"[Expressive] speech sway failed: {error!r}")

    def cut_audio(self) -> None:
        self._pcm_carry = b""
        self._animator.interrupt_speech()

    def close(self) -> None:
        """Leave the base stopped: a stance in flight would otherwise coast until the deadman."""
        if self._stance.release() is not None:
            self._cmd_vel_pub.publish(Twist())

    def _stage(self, seq: int, made: Made, request_id: str | None) -> None:
        with self._lock:
            if seq == self._seq:
                self._label = _Label(made.clip.name, made.source, request_id)

    # ================= the tick (executor thread) =================
    def _tick(self) -> None:
        now = time.monotonic()
        try:
            self._step(now)
        except Exception as error:  # noqa: BLE001 — a timer callback must never take the brain node down
            if now - self._last_error_at > 5.0:
                self._last_error_at = now
                self._logger.error(f"[Expressive] tick failed: {error!r}")

    def _step(self, now: float) -> None:
        dt = min(now - self._last_tick, 0.2) if self._last_tick else 1.0 / FPS
        self._last_tick = now
        self._follow_activation(now)
        stage = self._animator.state(now)  # before the tick: a clip enters one tick after it is seen
        if stage["playing"] or stage["speaking"] or (self._config.idle_breathing and self._state.is_brain_active):
            self._live_until = now + LINGER_S
        masked = self._masked()
        if self._was_masked and not masked:
            self._unmasked_at = now
        self._was_masked = masked
        live = now < self._live_until and not masked
        streaming = live and now >= self._arm_quiet_until
        if streaming and not self._streaming:
            self._enter_from_measured()
        self._streaming = streaming
        pose = self._pose = self._animator.tick(now)
        self._drive_head(pose, (live or (self._gaze and not masked)) and now >= self._head_quiet_until)
        if streaming:
            self._drive_arm(pose)
        self._drive_base(pose, dt, not masked and not self._gaze and now >= self._base_quiet_until, now)
        self._ticks += 1
        if self._ticks % STATE_EVERY == 0:
            self._publish_state(stage, masked)

    def _enter_from_measured(self) -> None:
        """Start the stream where the body is: the measured arm and head, the base where it was aimed."""
        self._grip_lock = self._held_grip()
        if self._joints is None:
            return
        base = self._pose.vector[7:] if self._pose is not None else (0.0, 0.0)
        measured = [*self._joints[:6], math.degrees(self._joints[6]), *base]
        self._animator.enter_from(ActuatorPose(np.array(measured, dtype=np.float64)), ENTER_S)

    def _follow_activation(self, now: float) -> None:
        active = self._state.is_brain_active
        if active and not self._was_active:
            self._arm_quiet_until = max(self._arm_quiet_until, now + ACTIVATION_HOLD_S)
        self._was_active = active

    def _masked(self) -> bool:
        running = self._state.primitive_running
        skill = running is not None and not _is_express(running.skill_id)
        return skill or self._navigating or bool(self._running_skills)

    def _drive_head(self, pose: ActuatorPose, on: bool) -> None:
        if not on:
            self._head_cmd = None  # whoever had the head moved it: re-send on resume
            return
        degrees = int(round(pose.head_deg))
        if degrees == self._head_cmd:
            return
        self._head_cmd = degrees
        self._ours_head.append(degrees)
        self._head_pub.publish(Int32(data=degrees))

    def _drive_arm(self, pose: ActuatorPose) -> None:
        command = pose.arm
        if self._grip_lock is not None:
            command[5] = self._grip_lock
        self._ours.append(tuple(command))
        self._arm_pub.publish(Float64MultiArray(data=command))

    def _held_grip(self) -> float | None:
        """The commanded close to keep when the claw holds something: the measured claw stands open
        of it. Re-commanding j6 above a grip's target drops the object (robot/manipulation.py)."""
        held = self._standing_grip()
        if held is None or self._joints is None or self._joints[5] - held < GRIP_BLOCKED_RAD:
            return None
        self._logger.info(f"[Expressive] the claw holds something: keeping j6 at {held:.2f}")
        return held

    def _drive_base(self, pose: ActuatorPose, dt: float, free: bool, now: float) -> None:
        if free:
            fresh = self._odom if now - self._odom_at < ODOM_STALE_S else None
            twist = self._stance.step(pose.base_yaw, pose.base_x, fresh, dt)
        else:
            twist = self._stance.release()
        if twist is None:
            return
        msg = Twist()
        msg.linear.x, msg.angular.z = twist
        self._cmd_vel_pub.publish(msg)

    def _publish_state(self, stage: AnimatorState, masked: bool) -> None:
        with self._lock:
            label = self._label if stage["playing"] and self._label and self._label.name == stage["name"] else None
        payload = {
            **stage,
            "t": round(stage["t"], 2),
            "duration": round(stage["duration"], 2),
            "masked": masked,
            "source": label.source if label is not None else None,
            "id": label.request_id if label is not None else None,
        }
        self._state_pub.publish(String(data=json.dumps(payload)))

    # ================= subscriptions (executor thread) =================
    def _on_prompt(self, msg: String) -> None:
        """A bare prompt, or ``{"prompt", "id"}`` so a waiter can find its clip on the state topic."""
        prompt, request_id = msg.data, None
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            prompt, request_id = str(data.get("prompt", "")), data.get("id")
        self.express(prompt, None if request_id is None else str(request_id))

    def _on_play(self, msg: String) -> None:
        try:
            clip = Clip.load(msg.data)
        except (ValueError, KeyError, TypeError) as error:
            self._logger.warning(f"[Expressive] ignoring {PLAY_TOPIC}: {error}")
            return
        self.play(clip)

    def _on_generate_request(self, msg: String) -> None:
        try:
            data = json.loads(msg.data)
            request_id, prompt = data["id"], str(data["prompt"])
        except (json.JSONDecodeError, TypeError, KeyError):
            self._logger.warning(f"[Expressive] ignoring {GENERATE_REQ_TOPIC}: expected {{id, prompt}}")
            return
        threading.Thread(target=self._generate, args=(request_id, prompt), daemon=True).start()

    def _generate(self, request_id: object, prompt: str) -> None:
        try:
            made = self._maker.make(prompt)
            reply = {"id": request_id, "clip": made.clip.to_dict(), "source": made.source}
        except Exception as error:  # noqa: BLE001 — the studio waits on this id: it must get an answer
            reply = {"id": request_id, "error": str(error) or type(error).__name__}
        self._generated_pub.publish(String(data=json.dumps(reply)))

    def _on_skill_status(self, msg: String) -> None:
        """Mask while any skill but express runs (keyed by skill id: the app's mirror of a run and the
        skills server's own report carry different run ids), and react when one ends.

        Masking is silence, not ``Animator.set_mask``: the animator's mask scales the pose toward
        NEUTRAL after ``enter_from``, so a released mask would start the stream at NEUTRAL instead of
        where the skill left the arm."""
        try:
            payload = json.loads(msg.data)
            status, skill = str(payload["status"]), str(payload.get("skill_id") or payload.get("skill_name") or "")
        except (json.JSONDecodeError, TypeError, KeyError):
            return
        if not skill or _is_express(skill):
            return
        if status == "running":
            self._running_skills.add(skill)
            return
        if status not in TERMINAL or skill not in self._running_skills:
            return
        self._running_skills.discard(skill)
        if self._running_skills:
            return
        reaction = {"completed": self._config.on_skill_completed, "failed": self._config.on_skill_failed}
        if reaction.get(status):
            self.express(reaction[status])

    def _on_odom(self, msg: Odometry) -> None:
        position = msg.pose.pose.position
        self._odom = (position.x, position.y, quaternion_to_yaw(msg.pose.pose.orientation))
        self._odom_at = time.monotonic()

    def _on_joint_states(self, msg: JointState) -> None:
        positions = dict(zip(msg.name, msg.position, strict=False))
        if all(joint in positions for joint in JOINTS):
            self._joints = [float(positions[joint]) for joint in JOINTS]

    def _on_arm_command(self, msg: Float64MultiArray) -> None:
        now = time.monotonic()
        if tuple(msg.data) not in self._ours and self._outsider(now):
            self._arm_quiet_until = max(self._arm_quiet_until, now + FOREIGN_HOLD_S)

    def _on_head_command(self, msg: Int32) -> None:
        now = time.monotonic()
        if msg.data not in self._ours_head and self._outsider(now):
            self._head_quiet_until = now + FOREIGN_HOLD_S

    def _outsider(self, now: float) -> bool:
        """A command from neither us nor the skill that just owned the body (it yields by finishing)."""
        return not self._was_masked and now - self._unmasked_at > SKILL_TAIL_S

    def _on_nav_status(self, msg: GoalStatusArray) -> None:
        self._navigating = any(goal.status in NAV_LIVE for goal in msg.status_list)

    def _on_joystick(self, msg: Vector3) -> None:
        if msg.x or msg.y or msg.z:
            self._base_quiet_until = time.monotonic() + JOYSTICK_HOLD_S


def _is_express(skill_id: str) -> bool:
    return skill_id.rsplit("/", 1)[-1] == EXPRESS_SKILL
