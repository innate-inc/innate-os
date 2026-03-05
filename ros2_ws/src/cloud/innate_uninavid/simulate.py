#!/usr/bin/env python3
"""
Mock websocket server for innate_uninavid.

- Receives binary frames from the node and saves them as JPEGs in ./mock_frames/
- Sends a looping cycle of action codes: LEFT → RIGHT → FORWARD → STOP
"""
import asyncio
import itertools
import os
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


asyncio.run(main())


