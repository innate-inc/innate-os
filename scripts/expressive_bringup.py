#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Staged hardware bring-up of the expression layer (docs/EXPRESSIVE.md, "Hardware bring-up").

Each stage lets one more part of the body move (``expressive.enabled_parts``), plays presets and speech
through the driver's topics, measures what reached the servos, the speaker and the base, and prints
PASS or FAIL with the numbers. Every run leaves its log and raw samples in /tmp/expressive_bringup/.
Ctrl-C stops the expression and restores the parameters. Run it through scripts/expressive_bringup.sh,
with the agent stopped and no skill running.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from mars_msgs.msg import ArmStatus
from nav_msgs.msg import Odometry
from rcl_interfaces.msg import ParameterType, ParameterValue
from rcl_interfaces.srv import GetParameters, SetParameters
from rclpy.client import Client
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy, qos_profile_sensor_data
from rclpy.signals import SignalHandlerOptions
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray, Int32, String

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.expressive import presets
from brain_client.expressive.basis import Basis
from brain_client.expressive_driver import vocal

LOG_DIR = Path("/tmp/expressive_bringup")
BRAIN = "brain_client_node"
STAGE_PARTS = {"head": ["head"], "voice": ["head"], "arm": ["arm", "head"], "full": ["arm", "base", "head"]}
HEAD_PRESETS = ("curious", "agreeing", "sad")
SWAY_PRESET = "listening"
SOUND_PRESETS = ("surprised", "sleepy")
ARM_PRESETS = ("agreeing", "curious", "happy", "proud", "excited")
BASE_PRESETS = ("confused", "curious", "sad", "affectionate", "scared")  # smallest turn or step first
LINE = "Hello! I am checking how my body moves while I talk, so please watch me for a moment."

# Acceptance thresholds (docs/EXPRESSIVE.md lists them with their reasons).
HEAD_RATE_MAX = 31.0  # commands in any 1 s: the 30 Hz tick at most
HEAD_RANGE_DEG = 20.0
HEAD_TRACK_P95_DEG = 4.0
HEAD_LAG_MAX_S = 0.3
SWAY_MIN_DEG = 0.3  # RMS head difference between a clip with and without speech: 0 when the tap is dead
SPEAKING_MIN = 0.8
SOUND_START_MAX_S = 0.5
SOUND_LENGTH_TOL_S = 0.3
LOAD_ABORT_PCT = 70.0
LOAD_ABORT_HOLD_S = 0.1  # a single acceleration spike is not a stall
J6_LIMIT_MA = 1300.0  # mars_arm config/arm_config.yaml: the claw reports current, not load
STEP_RATIO_MAX = 1.15  # measured against arrival intervals, which jitter a few ms around the 33 ms tick
BASE_RETURN_M = 0.03
BASE_RETURN_DEG = 3.0
BASE_REACH_M = 0.5
BASE_SPEED_MAX = (0.15 * 1.1, 0.6 * 1.1)  # stance.py caps, m/s and rad/s
SETTLE_S = 2.5

_LATCHED = QoSProfile(
    depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL, reliability=QoSReliabilityPolicy.RELIABLE
)
Sample = tuple[float, Any]


class Abort(Exception):
    """A stage cannot go on: a safety trip, a missing precondition, or a step that never happened."""


@dataclass(frozen=True)
class Check:
    name: str
    value: str
    ok: bool | None  # None: reported for the record, no threshold


