"""Client for the host world server (world_server.py).

Exposes the slice of VirtualMars' surface the driver node uses, over the
localhost RPC. Two connections: a "state" channel for the small, frequent
calls (pose/joints/lidar/commands) and a "render" channel so a 15ms render
never delays a 30Hz odom read. State reads are coalesced: /odom,
/joint_states, /mars/arm/state and the head publisher all tick within a few
ms of each other, so one `state` RPC serves them all via a short-lived cache.
"""

import json
import math
import socket
import struct
import threading
import time

import numpy as np

STATE_CACHE_S = 0.015  # coalesce the 30/30/20/10Hz state publishers into one RPC
STATE_STALE_LIMIT_S = 5.0  # ride out a server restart; a dead server must surface


class _Channel:
    """One socket to the server. Connects lazily and reconnects once per
    call on a dropped connection -- the server is load-bearing for the whole
    driver, so a restart must not permanently kill the node."""

    def __init__(self, host: str, port: int):
        self._addr = (host, port)
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def _connect(self) -> None:
        self._sock = socket.create_connection(self._addr, timeout=10.0)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _read_frame(self) -> bytes:
        header = b""
        while len(header) < 4:
            chunk = self._sock.recv(4 - len(header))
            if not chunk:
                raise ConnectionError("world server closed the connection")
            header += chunk
        (length,) = struct.unpack(">I", header)
        payload = bytearray()
        while len(payload) < length:
            chunk = self._sock.recv(min(1 << 16, length - len(payload)))
            if not chunk:
                raise ConnectionError("world server closed the connection")
            payload += chunk
        return bytes(payload)

    def _call_once(self, req: dict, timeout: float) -> tuple[dict, bytes | None]:
        if self._sock is None:
            self._connect()
        self._sock.settimeout(timeout)
        payload = json.dumps(req).encode()
        self._sock.sendall(struct.pack(">I", len(payload)) + payload)
        meta = json.loads(self._read_frame())
        blob = self._read_frame() if meta.get("blob") is not None else None
        return meta, blob

    def _reset(self) -> None:
        if self._sock is not None:
            self._sock.close()
        self._sock = None

    def call(self, req: dict, timeout: float = 10.0) -> tuple[dict, bytes | None]:
        with self._lock:
            try:
                meta, blob = self._call_once(req, timeout)
            except (OSError, ConnectionError):
                # Dropped/never-up connection: reconnect and retry once.
                self._reset()
                try:
                    meta, blob = self._call_once(req, timeout)
                except (OSError, ConnectionError):
                    # A failed exchange can leave the server's reply in flight;
                    # reusing the socket would pair the NEXT request with THIS
                    # reply (a depth call reading a jpeg reply, forever offset).
                    self._reset()
                    raise
        if not meta.get("ok"):
            raise RuntimeError(f"world server error for {req.get('op')}: {meta.get('error')}")
        return meta, blob


class RemoteWorld:
    """VirtualMars-shaped proxy; see world_server.py for the other side."""

    def __init__(self, host: str, port: int):
        self._state_ch = _Channel(host, port)
        self._render_ch = _Channel(host, port)
        self._state: dict | None = None
        self._state_at = 0.0
        self._state_lock = threading.Lock()

    def ping(self) -> None:
        self._state_ch.call({"op": "ping"}, timeout=3.0)

    def wait_ready(self, timeout: float = 60.0) -> None:
        """Block until the server answers a ping (it builds the MuJoCo model
        before listening, so first contact can take a while)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.ping()
                return
            except (OSError, ConnectionError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1.0)

    # --- coalesced state ---

    def _fresh_state(self) -> dict:
        with self._state_lock:
            now = time.monotonic()
            if self._state is None or now - self._state_at > STATE_CACHE_S:
                try:
                    self._state, _ = self._state_ch.call({"op": "state"})
                    self._state_at = now
                except (OSError, RuntimeError):
                    # Server briefly away (restart): a stale snapshot beats
                    # crashing the publishers -- but only briefly. A DEAD
                    # server (e.g. OOM-killed) must stop /odom, or every
                    # health check keeps reporting a frozen robot as 'ok'.
                    if self._state is None or now - self._state_at > STATE_STALE_LIMIT_S:
                        raise
            return self._state

    @property
    def time(self) -> float:
        return float(self._fresh_state()["time"])

    def pose(self) -> tuple[float, float, float]:
        return tuple(self._fresh_state()["pose"])

    def velocity(self) -> tuple[float, float, float]:
        return tuple(self._fresh_state()["vel"])

    def joint_positions(self) -> dict[str, float]:
        return dict(self._fresh_state()["joints"])

    def joint_targets(self) -> dict[str, float]:
        return dict(self._fresh_state()["targets"])

    # --- commands ---

    def set_cmd_vel(self, vx: float, wz: float) -> None:
        self._state_ch.call({"op": "cmd_vel", "vx": vx, "wz": wz})

    def set_joint_targets(self, targets: dict[str, float]) -> None:
        self._state_ch.call({"op": "joint_targets", "targets": targets})
        with self._state_lock:
            self._state_at = 0.0  # targets changed; drop the cached snapshot

    def set_joint_target(self, name: str, value: float) -> None:
        self.set_joint_targets({name: value})

    def head_pitch_deg(self) -> float:
        return math.degrees(self._fresh_state()["joints"].get("joint_head", 0.0))

    def reset(self) -> None:
        self._state_ch.call({"op": "reset"})
        with self._state_lock:
            self._state_at = 0.0

    # --- sensors ---

    def lidar_scan(self, n_rays: int, max_range: float) -> np.ndarray:
        _meta, blob = self._state_ch.call({"op": "lidar", "n_rays": n_rays, "max_range": max_range})
        return np.frombuffer(blob, dtype=np.float32)

    def render_jpeg(self, camera: str) -> bytes:
        _meta, blob = self._render_ch.call({"op": "render_jpeg", "camera": camera})
        if blob is None:
            raise RuntimeError("malformed render_jpeg reply (no blob)")
        return blob

    def render_depth(self, camera: str) -> np.ndarray:
        meta, blob = self._render_ch.call({"op": "render_depth", "camera": camera})
        if "shape" not in meta or blob is None:
            raise RuntimeError(f"malformed render_depth reply (keys: {sorted(meta)})")
        h, w = meta["shape"]
        return np.frombuffer(blob, dtype=np.float32).reshape(h, w)
