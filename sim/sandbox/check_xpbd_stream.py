"""Exercise a dedicated running test server; never launches/stops any service."""

import argparse
import json
import socket
import struct
import time

from websockets.sync.client import connect


def rpc(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        raw = json.dumps(request).encode()
        conn.sendall(struct.pack(">I", len(raw)) + raw)

        def read(n):
            result = b""
            while len(result) < n:
                chunk = conn.recv(n - len(result))
                if not chunk:
                    raise EOFError("world server disconnected")
                result += chunk
            return result

        (length,) = struct.unpack(">I", read(4))
        reply = json.loads(read(length))
        if reply.get("blob"):
            (length,) = struct.unpack(">I", read(4))
            read(length)
        return reply


def run(port, state_port):
    assert rpc(port, {"op": "ping"})["cloth_backend"] == "xpbd"
    with connect(f"ws://127.0.0.1:{state_port}") as ws:
        ws.send(json.dumps({"op": "subscribe_deformables", "encoding": "idf1"}))
        ws.send(json.dumps({"op": "place_prop_at_robot", "name": "soft_sock"}))
        begin = time.perf_counter()
        frames, states, render_times = [], [], []
        next_render = begin
        while time.perf_counter() - begin < 6:
            msg = ws.recv(timeout=10)
            if isinstance(msg, bytes):
                magic, _, stamp, count, _ = struct.unpack("<4sIdII", msg[:24])
                assert magic == b"IDF1" and count == 133 and len(msg) == 24 + count * 12
                frames.append(stamp)
            else:
                state = json.loads(msg)
                if "t" in state:
                    states.append((time.perf_counter(), state["t"]))
            if time.perf_counter() >= next_render:
                tick = time.perf_counter()
                assert rpc(port, {"op": "render_jpeg", "camera": "main"})["ok"]
                render_times.append(time.perf_counter() - tick)
                next_render = tick + 0.125
        assert len(frames) > 30
        ws.send(json.dumps({"op": "remove_prop", "name": "soft_sock"}))
        while True:
            msg = ws.recv(timeout=10)
            if isinstance(msg, str):
                state = json.loads(msg)
                if "objects" in state and "soft_sock" not in state["objects"]:
                    break
        reset = rpc(port, {"op": "reset"})
        assert reset["ok"]
        ws.send(json.dumps({"op": "place_prop_at_robot", "name": "soft_sock"}))
        while not isinstance(ws.recv(timeout=10), bytes):
            pass
        ws.send(json.dumps({"op": "remove_prop", "name": "soft_sock"}))
    wall = states[-1][0] - states[0][0]
    sim = states[-1][1] - states[0][1]
    print(
        json.dumps(
            dict(
                wall_seconds=wall,
                sim_seconds=sim,
                real_time_factor=sim / wall,
                deformable_frames=len(frames),
                camera_requests=len(render_times),
                lifecycle_passed=True,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-port", type=int, required=True)
    args = parser.parse_args()
    run(args.port, args.state_port)
