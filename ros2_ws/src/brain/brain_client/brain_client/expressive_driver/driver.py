# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The expression layer's ROS edge, inside the brain client node: one Animator ticked at 30 Hz.

Each tick's ActuatorPose goes to the head servo (when its integer degree changes), the arm's
streaming pass-through, and the base through a stance tracker on /odom. Whenever the arm stream
(re)starts, the animator enters from the measured pose, so expression never jumps from wherever
a skill or an operator left the body; the animator also rate-limits every joint. The body is yielded
whenever something else owns it: a running skill masks the parts it declares and a Nav2 goal
everything, a foreign /mars/arm/commands stream (teleop, the arm SDK page) or Mad mode's arm brace
the arm, a foreign head command the head, the joystick and the gaze tracker the base;
``expressive.enabled_parts`` keeps the rest still for a staged bring-up. Outside a clip or speech the arm
streams only while an agent is running and idle breathing is on, so a robot nobody talks to keeps
its arm wherever skills and auto-rest put it.

Prompts arrive on topics, from the express skill and from the agent's emote tags, and become clips
down the fallback chain in ``sources``. Clips are synthesized to actuator frames on a worker
thread, never on the executor or under the lock, and a newer request, a stop or a deactivation
drops a clip still on its way.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

import numpy as np
from action_msgs.msg import GoalStatus, GoalStatusArray
from geometry_msgs.msg import Twist, Vector3
from nav_msgs.msg import Odometry
from rcl_interfaces.msg import SetParametersResult
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray, Int32, String

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.expressive import presets
from brain_client.expressive.animator import Animator
from brain_client.expressive.basis import ActuatorPose, Basis
from brain_client.expressive.breathing import Breathing
from brain_client.expressive.motion import Clip
from brain_client.expressive_driver import vocal
from brain_client.expressive_driver.sources import ClipMaker, ClipSource, Made
from brain_client.expressive_driver.stance import StanceTracker
from brain_client.expressive_driver.utils import ALL_PARTS, Parts, is_mad_mode, leaves_body, parts_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from innate_llm import Provider
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.publisher import Publisher

    from brain_client.core.state import BrainState
    from brain_client.expressive.animator import AnimatorState
    from brain_client.perception.pose import Pose

FPS = 30.0
BLEND_S = 0.4
STATE_EVERY = 6  # ticks: 5 Hz
ENTER_S = 1.5  # crossfade from the measured pose into the animation when output (re)starts
LINGER_S = BLEND_S  # keep streaming past a clip's end while the animator crossfades back to idle
LATE_TICK_S = 2.0 / FPS
ECHO_S = 1.0  # our own head command comes back within this, or it was lost
MAX_CLIP_JSON = 4_000_000  # bytes: a 40 s actuator clip at 1000 fps fits; nothing legitimate is larger
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
REPLY_EMOTE_S = 3.0  # a reaction would repeat what the agent's own emote just said

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
    stand_in: bool  # play the keyword preset at once while a prompt's clip is generated
    vocalize: bool  # a short non-verbal sound with an emote nobody is speaking over
    enabled_parts: Parts  # the rest of the body never moves (scripts/expressive_bringup.sh stages it)
    speech_latency_s: float  # how far the speaker trails the speech-sway tap
    server_url: str
    on_skill_completed: str  # a preset name, or a prompt to generate; "" = no reaction
    on_skill_failed: str

    @classmethod
    def load(cls, node: Node) -> ExpressiveConfig | None:
        """None when ``expressive.enabled`` is false: then nothing of the layer exists."""
        enabled = node.declare_parameter("expressive.enabled", True).value
        config = cls(
            idle_breathing=bool(node.declare_parameter("expressive.idle_breathing", True).value),
            stand_in=bool(node.declare_parameter("expressive.stand_in", True).value),
            vocalize=bool(node.declare_parameter("expressive.vocalize", True).value),
            enabled_parts=_parts(node.declare_parameter("expressive.enabled_parts", sorted(ALL_PARTS)).value),
            speech_latency_s=float(node.declare_parameter("expressive.speech_latency_s", 0.0).value or 0.0),
            server_url=str(node.declare_parameter("expressive.server_url", "").value),
            on_skill_completed=str(node.declare_parameter("expressive.on_skill_completed", "agreeing").value),
            on_skill_failed=str(node.declare_parameter("expressive.on_skill_failed", "sad").value),
        )
        return config if enabled else None