class Probe(Node):
    """Every topic the stages measure, timestamped on arrival, and the arm watchdog."""

    def __init__(self) -> None:
        super().__init__("expressive_bringup")
        self.series: dict[str, list[Sample]] = {}
        self._lock = threading.Lock()
        self.tripped: str | None = None
        self.watch_arm = False
        self._loaded_since: float | None = None
        self.prompt = self.create_publisher(String, "/brain/express/prompt", 10)
        self._stop = self.create_publisher(String, "/brain/express/stop", 10)
        self.tts = self.create_publisher(String, "/brain/tts", 10)
        self.cmd_vel: Any = None
        self._get = self.create_client(GetParameters, f"/{BRAIN}/get_parameters")
        self._set = self.create_client(SetParameters, f"/{BRAIN}/set_parameters")
        self._listen(String, "/brain/express/state", "state", lambda m: json.loads(m.data))
        self._listen(String, "/brain/agent_status", "agent", lambda m: json.loads(m.data), _LATCHED)
        self._listen(String, "/tts/is_playing", "tts", lambda m: m.data == "true")
        self._listen(Int32, "/mars/head/set_position", "head_cmd", lambda m: m.data)
        self._listen(
            Float64MultiArray, "/mars/arm/commands", "arm_cmd", lambda m: list(m.data), qos_profile_sensor_data
        )
        self._listen(JointState, "/joint_states", "joints", _joints, qos_profile_sensor_data)
        self._listen(JointState, "/mars/arm/state", "arm_state", self._on_arm_state, qos_profile_sensor_data)
        self._listen(ArmStatus, "/mars/arm/status", "arm_status", self._on_arm_status)
        self._listen(Odometry, "/odom", "odom", _pose)

    def listen_cmd_vel(self, topic: str) -> None:
        self.cmd_vel = self.create_publisher(Twist, topic, 10)
        self._listen(Twist, topic, "cmd_vel", lambda m: (m.linear.x, m.angular.z))

    def _listen(self, kind: type, topic: str, key: str, value: Callable[[Any], Any], qos: Any = 10) -> None:
        self.series[key] = []

        def record(msg: Any) -> None:
            sample = (time.monotonic(), value(msg))
            with self._lock:
                self.series[key].append(sample)

        self.create_subscription(kind, topic, record, qos)

    def window(self, key: str, start: float = -math.inf, end: float = math.inf) -> list[Sample]:
        with self._lock:
            return [(t, v) for t, v in self.series[key] if start <= t <= end]

    def latest(self, key: str) -> Sample | None:
        with self._lock:
            return self.series[key][-1] if self.series[key] else None

    def stop_expression(self) -> None:
        self._stop.publish(String())

    def trip(self, reason: str) -> None:
        if self.tripped is None:
            self.tripped = reason
            self.stop_expression()

    def _on_arm_state(self, msg: JointState) -> tuple[list[float], list[float]]:
        effort = list(msg.effort[:6]) if len(msg.effort) >= 6 else [0.0] * 6  # the sim reports no effort
        loads = [abs(e) for e in effort[:5]] + [abs(effort[5]) / J6_LIMIT_MA * 100.0]
        if self.watch_arm:
            now = time.monotonic()
            if max(loads[:5]) <= LOAD_ABORT_PCT:
                self._loaded_since = None
            elif self._loaded_since is None:
                self._loaded_since = now
            elif now - self._loaded_since >= LOAD_ABORT_HOLD_S:
                self.trip(f"servo load {max(loads[:5]):.0f} % over {LOAD_ABORT_PCT:.0f} % for {LOAD_ABORT_HOLD_S} s")
        return list(msg.position[:6]), loads

    def _on_arm_status(self, msg: ArmStatus) -> tuple[bool, str, bool]:
        if self.watch_arm and not msg.is_ok:
            self.trip(f"/mars/arm/status: {msg.error}")
        return msg.is_ok, msg.error, msg.is_torque_enabled

    # ---- the brain's parameters ----
    def get_params(self, names: list[str]) -> dict[str, Any]:
        response = self._call(self._get, GetParameters.Request(names=names))
        return {name: _value(value) for name, value in zip(names, response.values, strict=True)}

    def set_params(self, values: dict[str, Any]) -> None:
        request = SetParameters.Request(
            parameters=[Parameter(k, value=v).to_parameter_msg() for k, v in values.items()]
        )
        for name, result in zip(values, self._call(self._set, request).results, strict=True):
            if not result.successful:
                raise Abort(f"could not set {name}: {result.reason or 'rejected'} (is the brain up to date?)")

    def _call(self, client: Client, request: Any, timeout: float = 5.0) -> Any:
        if not client.wait_for_service(timeout_sec=timeout):
            raise Abort(f"{client.srv_name} is not available: is brain_client_node running?")
        done = threading.Event()
        future = client.call_async(request)
        future.add_done_callback(lambda _future: done.set())
        if not done.wait(timeout):
            raise Abort(f"{client.srv_name} did not answer within {timeout:.0f} s")
        return future.result()


