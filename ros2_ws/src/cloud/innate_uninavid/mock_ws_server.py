#!/usr/bin/env python3
"""Mock websocket server: broadcasts left / right / stop in a loop."""
import asyncio
import itertools
import websockets

HOST = "localhost"
PORT = 9001
COMMANDS = ["left", "right", "stop"]
INTERVAL = 1.5  # seconds between commands


async def handler(ws):
    print(f"[mock-ws] client connected: {ws.remote_address}")
    for cmd in itertools.cycle(COMMANDS):
        print(f"[mock-ws] sending: {cmd!r}")
        await ws.send(cmd)
        await asyncio.sleep(INTERVAL)


async def main():
    print(f"[mock-ws] listening on ws://{HOST}:{PORT}")
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
