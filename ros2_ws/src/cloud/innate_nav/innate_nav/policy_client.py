#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Client for the navigation policy service (innate-nav-inference).

The policy runs on a GPU server; the robot only speaks its protocol. This is
that protocol, implemented here so innate-os depends on a running service and
not on the service's source tree -- nothing here imports the inference repo,
and the only third-party needs are `websockets` and stdlib.

    POST   /v1/episodes            open one, get {episode_id, stream_path, ...}
    WS     {stream_path}           binary frames up, JSON plans down
    DELETE /v1/episodes/{id}       close it
    GET    /v1/model               what the loaded checkpoint expects
    GET    /healthz                liveness, unauthenticated

Two rules the wire depends on:

`stream_path` is a PATH. The server cannot know its own external address --
behind a proxy that address belongs to the proxy -- so the caller joins the
path to the base URL it dialed, which is also what carries https -> wss.

Frames are BINARY, `<4-byte BE header length><utf8 JSON header><jpeg>`. The
JPEG is the bulk of the uplink and base64 would add a third to it for nothing.
Plans come back as JSON text.

Frames are fire-and-forget: the robot pushes observations at its own rate and
never waits for a reply. The receive thread only ever replaces the current
plan, so the control loop reads whatever is there and never blocks.
"""

from __future__ import annotations

import json
import struct
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields

from websockets.sync.client import connect

# Keepalive is not optional over a real network: a half-open TCP through NAT or
# a proxy otherwise leaves the control loop tracking a plan that stopped coming.
PING_INTERVAL_S = 20.0
# Teardown must be bounded and small -- a caller cancelling an episode is
# holding a robot still while it runs. A DELETE that never lands costs nothing:
# the server reaps idle episodes.
CLOSE_TIMEOUT_S = 2.0
TEARDOWN_TIMEOUT_S = 2.0


def ws_url(base: str, path: str) -> str:
    """Join the base URL the client dialed with the server's stream path."""
    scheme, sep, rest = base.partition("://")
    if not sep:
        raise ValueError(f"server URL needs a scheme: {base!r}")
    mapped = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(scheme.lower())
    if mapped is None:
        raise ValueError(f"cannot open a WebSocket against {scheme}://")
    return f"{mapped}://{rest.rstrip('/')}/{path.lstrip('/')}"


@dataclass(frozen=True)
class Pose:
    """Robot pose in the odometry frame. REP-103: x forward, y left, yaw CCW."""

    x: float
    y: float
    yaw: float


@dataclass(frozen=True)
class Plan:
    """Waypoints are metres in the robot frame *at capture_pose*, not at
    arrival. Plans land a few hundred ms old, so a tracker must re-express them
    through the odometry delta since then (pursuit.reanchor); skipping that
    puts a systematic 0.1-0.3 m bias into every tracked path."""

    waypoints_m: list[list[float]]
    capture_pose: Pose
    capture_stamp: float
    max_reach_m: float
    stop: bool
    p_stop: float | None
    keyframes: int
    # Which observations the plan was made from: ordinals into the server's
    # keyframe list, and the robot's own stamps for the same frames. Only the
    # robot still has those frames, so this is what lets it show them.
    history_indices: list[int]
    history_stamps: list[float]
    seq: int
    compute_ms: float
    model: str

    @staticmethod
    def from_json(text: str) -> Plan:
        """Unknown fields are DROPPED rather than passed to the constructor: a
        server that grows a field must not kill this client's receive thread,
        which is exactly what a strict Plan(**d) did the first time one did."""
        d = json.loads(text)
        d["capture_pose"] = Pose(**d["capture_pose"])
        known = {f.name for f in fields(Plan)}
        missing = known - set(d)
        if missing:
            raise ValueError(f"plan is missing {sorted(missing)}; server too old?")
        return Plan(**{k: v for k, v in d.items() if k in known})


def encode_frame(jpeg: bytes, pose: Pose, stamp: float) -> bytes:
    head = json.dumps({"pose": asdict(pose), "stamp": stamp}).encode()
    return struct.pack(">I", len(head)) + head + jpeg


@dataclass(frozen=True)
class Episode:
    """What POST /v1/episodes decided: the resolved instruction (templated
    families are worded server-side) and the history mode the family implies."""

    episode_id: str
    stream_path: str
    instruction: str
    task_family: str
    history_mode: str
    token_budget: int


class PolicyClient:
    """One episode's connection. One producer thread for frames is assumed."""

    def __init__(self, server: str = "http://localhost:8900",
                 token: str | None = None, timeout_s: float = 10.0):
        self.server = server.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        self.episode: Episode | None = None
        self._ws = None
        self._plan: Plan | None = None
        self._lock = threading.Lock()
        self._closed = False

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(self, path: str, method: str = "GET", body: dict | None = None,
                 timeout_s: float | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.server}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s or self.timeout_s) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"{method} {path} -> {exc.code}: {exc.read().decode(errors='replace')}"
            ) from exc

    def model_info(self) -> dict:
        """Camera geometry, history depth and the trained token-budget grid, as
        the loaded checkpoint declares them."""
        return self._request("/v1/model")

    def health(self) -> dict:
        return self._request("/healthz")

    def start(self, instruction: str = "", task_family: str = "r2r",
              token_budget: int = 3072, category: str = "",
              goal_fwd_m: float | None = None,
              goal_left_m: float | None = None) -> Episode:
        body = {"instruction": instruction, "task_family": task_family,
                "token_budget": token_budget, "category": category,
                "goal_fwd_m": goal_fwd_m, "goal_left_m": goal_left_m}
        self.episode = Episode(**self._request("/v1/episodes", method="POST", body=body))
        self._ws = connect(
            ws_url(self.server, self.episode.stream_path),
            additional_headers=self._headers(), max_size=None,
            ping_interval=PING_INTERVAL_S, ping_timeout=PING_INTERVAL_S,
            close_timeout=CLOSE_TIMEOUT_S)
        threading.Thread(target=self._receive, daemon=True).start()
        return self.episode

    def close(self) -> None:
        """Bounded and best-effort. The flag goes first: a producer thread
        inside send() holds the connection mutex, and close() would otherwise
        wait on the network for it."""
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001 -- teardown must not raise at a caller
                pass
        if self.episode is not None:
            try:
                self._request(f"/v1/episodes/{self.episode.episode_id}",
                              method="DELETE", timeout_s=TEARDOWN_TIMEOUT_S)
            except (OSError, RuntimeError):
                pass  # server already gone; the reaper collects it

    def send_frame(self, jpeg: bytes, pose: Pose, stamp: float) -> None:
        """Push one observation. A no-op once closed rather than an error: the
        producer is a camera callback on another thread, and a frame racing
        teardown is the normal case."""
        if self._ws is None:
            raise RuntimeError("start() first")
        if self._closed:
            return
        self._ws.send(encode_frame(jpeg, pose, stamp))

    @property
    def plan(self) -> Plan | None:
        """The most recent plan, or None before the first arrives."""
        with self._lock:
            return self._plan

    def _receive(self) -> None:
        try:
            for message in self._ws:
                plan = Plan.from_json(message)
                with self._lock:
                    self._plan = plan
        except Exception:  # noqa: BLE001 -- a dropped socket ends the episode
            if not self._closed:
                raise