class Bench:
    """One stage's run: drive the driver, wait on what it reports, and keep the log."""

    def __init__(self, probe: Probe, stage: str) -> None:
        self.probe = probe
        self.stage = stage
        stamp = time.strftime("%Y%m%d-%H%M%S")
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"{stage}-{stamp}.log"
        self.data_path = LOG_DIR / f"{stage}-{stamp}.json"
        self._log = self.log_path.open("w")
        self.checks: list[Check] = []
        self.restore: dict[str, Any] = {}

    def say(self, text: str) -> None:
        print(text)
        self._log.write(text + "\n")
        self._log.flush()

    def check(self, name: str, value: str, ok: bool | None) -> None:
        self.checks.append(Check(name, value, ok))
        self.say(f"  {'info' if ok is None else 'PASS' if ok else 'FAIL'}  {name:50s} {value}")

    def configure(self, values: dict[str, Any]) -> None:
        """Set brain parameters for this stage, remembering the originals for ``finish``."""
        originals = self.probe.get_params(list(values))
        for name, original in originals.items():
            if original is None:
                raise Abort(f"{name} is not declared on {BRAIN}: the robot runs an older brain")
            self.restore.setdefault(name, original)
        self.probe.set_params(values)

    def finish(self) -> bool:
        self.probe.watch_arm = False
        self.probe.stop_expression()
        if self.probe.cmd_vel is not None and self.stage == "full":
            self.probe.cmd_vel.publish(Twist())
        if self.restore:
            try:
                self.probe.set_params(self.restore)
            except Abort as error:
                self.say(f"  could not restore the parameters: {error}")
        passed = bool(self.checks) and all(c.ok is not False for c in self.checks)
        self.say(f"\n{self.stage}: {'PASS' if passed else 'FAIL'}   log {self.log_path}")
        with self.probe._lock:
            json.dump(
                {"checks": [c.__dict__ for c in self.checks], "series": self.probe.series}, self.data_path.open("w")
            )
        self._log.close()
        return passed

    def wait(self, ready: Callable[[], float | None], timeout: float, what: str, abortable: bool = True) -> float:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if abortable and self.probe.tripped is not None:
                raise Abort(f"safety stop: {self.probe.tripped}")
            found = ready()
            if found is not None:
                return found
            time.sleep(0.02)
        raise Abort(f"{what} within {timeout:.0f} s")

    def pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        self.wait(lambda: time.monotonic() if time.monotonic() >= end else None, seconds + 1.0, "pause")

    def play(self, preset: str) -> tuple[float, float]:
        """Play ``preset`` and block until it has played out: its (start, end) on the monotonic clock."""
        request_id = uuid.uuid4().hex[:8]
        sent = time.monotonic()
        self.probe.prompt.publish(String(data=json.dumps({"preset": preset, "id": request_id})))
        start = self.wait(
            lambda: self._state(sent, lambda s: s["id"] == request_id and s["playing"]), 3.0, f"{preset} never started"
        )
        duration = presets.clip(preset).duration
        end = self.wait(
            lambda: self._state(start, lambda s: s["id"] != request_id or not s["playing"]),
            duration + 3.0,
            f"{preset} never finished",
        )
        return start, end

    def speech_starts(self, after: float) -> float:
        return self.wait(lambda: self._tts(after, True), 10.0, "the speaker never started (is TTS configured?)")

    def speech_ends(self, after: float) -> float:
        return self.wait(lambda: self._tts(after, False), 30.0, "the speaker never stopped")

    def _state(self, after: float, match: Callable[[dict], bool]) -> float | None:
        return next((t for t, s in self.probe.window("state", after) if match(s)), None)

    def _tts(self, after: float, playing: bool) -> float | None:
        return next((t for t, v in self.probe.window("tts", after) if v == playing), None)


# ================= stages =================