class Voice(Protocol):
    """The robot's speaker, shared with speech (transport/tts.py ``TTSHandler``)."""

    def busy(self) -> bool: ...

    def play_sound_async(self, pcm: bytes, sample_rate: int) -> bool: ...


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
        voice: Voice | None = None,
    ) -> None:
        """``standing_grip`` is the arm's last commanded j6 (hardware only); ``provider`` the brain's LLM."""
        self._logger = node.get_logger()
        self._state = state
        self._config = config
        self._cmd_vel_pub = cmd_vel_pub
        self._standing_grip = standing_grip
        self._voice = voice
        self._maker = ClipMaker(config.server_url, provider, self._logger)
        self._basis = Basis.load()
        self._animator = Animator(
            fps=FPS,
            idle=Breathing() if config.idle_breathing else STILL,
            blend_s=BLEND_S,
            basis=self._basis,
            speech_latency_s=config.speech_latency_s,
        )
        self._stance = StanceTracker()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="expressive")
        # A pool, not a thread per prompt: Thread.start() waits for the new thread to take the GIL, which
        # a synthesizing worker holds for milliseconds — too long for the speech streamer calling in.
        self._generators = ThreadPoolExecutor(max_workers=3, thread_name_prefix="expressive-gen")
        if config.vocalize:
            self._submit(_warm_sounds)  # the first synthesis pays numpy's FFT setup: not on an emote
        self._lock = threading.Lock()  # the request bookkeeping below; hooks arrive on many threads

        self._seq = 0  # bumped by every request and stop: a clip made for an older one is dropped
        self._final_seq = 0  # the request whose own clip is on stage: its stand-in comes too late
        self._labels: deque[_Label] = deque(maxlen=4)  # newest last: a stand-in and its replacement overlap
        self._pcm_carry = b""
        self._abandon_stance = False  # set by stop() on any thread, acted on by the tick

        self._odom: Pose | None = None
        self._odom_at = 0.0
        self._joints: list[float] | None = None  # measured j1..j6 rad, head rad
        self._running_skills: dict[str, Parts] = {}
        self._skill_parts: dict[str, Parts] = {}  # last declared parts per skill: the agent's slot claims first
        self._navigating = False
        self._enabled = config.enabled_parts
        self._masked_now: Parts = frozenset()
        self._released_at: dict[str, float] = {}  # when a skill last let go of each part
        self._reply_emote_at = -math.inf
        self._gaze = False
        self._streaming = False
        self._grip_lock: float | None = None
        self._pose: ActuatorPose | None = None
        self._head_cmd: int | None = None
        self._head_on = False
        self._head_entry: tuple[float, float] | None = None  # (start, measured degrees) of a resume
        self._head_quiet_until = 0.0
        self._ours: deque[tuple[float, ...]] = deque(maxlen=64)  # recent arm commands: our own echoes
        self._head_echoes: deque[tuple[int, float]] = deque(maxlen=64)  # (degrees, sent) not yet echoed
        self._mad = False
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
        # String, payload ignored: rosbridge (rws) serializes std_msgs/Empty to 0 bytes, which ROS cannot read.
        node.create_subscription(String, STOP_TOPIC, lambda _msg: self.stop(), 10)
        node.create_subscription(String, GENERATE_REQ_TOPIC, self._on_generate_request, 10)
        node.create_subscription(String, "/brain/skill_status_update", self._on_skill_status, 10)
        node.create_subscription(Odometry, "/odom", self._on_odom, 10)
        node.create_subscription(JointState, "/joint_states", self._on_joint_states, qos_profile_sensor_data)
        node.create_subscription(Float64MultiArray, "/mars/arm/commands", self._on_arm_command, qos_profile_sensor_data)
        node.create_subscription(Int32, "/mars/head/set_position", self._on_head_command, 10)
        node.create_subscription(Vector3, "/joystick", self._on_joystick, 10)
        node.create_subscription(GoalStatusArray, NAV_STATUS_TOPIC, self._on_nav_status, _ACTION_STATUS_QOS)
        node.create_subscription(String, "/robot/info", self._on_robot_info, 10)
        node.create_timer(1.0 / FPS, self._tick)
        node.add_on_set_parameters_callback(self._on_parameters)
        self._logger.info(
            f"[Expressive] driver up: {FPS:.0f} Hz, planner server {config.server_url or '(none)'}, "
            f"idle breathing {'on' if config.idle_breathing else 'off'}"
        )

    # ================= hooks (any thread) =================
    def express(self, prompt: str, request_id: str | None = None, *, vocalize: bool = True) -> None:
        """Play ``prompt``'s keyword preset at once (with ``stand_in``), and the generated clip when it
        arrives; a newer play, prompt or stop supersedes a clip still being generated. Cheap: safe
        to call from the speech streamer while it holds its lock."""
        prompt = " ".join(prompt.split())
        if not prompt:
            return
        seq = self._next_seq()
        stand_in = presets.match(prompt)
        if self._config.stand_in:
            self._submit(self._play_preset, seq, stand_in, ClipSource.STAND_IN, request_id, prompt)
        if vocalize:
            self._submit(self._vocalize, seq, prompt, stand_in)
        self._generators.submit(self._make_and_play, seq, prompt, request_id)

    def emote(self, prompt: str) -> None:
        """An emote tag from the agent's reply, played as its sentence goes to TTS — never voiced:
        the sentence it opens is about to be spoken."""
        self._reply_emote_at = time.monotonic()
        self.express(prompt, vocalize=False)

    def play_preset(self, name: str, request_id: str | None = None) -> None:
        """A built-in preset, at once and with its sound (``name`` must be in ``presets.PRESETS``)."""
        seq = self._next_seq()
        self._submit(self._play_preset, seq, name, ClipSource.PRESET, request_id)
        self._submit(self._vocalize, seq, "", name)  # its own sound: the name's words would pick another (sleepy)

    def play(self, clip: Clip, request_id: str | None = None) -> None:
        seq = self._next_seq()
        self._submit(self._prepare_and_play, seq, Made(clip, ClipSource.PLAYED), request_id)

    def stop(self) -> None:
        """Back to idle now: the clip on stage, any clip still being made, the speech sway, and the
        stance — the base stops where it is instead of driving back to the anchor."""
        with self._lock:
            self._seq += 1
            self._labels.clear()
            self._animator.stop()
        self._animator.interrupt_speech()
        self._abandon_stance = True

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
        self._worker.shutdown(wait=False, cancel_futures=True)
        self._generators.shutdown(wait=False, cancel_futures=True)
        if self._stance.release() is not None:
            self._cmd_vel_pub.publish(Twist())

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _submit(self, job: Callable[..., None], *args: object) -> None:
        self._worker.submit(self._run_job, job, *args)

    def _run_job(self, job: Callable[..., None], *args: object) -> None:
        try:
            job(*args)
        except Exception as error:  # noqa: BLE001 — one bad clip must not stop the worker, nor vanish in a Future
            self._logger.error(f"[Expressive] {getattr(job, '__name__', 'job')} failed: {error!r}")

    # ================= clip delivery (worker and generation threads) =================
    def _make_and_play(self, seq: int, prompt: str, request_id: str | None) -> None:
        if seq != self._seq:
            return  # superseded before a generator was free: spare the LLM call
        try:
            started = time.monotonic()
            made = self._maker.make(prompt)
            self._logger.info(
                f"[Expressive] '{prompt}' -> {made.clip.name} ({made.source}, {time.monotonic() - started:.1f} s)"
            )
            if made.source == ClipSource.PRESET and self._config.stand_in:
                self._submit(self._settle_on_stand_in, seq, made.clip.name, request_id)  # after the stand-in job
                return
            self._prepare_and_play(seq, made, request_id)
        except Exception as error:  # noqa: BLE001 — a thread's crash reporter: generation must not die silently
            self._logger.error(f"[Expressive] could not make a clip for '{prompt}': {error!r}")

    def _vocalize(self, seq: int, prompt: str, preset: str) -> None:
        """A non-verbal sound for an emote, unless the robot speaks or is about to (worker thread)."""
        if seq != self._seq or not self._config.vocalize or self._voice is None or self._voice.busy():
            return
        sound = vocal.sound_for(prompt, preset)
        if sound is not None:
            self._voice.play_sound_async(vocal.synthesize(sound, seq % 3).tobytes(), vocal.RATE)

    def _play_preset(self, seq: int, name: str, source: ClipSource, request_id: str | None, prompt: str = "") -> None:
        self._prepare_and_play(seq, Made(presets.clip(name, prompt=prompt), source), request_id)

    def _prepare_and_play(self, seq: int, made: Made, request_id: str | None) -> None:
        """Synthesize ``made`` to actuator frames here, off the executor and outside the lock."""
        if seq != self._seq:
            return  # superseded while queued: spare the synthesis
        prepared = made.clip.to_actuators(self._basis).resample(FPS)
        with self._lock:
            if seq != self._seq or (made.source == ClipSource.STAND_IN and self._final_seq == seq):
                return
            if made.source != ClipSource.STAND_IN:
                self._final_seq = seq
            self._labels.append(_Label(prepared.name, made.source, request_id))
            self._animator.play(prepared)

    def _settle_on_stand_in(self, seq: int, name: str, request_id: str | None) -> None:
        """The chain only reached the preset the stand-in already is: label it final, let it play out."""
        with self._lock:
            if seq == self._seq:
                self._labels.append(_Label(name, ClipSource.PRESET, request_id))

    # ================= the tick (executor thread) =================
    def _tick(self) -> None:
        now = time.monotonic()
        if self._last_tick and now - self._last_tick > LATE_TICK_S:
            self._logger.warning(
                f"[Expressive] tick {1000 * (now - self._last_tick):.0f} ms after the last: the executor was busy",
                throttle_duration_sec=5.0,
            )
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
        # Speech and breathing animate only an agent that runs: a deactivated robot finishing its last
        # sentence keeps its arm still.
        agent = self._state.is_brain_active and (stage["speaking"] or self._config.idle_breathing)
        if stage["playing"] or agent:
            self._live_until = now + LINGER_S
        masked = self._masked()
        for part in self._masked_now - masked:
            self._released_at[part] = now
        self._masked_now = masked
        held = masked | (ALL_PARTS - self._enabled)
        live = now < self._live_until
        streaming = live and "arm" not in held and now >= self._arm_quiet_until and not self._mad
        entering = streaming and not self._streaming
        if entering:
            self._enter_from_measured()
        self._streaming = streaming
        head_on = (live or self._gaze) and "head" not in held and now >= self._head_quiet_until
        if head_on and not self._head_on and not entering and self._joints is not None:
            self._head_entry = (now, math.degrees(self._joints[6]))  # the arm's entry blend covers the head
        self._head_on = head_on
        pose = self._pose = self._animator.tick(now)
        self._drive_head(pose, head_on, now)
        if streaming:
            self._drive_arm(pose)
        self._drive_base(pose, dt, "base" not in held and not self._gaze and now >= self._base_quiet_until, now)
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

    def _masked(self) -> Parts:
        """The parts something else owns right now: running skills' declared parts, everything for a
        Nav2 goal. The agent's own skill slot is claimed before the skills server reports the run."""
        if self._navigating:
            return ALL_PARTS
        masked = frozenset().union(*self._running_skills.values())
        running = self._state.primitive_running
        if running is not None and running.skill_id not in self._running_skills:
            masked |= self._skill_parts.get(running.skill_id, parts_of(running.skill_id, None))
        return masked

    def _drive_head(self, pose: ActuatorPose, on: bool, now: float) -> None:
        if not on:
            self._head_cmd = None  # whoever had the head moved it: re-send on resume
            self._head_entry = None
            return
        target = pose.head_deg
        if self._head_entry is not None:
            start, measured = self._head_entry
            ramp = min(1.0, (now - start) / ENTER_S)
            target = measured + ramp * ramp * (3.0 - 2.0 * ramp) * (target - measured)
            if ramp >= 1.0:
                self._head_entry = None
        degrees = int(round(target))
        if degrees == self._head_cmd:
            return
        self._head_cmd = degrees
        self._head_echoes.append((degrees, now))
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
        if self._abandon_stance:
            self._abandon_stance = False
            twist = self._stance.abandon()
        elif free:
            fresh = self._odom if now - self._odom_at < ODOM_STALE_S else None
            twist = self._stance.step(pose.base_yaw, pose.base_x, fresh, dt)
        else:
            twist = self._stance.release()
        if twist is None:
            return
        msg = Twist()
        msg.linear.x, msg.angular.z = twist
        self._cmd_vel_pub.publish(msg)

    def _publish_state(self, stage: AnimatorState, masked: Parts) -> None:
        with self._lock:
            on_stage = [label for label in self._labels if stage["playing"] and label.name == stage["name"]]
        label = on_stage[-1] if on_stage else None
        payload = {
            **stage,
            "t": round(stage["t"], 2),
            "duration": round(stage["duration"], 2),
            "masked": bool(masked),
            "masked_parts": sorted(masked),
            "source": label.source if label is not None else None,
            "id": label.request_id if label is not None else None,
        }
        self._state_pub.publish(String(data=json.dumps(payload)))

    # ================= subscriptions (executor thread) =================
    def _on_prompt(self, msg: String) -> None:
        """A bare prompt, ``{"prompt", "id"}`` so a waiter can find its clip on the state topic, or
        ``{"preset", "id"}`` to play a built-in one exactly (head_emotion)."""
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            self.express(msg.data)
            return
        request_id = None if data.get("id") is None else str(data["id"])
        preset = data.get("preset")
        if isinstance(preset, str) and preset in presets.PRESETS:
            self.play_preset(preset, request_id)
            return
        self.express(str(data.get("prompt") or preset or ""), request_id)

    def _on_play(self, msg: String) -> None:
        """Clip JSON only — never a path: the topic is open to anything on the network. Parsed on the
        worker: a megabyte of JSON parsed here stalls the tick."""
        if len(msg.data) > MAX_CLIP_JSON:
            self._logger.warning(f"[Expressive] ignoring {PLAY_TOPIC}: {len(msg.data)} bytes of clip JSON")
            return
        self._submit(self._parse_and_play, self._next_seq(), msg.data)

    def _parse_and_play(self, seq: int, payload: str) -> None:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("expected a Clip JSON object")
            clip = Clip.from_dict(data)
        except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as error:
            self._logger.warning(f"[Expressive] ignoring {PLAY_TOPIC}: {error}")
            return
        self._prepare_and_play(seq, Made(clip, ClipSource.PLAYED), None)

    def _on_generate_request(self, msg: String) -> None:
        try:
            data = json.loads(msg.data)
            request_id, prompt = data["id"], str(data["prompt"])
        except (json.JSONDecodeError, TypeError, KeyError):
            self._logger.warning(f"[Expressive] ignoring {GENERATE_REQ_TOPIC}: expected {{id, prompt}}")
            return
        self._generators.submit(self._generate, request_id, prompt)

    def _generate(self, request_id: object, prompt: str) -> None:
        try:
            made = self._maker.make(prompt)
            reply = {"id": request_id, "clip": made.clip.to_dict(), "source": made.source}
        except Exception as error:  # noqa: BLE001 — the studio waits on this id: it must get an answer
            reply = {"id": request_id, "error": str(error) or type(error).__name__}
        self._generated_pub.publish(String(data=json.dumps(reply)))

    def _on_skill_status(self, msg: String) -> None:
        """Mask the parts a running skill may move (keyed by skill id: the app's mirror of a run and the
        skills server's own report carry different run ids), and react when it ends. The server's
        report names the parts; a report without them masks everything, unless the same skill's
        parts are already known from another report of this run.

        Masking is silence, not ``Animator.set_mask``: the animator's mask scales the pose toward
        NEUTRAL after ``enter_from``, so a released mask would start the stream at NEUTRAL instead of
        where the skill left the arm."""
        try:
            payload = json.loads(msg.data)
            status, skill = str(payload["status"]), str(payload.get("skill_id") or payload.get("skill_name") or "")
        except (json.JSONDecodeError, TypeError, KeyError):
            return
        if not skill or leaves_body(skill):
            return
        if status == "running":
            declared = payload.get("body")
            if declared is None and skill in self._running_skills:
                return  # the app's mirror of a run the server already described
            parts = parts_of(skill, declared)
            if declared is not None:
                self._skill_parts[skill] = parts
            self._running_skills[skill] = parts
            return
        if status not in TERMINAL or self._running_skills.pop(skill, None) is None:
            return
        if self._running_skills:
            return
        reaction = {"completed": self._config.on_skill_completed, "failed": self._config.on_skill_failed}.get(status)
        if not reaction or not self._state.is_brain_active or time.monotonic() - self._reply_emote_at <= REPLY_EMOTE_S:
            return
        if self._animator.playing is not None:
            return  # an emote still on stage says more than a generic nod would; cutting it short says less
        if reaction not in presets.PRESETS:
            self.express(reaction)  # free text: a prompt to generate
            return
        self.play_preset(reaction)

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
        if tuple(msg.data) not in self._ours and self._outsider("arm", now):
            self._arm_quiet_until = max(self._arm_quiet_until, now + FOREIGN_HOLD_S)

    def _on_head_command(self, msg: Int32) -> None:
        """Our own commands come back once each: consume the echo, so an operator's slider landing
        on a value we also sent still reads as foreign."""
        now = time.monotonic()
        while self._head_echoes and now - self._head_echoes[0][1] > ECHO_S:
            self._head_echoes.popleft()
        for index, (degrees, _sent) in enumerate(self._head_echoes):
            if degrees == msg.data:
                del self._head_echoes[index]
                return
        if self._outsider("head", now):
            self._head_quiet_until = now + FOREIGN_HOLD_S

    def _outsider(self, part: str, now: float) -> bool:
        """A command for ``part`` from neither us nor the skill that owns it or just let it go."""
        return part not in self._masked_now and now - self._released_at.get(part, -math.inf) > SKILL_TAIL_S

    def _on_nav_status(self, msg: GoalStatusArray) -> None:
        self._navigating = any(goal.status in NAV_LIVE for goal in msg.status_list)

    def _on_joystick(self, msg: Vector3) -> None:
        if msg.x or msg.y or msg.z:
            self._base_quiet_until = time.monotonic() + JOYSTICK_HOLD_S

    def _on_robot_info(self, msg: String) -> None:
        self._mad = is_mad_mode(msg.data)

    def _on_parameters(self, params: list[Parameter]) -> SetParametersResult:
        """``expressive.enabled_parts`` and ``expressive.vocalize`` apply at once; every other expressive
        parameter is read at startup."""
        for param in params:
            if param.name == "expressive.enabled_parts":
                parts = _parts(param.value)
                if len(parts) != len(set(param.value or ())):
                    return SetParametersResult(successful=False, reason=f"parts are {', '.join(sorted(ALL_PARTS))}")
                self._enabled = parts
            elif param.name == "expressive.vocalize":
                self._config = replace(self._config, vocalize=bool(param.value))
                if param.value:
                    self._submit(_warm_sounds)
        return SetParametersResult(successful=True)


def _parts(names: object) -> Parts:
    if not isinstance(names, (list, tuple)):
        return ALL_PARTS
    return frozenset(str(name) for name in names) & ALL_PARTS


def _warm_sounds() -> None:
    for sound in vocal.Sound:
        for seed in range(3):
            vocal.synthesize(sound, seed)
