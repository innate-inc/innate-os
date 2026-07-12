"""The world server: VirtualMars behind two localhost interfaces (see
sim/README.md "world_server.py"):

- driver RPC (--port): sensing/actuation for the robot adapter (node.py).
  Framing: 4-byte big-endian length | JSON; responses are one JSON frame,
  then one binary frame iff the JSON says "blob": <nbytes>.
- observer state stream (--state-port): a WebSocket broadcasting ground
  truth ({t, wall, pose, joints}) after every physics slice. View-only;
  robot software must never consume it.

Always runs on the host (the launcher starts it via uv): in-container
software GL was slow enough to starve the whole ROS stack. No ROS; beyond
VirtualMars' deps only `websockets`, and without it the stream is disabled.
Binds 127.0.0.1 unless --bind says otherwise (see its help). macOS GL is
main-thread-sensitive, so all render work runs on the main thread; state
reads take the physics lock directly.
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time

import numpy as np
from PIL import Image as PILImage

try:
    from websockets.sync.server import serve as ws_serve
except ImportError:  # view-only feature; the sim must not die without it
    ws_serve = None

from .core import CAMERA_HEIGHT, CAMERA_WIDTH, VirtualMars, encode_jpeg

# Depth renders at the pointcloud grid: identical published cloud, 16x less fill.
DEPTH_WH = (CAMERA_WIDTH // 4, CAMERA_HEIGHT // 4)


def _read_frame(conn: socket.socket) -> bytes | None:
    header = b""
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    (length,) = struct.unpack(">I", header)
    payload = bytearray()
    while len(payload) < length:
        chunk = conn.recv(min(1 << 16, length - len(payload)))
        if not chunk:
            return None
        payload += chunk
    return bytes(payload)


def _send_frame(conn: socket.socket, payload: bytes) -> None:
    conn.sendall(struct.pack(">I", len(payload)) + payload)


# A product stays active this long after its last request (unwatched = free).
PRODUCT_TTL_S = 3.0
PRODUCTS = ("jpeg:main", "jpeg:wrist", "depth:main")


class WorldServer:
    def __init__(self, sim: VirtualMars):
        self.sim = sim
        self.lock = threading.Lock()
        # Advertised in ping replies so the launcher can tell a current
        # server from a stale pre-stream one (which it must restart).
        self.state_port: int | None = None
        # Latest rendered frame per product; RPCs return the freshest frame
        # instead of rendering inline, so a GL stall degrades freshness,
        # never liveness.
        self.latest: dict[str, tuple[dict, bytes]] = {}
        self.requested_at: dict[str, float] = {}
        self.frame_ready = threading.Condition()
        # Demand pacing: render once per client request (~8Hz) instead of
        # free-running at GPU speed (half a core of waste that jitters the
        # whole machine).
        self.wanted: set[str] = set()
        self.render_demand = threading.Event()
        # Observer state stream: newest ground-truth snapshot + a seq,
        # broadcast to every connected WebSocket (see serve_state).
        self.state_payload = "{}"
        self.state_seq = 0
        self.state_cond = threading.Condition()

    # --- physics (side thread; MuJoCo stepping is pure CPU) ---

    # Longest step slice: bounds the lock hold and the sim-time jump between
    # published states, so a scheduling stall replays as smooth slices.
    MAX_SLICE_S = 0.025

    def physics_loop(self) -> None:
        start_wall = time.perf_counter()
        start_sim = self.sim.data.time
        while True:
            target = start_sim + (time.perf_counter() - start_wall)
            stepped = False
            with self.lock:
                behind = target - self.sim.data.time
                if behind > 0.5:
                    # Catching up a long stall would monopolize the lock; drop it.
                    print(f"[world-server] dropping {behind:.1f}s of physics backlog after a stall", flush=True)
                    start_wall = time.perf_counter()
                    start_sim = self.sim.data.time
                elif behind > 0:
                    self.sim.step(min(behind, self.MAX_SLICE_S))
                    stepped = True
                    behind -= self.MAX_SLICE_S
            if stepped:
                self.publish_state()
            time.sleep(0.001 if behind > self.MAX_SLICE_S else 0.01)  # fast while behind

    # --- observer state stream (ground truth for viewers, never for robot code) ---

    def publish_state(self) -> None:
        with self.lock:
            x, y, yaw = self.sim.pose()
            joints = self.sim.joint_positions()
            sim_time = float(self.sim.data.time)
        # t = sim clock (playback timeline); wall = shared clock for lag HUDs.
        payload = json.dumps({"t": sim_time, "wall": time.time(), "pose": [x, y, yaw], "joints": joints})
        with self.state_cond:
            self.state_payload = payload
            self.state_seq += 1
            self.state_cond.notify_all()

    def serve_state(self, ws) -> None:
        """One observer connection: push each new state, latest-wins (a slow
        client skips states instead of queueing lag)."""
        last_seq = -1
        try:
            while True:
                with self.state_cond:
                    self.state_cond.wait_for(lambda seen=last_seq: self.state_seq != seen)
                    payload, last_seq = self.state_payload, self.state_seq
                ws.send(payload)
        except Exception:  # noqa: BLE001,S110 -- client gone; the stream just ends
            pass

    # --- renders (main thread only: macOS GL is main-thread-sensitive) ---

    def _render_product(self, product: str) -> None:
        kind, camera = product.split(":")
        if kind == "jpeg":
            with self.lock:
                self.sim.update_camera(camera)
            rgb = self.sim.read_rgb()
            if rgb.shape[0] != CAMERA_HEIGHT:  # scaled render -> wire res
                rgb = np.asarray(PILImage.fromarray(rgb).resize((CAMERA_WIDTH, CAMERA_HEIGHT), PILImage.BILINEAR))
            frame = ({"ok": True}, encode_jpeg(rgb))
        else:  # depth
            with self.lock:
                self.sim.update_depth(camera)
            depth = self.sim.read_depth().astype(np.float32)
            frame = ({"ok": True, "shape": list(depth.shape), "dtype": "float32"}, depth.tobytes())
        with self.frame_ready:
            self.latest[product] = frame
            self.frame_ready.notify_all()

    def render_loop(self) -> None:
        """Demand-paced frame pump: one render per client request, plus a
        keep-warm heartbeat when no client watches."""
        while True:
            now = time.monotonic()
            active = [p for p in PRODUCTS if now - self.requested_at.get(p, -1e9) < PRODUCT_TTL_S]
            if not active:
                # macOS parks idle offscreen GL contexts and re-acquiring can
                # stall for minutes; a 5Hz heartbeat prevents the parking.
                # Only macOS needs it -- elsewhere it's 5 renders/s of waste.
                if sys.platform == "darwin":
                    try:
                        self._render_product("jpeg:main")
                    except Exception:  # noqa: BLE001,S110 -- heartbeat is best-effort
                        pass
                time.sleep(0.2)
                continue
            todo = [p for p in active if p in self.wanted]
            if not todo:
                self.render_demand.wait(timeout=0.2)
                self.render_demand.clear()
                continue
            for product in todo:
                self.wanted.discard(product)
                try:
                    self._render_product(product)
                except Exception as exc:  # noqa: BLE001 -- the pump must never die
                    with self.frame_ready:
                        self.latest[product] = ({"ok": False, "error": repr(exc)}, None)
                        self.frame_ready.notify_all()

    def render(self, camera: str, kind: str) -> tuple[dict, bytes | None]:
        """Return the freshest frame for this product (waits only for the
        very first one after activation) and queue the next render."""
        product = f"{kind}:{camera}"
        self.requested_at[product] = time.monotonic()
        self.wanted.add(product)
        self.render_demand.set()
        with self.frame_ready:
            if product not in self.latest:
                self.frame_ready.wait_for(lambda: product in self.latest, timeout=10.0)
            if product not in self.latest:
                return {"ok": False, "error": "render timeout (no frame produced)"}, None
            meta, blob = self.latest[product]
        return meta, blob

    # --- request handling (one thread per connection) ---

    def handle(self, req: dict) -> tuple[dict, bytes | None]:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "state_port": self.state_port}, None
        if op == "state":
            with self.lock:
                x, y, yaw = self.sim.pose()
                vx, vy, wz = self.sim.velocity()
                joints = self.sim.joint_positions()
                targets = self.sim.joint_targets()
                sim_time = float(self.sim.data.time)
            return {
                "ok": True,
                "time": sim_time,
                "pose": [x, y, yaw],
                "vel": [vx, vy, wz],
                "joints": joints,
                "targets": targets,
            }, None
        if op == "cmd_vel":
            with self.lock:
                self.sim.set_cmd_vel(float(req["vx"]), float(req["wz"]))
            return {"ok": True}, None
        if op == "joint_targets":
            with self.lock:
                for name, value in req["targets"].items():
                    self.sim.set_joint_target(name, float(value))
            return {"ok": True}, None
        if op == "reset":
            with self.lock:
                self.sim.reset()
            return {"ok": True}, None
        if op == "lidar":
            with self.lock:
                ranges = self.sim.lidar_scan(int(req["n_rays"]), float(req["max_range"]))
            blob = np.asarray(ranges, dtype=np.float32).tobytes()
            return {"ok": True, "dtype": "float32"}, blob
        if op == "render_jpeg":
            return self.render(str(req["camera"]), "jpeg")
        if op == "render_depth":
            return self.render(str(req["camera"]), "depth")
        if op == "shutdown":  # used by the launcher for a clean stop
            return {"ok": True}, None
        return {"ok": False, "error": f"unknown op {op!r}"}, None

    def serve_connection(self, conn: socket.socket) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while True:
                payload = _read_frame(conn)
                if payload is None:
                    return
                try:
                    meta, blob = self.handle(json.loads(payload))
                except Exception as exc:  # noqa: BLE001 -- bad request must not kill the server
                    meta, blob = {"ok": False, "error": repr(exc)}, None
                if blob is not None:
                    meta["blob"] = len(blob)
                _send_frame(conn, json.dumps(meta).encode())
                if blob is not None:
                    _send_frame(conn, blob)
        finally:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument(
        "--state-port",
        type=int,
        default=8800,
        help="Observer state-stream WebSocket port (the webapp proxy's /worldstate assumes this default)",
    )
    parser.add_argument(
        "--render-scale",
        type=int,
        default=1,
        help="Divide the RGB render resolution by N (software-GL mitigation; the wire stays 640x480)",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Comma-separated addresses to listen on. Docker Desktop reaches the host loopback via "
        "host.docker.internal, but a native Linux/WSL Docker engine resolves it to the bridge "
        "gateway -- there the launcher passes '127.0.0.1,<gateway>' (host-owned, not LAN-routable).",
    )
    args = parser.parse_args()

    print(f"[world-server] loading VirtualMars (render scale {args.render_scale})...", flush=True)
    render_wh = (CAMERA_WIDTH // args.render_scale, CAMERA_HEIGHT // args.render_scale)
    server = WorldServer(VirtualMars(render_wh=render_wh, depth_render_wh=DEPTH_WH))
    server.sim.step(0.5)  # settle from the spawn drop before clients look

    # Boot self-test: prove GL works before accepting clients, and report the
    # per-frame cost so placement decisions are visible in the log.
    t0 = time.perf_counter()
    server.sim.render_rgb("main")
    first_ms = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    server.sim.render_rgb("main")
    steady_ms = (time.perf_counter() - t1) * 1000
    print(f"[world-server] GL self-test: {steady_ms:.0f} ms/frame (first frame {first_ms:.0f} ms)", flush=True)

    binds = [b.strip() for b in args.bind.split(",") if b.strip()]
    listeners = [socket.create_server((bind, args.port)) for bind in binds]
    print(f"[world-server] ready on {', '.join(f'{b}:{args.port}' for b in binds)}", flush=True)

    server.publish_state()  # observers get a frame before the first physics slice
    if ws_serve is None:
        print("[world-server] `websockets` not installed -- observer state stream disabled", flush=True)
    else:
        for bind in binds:
            state_server = ws_serve(server.serve_state, bind, args.state_port)
            threading.Thread(target=state_server.serve_forever, daemon=True).start()
        server.state_port = args.state_port
        print(f"[world-server] observer state stream on port {args.state_port} ({', '.join(binds)})", flush=True)

    threading.Thread(target=server.physics_loop, daemon=True).start()

    def accept_loop(listener: socket.socket) -> None:
        while True:
            conn, _addr = listener.accept()
            threading.Thread(target=server.serve_connection, args=(conn,), daemon=True).start()

    for listener in listeners:
        threading.Thread(target=accept_loop, args=(listener,), daemon=True).start()
    server.render_loop()  # main thread owns the GL context


if __name__ == "__main__":
    main()