def check_stage(bench: Bench) -> None:
    probe = bench.probe
    bench.say("check: is everything the expression layer needs present?")
    names = [n for n, _ in probe.get_node_names_and_namespaces()]
    bench.check(BRAIN, "running" if BRAIN in names else "not found", BRAIN in names)
    params = probe.get_params(
        [
            "expressive.enabled",
            "expressive.enabled_parts",
            "expressive.vocalize",
            "expressive.idle_breathing",
            "expressive.stand_in",
            "expressive.server_url",
            "expressive.on_skill_completed",
            "expressive.on_skill_failed",
            "simulator_mode",
            "cmd_vel_topic",
        ]
    )
    for name, value in params.items():
        expected = {"expressive.enabled": True, "simulator_mode": False}.get(name)
        bench.check(name, json.dumps(value), None if expected is None else value == expected)
    bench.check(
        "expressive.enabled_parts declared",
        "yes" if params["expressive.enabled_parts"] is not None else "no: update the robot",
        params["expressive.enabled_parts"] is not None,
    )
    probe.listen_cmd_vel(str(params["cmd_vel_topic"]))
    t0 = time.monotonic()
    time.sleep(3.0)
    rates = {key: len(probe.window(key, t0)) / 3.0 for key in ("state", "joints", "arm_state", "odom")}
    bench.check("/brain/express/state", f"{rates['state']:.1f} Hz", 4.0 <= rates["state"] <= 6.0)
    for key, topic in (("joints", "/joint_states"), ("arm_state", "/mars/arm/state"), ("odom", "/odom")):
        bench.check(topic, f"{rates[key]:.0f} Hz", rates[key] > 5.0)
    for topic in (
        "/brain/express/prompt",
        "/brain/express/stop",
        "/brain/tts",
        "/mars/head/set_position",
        "/mars/arm/commands",
    ):
        count = probe.count_subscribers(topic)
        bench.check(f"{topic} subscribers", str(count), count >= 1)
    cmd_vel_subs = probe.count_subscribers(str(params["cmd_vel_topic"]))
    bench.check(f"{params['cmd_vel_topic']} subscribers", str(cmd_vel_subs), cmd_vel_subs >= 1)
    tts_pubs = probe.count_publishers("/tts/is_playing")
    bench.check("/tts/is_playing publishers (the speaker path)", str(tts_pubs), tts_pubs >= 1)
    try:
        ok, error, torque = _arm_status(bench)
        bench.check("/mars/arm/status", f"ok={ok} torque={torque} '{error}'", ok and torque)
    except Abort as error:
        bench.check("/mars/arm/status", str(error), False)
    agent = probe.latest("agent")
    active = bool(agent and agent[1].get("brain_active"))
    bench.check("agent stopped", "active: stop it on the Agent page" if active else "stopped", not active)
    state = probe.latest("state")
    held = state[1].get("masked_parts", []) if state else []
    bench.check("no skill holding the body", json.dumps(held), not held)
    url = str(params["expressive.server_url"] or "")
    if not url:
        bench.check("planner server", "none set: prompts use the brain's LLM (2-7 s)", None)
        return
    started = time.monotonic()
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2.0) as response:
            bench.check(
                "planner server",
                f"{url} answered {response.status} in {(time.monotonic() - started) * 1000:.0f} ms",
                True,
            )
    except OSError as error:
        bench.check("planner server", f"{url} unreachable ({error}); prompts fall back to the LLM", False)


