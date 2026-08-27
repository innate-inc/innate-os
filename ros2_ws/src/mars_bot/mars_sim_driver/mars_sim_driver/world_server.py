"""The world server: VirtualMars behind two localhost interfaces (see
sim/README.md "world_server.py"):

- driver RPC (--port): sensing/actuation for the robot adapter (node.py).
  Framing: 4-byte big-endian length | JSON; responses are one JSON frame,
  then one binary frame iff the JSON says "blob": <nbytes>.
- observer state stream (--state-port): a WebSocket broadcasting ground
  truth ({t, wall, pose, joints, objects}) after every physics slice, and
  accepting stage commands ({"op": "drop_prop_at", ...}) back. Ground truth and
  scenery only -- robot software must never consume or drive it.

Always runs on the host (the launcher starts it via uv): in-container
software GL was slow enough to starve the whole ROS stack. No ROS; beyond
VirtualMars' deps only `websockets`, and without it the stream is disabled.
Binds 127.0.0.1 unless --bind says otherwise (see its help). macOS GL is
main-thread-sensitive, so all render work runs on the main thread; state
reads take the physics lock directly.
"""

import argparse
import contextlib
import json
import os
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

from .challenges import ChallengeChatBridge, ChallengeEngine, SkillEventBridge
from .core import CAMERA_HEIGHT, CAMERA_WIDTH, VirtualMars, encode_jpeg, release_freed_heap

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
        # Advertised in ping replies so the launcher can restart a reused
        # server whose listeners don't match the current bind policy (a
        # leftover INNATE_SIM_WORLD_BIND=0.0.0.0 server must not outlive the
        # run that asked for it).
        self.binds: list[str] | None = None
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
        # Challenge judge: evaluated on each published state, driven by
        # observer commands, fed skill events by SkillEventBridge (main()).
        self.challenges = ChallengeEngine(sim, self.lock)
        self._challenge_error_at = 0.0  # last throttled challenge-failure log

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
            objects = self.sim.object_poses()
            # Prop CENTRES for the judge (props.py center_offset): a distance
            # to the human has to mean its body, not the feet its origin sits
            # at. Gathered here because the judge runs without the sim.
            centers = self.sim.object_centers()
            sim_time = float(self.sim.data.time)
            # Under the same lock hold as the snapshot: names WHICH world these
            # numbers came from, so a challenge start landing between here and
            # tick() cannot get them judged against its fresh run.
            epoch = self.challenges.world_epoch
        # Judged outside the sim lock: pure evaluation over the gathered state.
        # Wrapped because this runs on the physics thread and physics_loop has
        # no guard of its own: a predicate bug, a hand-edited challenges.json,
        # anything at all in the challenge layer would otherwise kill that
        # thread and freeze the world for every viewer. The block goes missing
        # for a frame instead -- which is the module's own contract, that a
        # broken challenge degrades that challenge and never the sim.
        try:
            challenge = self.challenges.tick(sim_time, (x, y, yaw), centers, epoch)
        except Exception as exc:  # noqa: BLE001 -- degrade the challenge, never the sim
            challenge = None
            if time.time() - self._challenge_error_at > 5.0:  # 75Hz: do not flood the log
                self._challenge_error_at = time.time()
                print(f"[world-server] challenge tick failed: {exc!r}", flush=True)
        # t = sim clock (playback timeline); wall = shared clock for lag HUDs.
        payload = json.dumps(
            {
                "t": sim_time,
                "wall": time.time(),
                "pose": [x, y, yaw],
                "joints": joints,
                "objects": objects,
                "challenge": challenge,
            }
        )
        with self.state_cond:
            self.state_payload = payload
            self.state_seq += 1
            self.state_cond.notify_all()

    def _serve_scenario_commands(self, ws) -> None:
        """Read the observer socket for stage commands. This is the sim's own
        scenery, not robot control: the ops place props (see props.py) and take
        them away again, without a full reset."""
        try:
            for raw in ws:
                try:
                    self._run_scenario_command(json.loads(raw))
                except Exception as exc:  # noqa: BLE001 -- one bad command must not drop the connection
                    print(f"[world-server] ignoring stage command: {exc!r}", flush=True)
        except Exception:  # noqa: BLE001,S110 -- client gone, or junk on the wire
            pass

    def _run_scenario_command(self, cmd: dict) -> None:
        op = cmd.get("op")
        if op == "drop_prop_at":  # user picked the spot: release + settle
            name, x, y = str(cmd["name"]), float(cmd["x"]), float(cmd["y"])
            yaw = float(cmd.get("yaw", 0.0))
            with self.lock:
                ok = self.sim.drop_prop_at(name, x, y, yaw)
        elif op == "place_prop_at_robot":  # at rest, at the prop's own reach offset
            name = str(cmd["name"])
            with self.lock:
                ok = self.sim.place_prop_at_robot(name)
        elif op == "remove_prop":
            name = str(cmd["name"])
            with self.lock:
                ok = self.sim.remove_prop(name)
        elif op == "place_group":  # a whole set at once, each at its own offset
            with self.lock:
                self.sim.place_group(str(cmd.get("group", "manipulation")))
            ok = True
        elif op == "remove_all_props":  # the stage's "clear" chip
            with self.lock:
                self.sim.remove_all_props()
            ok = True
        elif op == "start_challenge":  # sets its own scene up; see challenges.py
            self.challenges.start(str(cmd.get("id", "")))
            self.publish_state()
            return
        elif op == "abort_challenge":
            self.challenges.abort()
            self.publish_state()
            return
        else:
            return
        if not ok:
            print(f"[world-server] {op} ignored: no prop {cmd.get('name')!r} in this world", flush=True)
        self.publish_state()

    def serve_state(self, ws) -> None:
        """One observer connection: push each new state, latest-wins (a slow
        client skips states instead of queueing lag), and accept the stage
        commands above on the way back."""
        threading.Thread(target=self._serve_scenario_commands, args=(ws,), daemon=True).start()
        # The prop roster (props.py sidecars) and the challenge roster
        # (challenges.py) never change while the server runs, so they go out
        # once per connection instead of riding every state broadcast. The
        # viewer builds its models and buttons from the props; the challenge
        # panel gets titles and briefs here and progress from the stream.
        with contextlib.suppress(Exception):  # noqa: BLE001 -- client gone before the first frame
            ws.send(json.dumps({"props": self.sim.prop_manifest(), "challenges": self.challenges.roster()}))
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
            return {"ok": True, "state_port": self.state_port, "binds": self.binds}, None
        if op == "state":
            with self.lock:
                x, y, yaw = self.sim.pose()
                vx, vy, wz = self.sim.velocity()
                # Encoder-side, not link-side: this is what the DRIVER
                # publishes on /joint_states, and real encoders sit before the
                # structural sag (see core.encoder_positions). The observer
                # stream below keeps ground truth for viewers.
                joints = self.sim.encoder_positions()
                targets = self.sim.joint_targets()
                sim_time = float(self.sim.data.time)
                world_epoch = self.sim.world_epoch
            return {
                "ok": True,
                "time": sim_time,
                "world_epoch": world_epoch,
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

    # Boot self-test: prove GL works before accepting clients, and report
    # backend + per-frame cost -- the launcher parses this line into the
    # startup health checks.
    t0 = time.perf_counter()
    server.sim.render_rgb("main")
    first_ms = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    frame = server.sim.render_rgb("main")
    steady_ms = (time.perf_counter() - t1) * 1000
    # A context can be created "successfully" yet render nothing (seen on a
    # Raspberry Pi: EGL came up with GL_OUT_OF_MEMORY warnings and produced
    # blank frames). A real render of the spawn view always has texture;
    # refuse to serve garbage so the launcher's ladder falls to the next
    # backend instead.
    if float(frame.std()) < 1.0:
        print(
            "[world-server] GL self-test produced a blank image -- the GL context is not actually "
            "rendering (GPU out of memory?). Refusing to serve broken frames.",
            flush=True,
        )
        raise SystemExit(1)
    backend = os.environ.get("MUJOCO_GL", "").strip() or "native"
    print(
        f"[world-server] GL self-test ({backend}): {steady_ms:.0f} ms/frame (first frame {first_ms:.0f} ms)",
        flush=True,
    )
    release_freed_heap()  # model-compile + GL-context scratch, ~1GB on glibc

    binds = [b.strip() for b in args.bind.split(",") if b.strip()]
    server.binds = binds
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
    SkillEventBridge(server.challenges)  # robot skill events for challenge goals (best-effort)
    ChallengeChatBridge(server.challenges)  # robot speech <-> environment replies (best-effort)

    def accept_loop(listener: socket.socket) -> None:
        while True:
            conn, _addr = listener.accept()
            threading.Thread(target=server.serve_connection, args=(conn,), daemon=True).start()

    for listener in listeners:
        threading.Thread(target=accept_loop, args=(listener,), daemon=True).start()
    server.render_loop()  # main thread owns the GL context


if __name__ == "__main__":
    main()
