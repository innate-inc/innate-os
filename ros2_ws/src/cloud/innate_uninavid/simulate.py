#!/usr/bin/env python3
"""
Mock websocket server for innate_uninavid.

- Receives binary frames from the node and saves them as JPEGs in ./mock_frames/
- Sends a looping cycle of action codes: LEFT → RIGHT → FORWARD → STOP

Run with ``--test`` to execute quick offline smoke tests instead.
"""
import asyncio
import itertools
import os
import sys
import time
import websockets

SAVE_DIR = os.path.join(os.path.dirname(__file__), "mock_frames")
os.makedirs(SAVE_DIR, exist_ok=True)

ACTION_LOOP = itertools.cycle([
    (2, "LEFT"),
    (3, "RIGHT"),
    (1, "FORWARD"),
    (0, "STOP"),
])
CMD_INTERVAL = 1  # seconds between commands


async def handler(ws):
    print(f"[mock-server] client connected: {ws.remote_address}")

    async def send_loop():
        for code, label in ACTION_LOOP:
            print(f"[mock-server] → {code} ({label})")
            await ws.send(str(code))
            await asyncio.sleep(CMD_INTERVAL)

    async def recv_loop():
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                # Split header\nimage
                nl = msg.find(b"\n")
                img_bytes = msg[nl + 1:] if nl != -1 else msg
                fname = os.path.join(SAVE_DIR, f"frame_{int(time.time() * 1000)}.jpg")
                with open(fname, "wb") as f:
                    f.write(img_bytes)
                print(f"[mock-server] ← saved {os.path.basename(fname)}  ({len(img_bytes)} bytes)")
            else:
                print(f"[mock-server] ← text: {msg!r}")

    await asyncio.gather(send_loop(), recv_loop())


async def main():
    host, port = "0.0.0.0", 9000
    print(f"[mock-server] listening on ws://{host}:{port}")
    print(f"[mock-server] frames → {SAVE_DIR}")
    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever


# ── Offline smoke tests ──────────────────────────────────────────────────────

def _run_tests():
    """Quick offline checks for _compute_cmd_vel."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from innate_uninavid.node import (
        _compute_cmd_vel,
        ACTION_STOP, ACTION_FORWARD, ACTION_LEFT, ACTION_RIGHT,
    )

    cases = [
        (str(ACTION_STOP),    0.0,  0.0),
        (str(ACTION_FORWARD), 0.3,  0.0),
        (str(ACTION_LEFT),    0.0,  0.8),
        (str(ACTION_RIGHT),   0.0, -0.8),
        (b"1",                0.3,  0.0),
        ("2\n",               0.0,  0.8),
    ]
    labels = {0: "STOP", 1: "FORWARD", 2: "LEFT", 3: "RIGHT"}
    for msg, exp_lx, exp_az in cases:
        t = _compute_cmd_vel(msg)
        raw = msg.decode() if isinstance(msg, bytes) else msg
        label = labels.get(int(raw.strip()), raw.strip())
        assert t is not None, f"{msg!r} returned None"
        assert abs(t.linear.x - exp_lx) < 1e-9, f"{label}: linear.x {t.linear.x} != {exp_lx}"
        assert abs(t.angular.z - exp_az) < 1e-9, f"{label}: angular.z {t.angular.z} != {exp_az}"
        print(f"  {label:<10}  linear.x={t.linear.x:+.1f}  angular.z={t.angular.z:+.1f}  OK")

    for bad in ["99", "nonsense", ""]:
        assert _compute_cmd_vel(bad) is None
    print("  unknown/empty → None  OK")
    print("\nAll checks passed.")


if "--test" in sys.argv:
    _run_tests()
else:
    asyncio.run(main())