def head_stage(bench: Bench) -> None:
    probe = bench.probe
    bench.say("head: only the head moves; three presets, then a spoken line over a clip")
    stage_start = time.monotonic()
    windows = []
    for preset in HEAD_PRESETS:
        start, end = bench.play(preset)
        windows.append((preset, start, end))
        bench.pause(0.5)
    baseline = bench.play(SWAY_PRESET)
    bench.pause(0.5)
    sent = time.monotonic()
    probe.tts.publish(String(data=LINE))
    speech_start = bench.speech_starts(sent)
    with_speech = bench.play(SWAY_PRESET)
    speech_end = bench.speech_ends(speech_start)
    stage_end = time.monotonic()

    head = probe.window("head_cmd", stage_start, stage_end)
    for preset, start, end in windows:
        count = len(probe.window("head_cmd", start, end + 0.5))
        bench.check(f"{preset}: head commands", str(count), count >= 5)
    degrees = [d for _, d in head]
    bench.check("head range", f"{min(degrees)}..{max(degrees)} deg", max(abs(d) for d in degrees) <= HEAD_RANGE_DEG)
    rate = _max_rate(head)
    bench.check("head command rate, worst 1 s", f"{rate:.0f}/s", rate <= HEAD_RATE_MAX)
    cap = float(Basis.load().max_speed[6]) / 30.0
    worst = max(abs(b - a) / (cap * max((tb - ta) * 30, 1.0)) for (ta, a), (tb, b) in pairwise(head))
    bench.check(
        "head step / (cap x ticks)",
        f"{worst:.2f} (cap {cap:.1f} deg/tick, rounding allows +1 deg)",
        worst <= 1.0 + 1.0 / cap,
    )
    lag, p95 = _tracking(
        head, [(t, math.degrees(j[6])) for t, j in probe.window("joints", stage_start, stage_end) if j]
    )
    bench.check("head servo tracking: lag", f"{lag * 1000:.0f} ms", lag <= HEAD_LAG_MAX_S)
    bench.check("head servo tracking: p95 error at that lag", f"{p95:.1f} deg", p95 <= HEAD_TRACK_P95_DEG)
    arm = len(probe.window("arm_cmd", stage_start, stage_end))
    base = len(probe.window("cmd_vel", stage_start, stage_end))
    bench.check("arm commands (arm disabled)", str(arm), arm == 0)
    bench.check("cmd_vel messages (base disabled)", str(base), base == 0)
    overlap = (with_speech[0], min(with_speech[1], speech_end))
    sway = _trace_rms(head, baseline, with_speech[0], overlap[1] - overlap[0])
    bench.check("speech sway at the head (RMS vs the silent clip)", f"{sway:.1f} deg", sway >= SWAY_MIN_DEG)
    first = next((t for t, s in probe.window("state", speech_start) if s["speaking"]), None)
    if first is None:
        bench.check("state 'speaking' while the speaker plays", "never: the sway tap got no audio", False)
        return
    bench.check(
        "sway starts after /tts/is_playing",
        f"{(first - speech_start) * 1000:.0f} ms (synthesis + aplay's start buffer)",
        None,
    )
    states = probe.window("state", first, speech_end - 0.3)
    speaking = sum(1 for _, s in states if s["speaking"]) / max(len(states), 1)
    bench.check(
        "state 'speaking' until the speaker stops", f"{speaking:.0%} of {len(states)} samples", speaking >= SPEAKING_MIN
    )


def voice_stage(bench: Bench) -> None:
    probe = bench.probe
    bench.say("voice: sounds with silent emotes, none over speech, never overlapping")
    bench.pause(1.0)
    for preset in SOUND_PRESETS:
        sound = vocal.sound_for("", preset)
        expected = len(vocal.synthesize(sound)) / vocal.RATE if sound is not None else 0.0
        sent = time.monotonic()
        _, end = bench.play(preset)
        intervals = _intervals(probe.window("tts", sent, end + 2.0))
        if not intervals:
            bench.check(f"{preset}: {sound} played", "no /tts/is_playing toggle", False)
            continue
        on, off = intervals[0]
        bench.check(
            f"{preset}: {sound} starts after the emote", f"{(on - sent) * 1000:.0f} ms", on - sent <= SOUND_START_MAX_S
        )
        bench.check(
            f"{preset}: {sound} length",
            f"{off - on:.2f} s (synthesized {expected:.2f} s)",
            abs(off - on - expected) <= SOUND_LENGTH_TOL_S,
        )
        bench.check(f"{preset}: sounds per emote", str(len(intervals)), len(intervals) == 1)
        bench.pause(1.0)

    sent = time.monotonic()
    probe.tts.publish(String(data=LINE))
    speech_start = bench.speech_starts(sent)
    probe.prompt.publish(String(data=json.dumps({"preset": "happy", "id": "voice-over-speech"})))
    speech_end = bench.speech_ends(speech_start)
    bench.pause(2.0)
    during = _intervals(probe.window("tts", sent, speech_end + 2.0))
    bench.check("emote during speech: speaker runs", f"{len(during)} (speech only)", len(during) == 1)

    bench.pause(3.0)
    sent = time.monotonic()
    probe.prompt.publish(String(data=json.dumps({"preset": "surprised", "id": "voice-then-speech"})))
    sound_start = bench.speech_starts(sent)
    probe.tts.publish(String(data=LINE))  # while the gasp plays: the voice must wait for it
    speech_start = bench.speech_starts(bench.speech_ends(sound_start))
    bench.speech_ends(speech_start)
    runs = _intervals(probe.window("tts", sent))
    gap = runs[1][0] - runs[0][1] if len(runs) >= 2 else math.nan
    bench.check(
        "sound, then speech: two separate runs",
        f"{len(runs)}; speech starts {gap * 1000:+.0f} ms after the sound",
        len(runs) == 2 and gap >= -0.01,
    )
    first, second = (vocal.sound_for("", preset) for preset in SOUND_PRESETS)
    heard = _ask(
        f"Did you hear a {first}, a {second}, then a {first} just before the speech, each clearly quieter than "
        "the voice, with no clicks? [y/N] "
    )
    bench.check(
        "operator: audible, under the voice, no clicks",
        {True: "yes", False: "no", None: "not asked (no terminal)"}[heard],
        heard,
    )


def arm_stage(bench: Bench, rounds: int) -> None:
    probe = bench.probe
    bench.say(f"arm: the arm streams with the head, base held; {rounds} round(s) of {', '.join(ARM_PRESETS)}")
    ok, error, torque = _arm_status(bench)
    if not (ok and torque):
        raise Abort(f"the arm is not ready: ok={ok} torque={torque} '{error}'")
    probe.watch_arm = True
    start = time.monotonic()
    try:
        for _ in range(rounds):
            for preset in ARM_PRESETS:
                bench.play(preset)
                bench.pause(0.5)
    finally:
        _arm_checks(bench, start, time.monotonic())
    base = len(probe.window("cmd_vel", start))
    bench.check("cmd_vel messages (base disabled)", str(base), base == 0)


def full_stage(bench: Bench) -> None:
    probe = bench.probe
    bench.say("full: the base turns and steps too (up to 0.6 rad/s, 0.15 m/s, about 25 cm)")
    answer = _ask("Clear a 1 m radius around the robot and keep a hand on its power. Type YES to start: ", exact="YES")
    if not answer:
        raise Abort("the base stage needs an operator at the robot (type YES)")
    ok, error, torque = _arm_status(bench)
    if not (ok and torque):
        raise Abort(f"the arm is not ready: ok={ok} torque={torque} '{error}'")
    if probe.latest("odom") is None:
        raise Abort("no /odom: the base cannot be checked")
    probe.watch_arm = True
    stage_start = time.monotonic()
    try:
        for preset in BASE_PRESETS:
            anchor = _last(probe, "odom")
            start, end = bench.play(preset)
            bench.pause(SETTLE_S)
            _base_checks(bench, preset, anchor, start, end + SETTLE_S)
    finally:
        _arm_checks(bench, stage_start, time.monotonic())


def _base_checks(bench: Bench, preset: str, anchor: tuple[float, float, float], start: float, settled: float) -> None:
    poses = [pose for _, pose in bench.probe.window("odom", start, settled)]
    final = poses[-1]
    reach = max(math.hypot(x - anchor[0], y - anchor[1]) for x, y, _ in poses)
    turn = max(abs(math.degrees(math.remainder(yaw - anchor[2], math.tau))) for _, _, yaw in poses)
    offset = math.hypot(final[0] - anchor[0], final[1] - anchor[1])
    heading = abs(math.degrees(math.remainder(final[2] - anchor[2], math.tau)))
    bench.check(f"{preset}: reach / turn", f"{reach * 100:.0f} cm / {turn:.0f} deg", reach <= BASE_REACH_M)
    bench.check(
        f"{preset}: back on the anchor",
        f"{offset * 100:.1f} cm, {heading:.1f} deg",
        offset <= BASE_RETURN_M and heading <= BASE_RETURN_DEG,
    )
    twists = bench.probe.window("cmd_vel", start, settled)
    v = max((abs(x) for _, (x, _) in twists), default=0.0)
    w = max((abs(z) for _, (_, z) in twists), default=0.0)
    bench.check(
        f"{preset}: peak twist", f"{v:.2f} m/s, {w:.2f} rad/s", v <= BASE_SPEED_MAX[0] and w <= BASE_SPEED_MAX[1]
    )
    last = twists[-1][1] if twists else (0.0, 0.0)
    quiet = not any(t > settled - 1.0 for t, _ in twists)
    bench.check(
        f"{preset}: a zero twist on arrival, then silence",
        f"{len(twists)} messages, last {last}, quiet for the final 1 s: {quiet}",
        last == (0.0, 0.0) and quiet,
    )


def _arm_checks(bench: Bench, start: float, end: float) -> None:
    probe = bench.probe
    states = probe.window("arm_state", start, end)
    loads = np.array([load for _, (_, load) in states]) if states else np.zeros((1, 6))
    peak = loads.max(axis=0)
    bench.check("peak load j1..j5", " ".join(f"{p:.0f}%" for p in peak[:5]), bool(peak[:5].max() <= LOAD_ABORT_PCT))
    bench.check(
        "peak claw current", f"{peak[5] * J6_LIMIT_MA / 100:.0f} mA ({peak[5]:.0f}% of {J6_LIMIT_MA:.0f})", None
    )
    bad = [s for _, s in probe.window("arm_status", start, end) if not s[0]]
    bench.check("/mars/arm/status during the stage", "ok" if not bad else bad[0][1], not bad)
    commands = probe.window("arm_cmd", start, end)
    cap = Basis.load().max_speed[:6] / 30.0
    ratios = [
        np.abs(np.subtract(b, a)) / (cap * max((tb - ta) * 30, 1.0))
        for (ta, a), (tb, b) in pairwise(commands)
        if tb - ta < 0.1
    ]
    worst = float(np.max(ratios)) if ratios else 0.0
    bench.check(
        "arm step / (max_speed x ticks), worst joint",
        f"{worst:.2f} over {len(commands)} commands",
        worst <= STEP_RATIO_MAX,
    )
    gaps = np.diff([t for t, _ in commands])
    rate = 1.0 / float(np.median(gaps[gaps < 0.1])) if len(gaps) and (gaps < 0.1).any() else 0.0
    bench.check("arm stream rate", f"{rate:.1f} Hz", None)
    errors = [np.abs(np.subtract(m[0], q)) for t, q in commands if (m := _after(states, t + 0.15)) is not None]
    if errors:
        p95 = np.percentile(np.array(errors), 95, axis=0)
        bench.check("arm tracking p95 at +150 ms, j1..j6", " ".join(f"{e:.2f}" for e in p95) + " rad", None)
    ok, error, torque = _arm_status(bench)
    bench.check("torque sanity after the stage", f"ok={ok} torque={torque} '{error}'", ok and torque)


# ================= helpers =================


def _joints(msg: JointState) -> list[float] | None:
    positions = dict(zip(msg.name, msg.position, strict=False))
    names = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head")
    return [float(positions[n]) for n in names] if all(n in positions for n in names) else None


def _pose(msg: Odometry) -> tuple[float, float, float]:
    p = msg.pose.pose.position
    return p.x, p.y, quaternion_to_yaw(msg.pose.pose.orientation)


def _value(value: ParameterValue) -> Any:
    return {
        ParameterType.PARAMETER_BOOL: value.bool_value,
        ParameterType.PARAMETER_INTEGER: value.integer_value,
        ParameterType.PARAMETER_DOUBLE: value.double_value,
        ParameterType.PARAMETER_STRING: value.string_value,
        ParameterType.PARAMETER_STRING_ARRAY: list(value.string_array_value),
    }.get(value.type)


def _arm_status(bench: Bench) -> tuple[bool, str, bool]:
    """The newest /mars/arm/status (ok, error, torque); the arm node publishes it every 5 s."""
    bench.wait(
        lambda: time.monotonic() if bench.probe.latest("arm_status") else None,
        7.0,
        "no /mars/arm/status",
        abortable=False,
    )
    return _last(bench.probe, "arm_status")


def _last(probe: Probe, key: str) -> Any:
    sample = probe.latest(key)
    if sample is None:
        raise Abort(f"no {key} samples")
    return sample[1]


def _after(samples: list[Sample], t: float) -> Any:
    return next((v for ts, v in samples if ts >= t), None)


def _intervals(samples: list[Sample]) -> list[tuple[float, float]]:
    """The (on, off) runs of a boolean topic."""
    runs, on = [], None
    for t, playing in samples:
        if playing and on is None:
            on = t
        elif not playing and on is not None:
            runs.append((on, t))
            on = None
    return runs


def _max_rate(samples: list[Sample]) -> float:
    times = [t for t, _ in samples]
    return float(max((np.searchsorted(times, t + 1.0) - i for i, t in enumerate(times)), default=0))


def _tracking(commands: list[Sample], measured: list[Sample]) -> tuple[float, float]:
    """The lag that best lines the measured head up with its commands, and the p95 error there."""
    if len(commands) < 5 or len(measured) < 5:
        return math.inf, math.inf
    times, values = np.array([t for t, _ in measured]), np.array([v for _, v in measured])
    sent, wanted = np.array([t for t, _ in commands]), np.array([v for _, v in commands], dtype=float)
    best = min(
        (float(np.mean(np.abs(np.interp(sent + lag, times, values) - wanted))), lag)
        for lag in np.arange(0.0, 0.5, 0.02)
    )
    lag = best[1]
    return float(lag), float(np.percentile(np.abs(np.interp(sent + lag, times, values) - wanted), 95))


def _trace_rms(head: list[Sample], baseline: tuple[float, float], start: float, seconds: float) -> float:
    """RMS difference between the commanded head over one clip with speech and over the same clip without."""
    if seconds <= 0.5:
        return 0.0
    times, values = np.array([t for t, _ in head]), np.array([v for _, v in head], dtype=float)

    def held(t: np.ndarray) -> np.ndarray:
        return values[np.clip(np.searchsorted(times, t, side="right") - 1, 0, len(values) - 1)]

    grid = np.arange(0.3, seconds, 0.05)
    return float(np.sqrt(np.mean((held(start + grid) - held(baseline[0] + grid)) ** 2)))


def _ask(prompt: str, exact: str | None = None) -> bool | None:
    if not sys.stdin.isatty():
        return None
    answer = input(prompt).strip()
    return answer == exact if exact is not None else answer.lower() in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=("check", "head", "voice", "arm", "full"))
    parser.add_argument("--rounds", type=int, default=1, help="arm stage: rounds of presets (6 is about three minutes)")
    args = parser.parse_args()
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)  # Ctrl-C must leave the context up to restore
    probe = Probe()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(probe)
    threading.Thread(target=executor.spin, daemon=True).start()
    bench = Bench(probe, args.stage)
    try:
        time.sleep(1.5)  # discovery, and the latched agent status
        if args.stage == "check":
            check_stage(bench)
        else:
            cmd_vel_topic = probe.get_params(["cmd_vel_topic"])["cmd_vel_topic"]
            probe.listen_cmd_vel(str(cmd_vel_topic))
            _preconditions(bench)
            values: dict[str, Any] = {"expressive.enabled_parts": STAGE_PARTS[args.stage]}
            if args.stage == "voice":
                values["expressive.vocalize"] = True
            bench.configure(values)
            if args.stage == "head":
                head_stage(bench)
            elif args.stage == "voice":
                voice_stage(bench)
            elif args.stage == "arm":
                arm_stage(bench, args.rounds)
            else:
                full_stage(bench)
    except Abort as error:
        bench.check("stage ran to the end", str(error), False)
    except KeyboardInterrupt:
        bench.check("stage ran to the end", "stopped by the operator (Ctrl-C)", False)
    finally:
        passed = bench.finish()
        executor.shutdown(timeout_sec=1.0)
        probe.destroy_node()
        rclpy.try_shutdown()
    return 0 if passed else 1


def _preconditions(bench: Bench) -> None:
    agent = bench.probe.latest("agent")
    if agent is not None and agent[1].get("brain_active"):
        raise Abort("the agent is running: stop it on the Agent page first")
    bench.wait(
        lambda: time.monotonic() if bench.probe.latest("state") else None,
        3.0,
        "no /brain/express/state: is expressive.enabled on?",
    )
    held = _last(bench.probe, "state").get("masked_parts", [])
    if held:
        raise Abort(f"a skill holds {held}: wait for it to finish")


if __name__ == "__main__":
    sys.exit(main())
