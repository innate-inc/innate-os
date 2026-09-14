#!/usr/bin/env python3
"""Serve the local hand-control studio and its dedicated MuJoCo simulator."""

import argparse
import asyncio
import json
import math
import mimetypes
import secrets
import sys
import time
from http import HTTPStatus
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "ros2_ws/src/mars_bot/mars_sim_driver"))

from engine import WATCHDOG_S, ArmWorld  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.http11 import Response  # noqa: E402


class ControlSession:
    def __init__(self, world):
        self.world = world
        self.owner = None
        self.token = None
        self.sequence = -1
        self.deadline = 0

    def stop(self, client, reason):
        if self.owner is client:
            self.world.hold(reason)
            self.owner = self.token = None

    def expire(self, now=None):
        if self.owner is not None and (time.monotonic() if now is None else now) > self.deadline:
            self.stop(self.owner, "input_timeout")

    def handle(self, client, message, wall_ms=None):
        if not isinstance(message, dict):
            raise ValueError("Expected a command object")
        op = message.get("op")
        if op == "begin":
            if self.owner is not None and self.owner is not client:
                raise ValueError("Another tab is controlling this simulation")
            self.world.hold("ready")
            self.owner, self.token, self.sequence = client, secrets.token_urlsafe(18), -1
            self.deadline = time.monotonic() + 2.0
            return {
                "type": "begun",
                "token": self.token,
                "request": message.get("request"),
                "offset": self.world.snapshot()["offset"],
            }
        if op == "stop":
            self.stop(client, "paused")
            return {"type": "stopped"}
        if self.owner is not client or message.get("token") != self.token:
            raise ValueError("Start following before sending movement")
        if op in ("hold", "keepalive"):
            # Ownership and motion freshness are independent. Keeping a tab's
            # session alive never renews the motion watchdog.
            self.deadline = time.monotonic() + 2.0
            if op == "hold" and self.world.active:
                self.world.hold("tracking_lost")
            return None
        if op != "move":
            raise ValueError("Unknown control command")
        sequence = message.get("sequence")
        if type(sequence) is not int or sequence <= self.sequence:
            raise ValueError("Old or invalid movement sequence")
        stamp = message.get("captured_at")
        now = time.time() * 1000 if wall_ms is None else wall_ms
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            raise ValueError("Invalid camera timestamp")
        if not -100 <= now - stamp <= 450:
            self.world.hold("stale_frame")
            self.sequence = sequence
            return {"type": "held", "reason": "stale_frame"}
        self.world.move(message.get("horizontal"), message.get("vertical"), message.get("reach"), message.get("grip"))
        self.deadline = time.monotonic() + 2.0
        self.sequence = sequence
        return None


class StatePublisher:
    """One pending snapshot per viewer; slow viewers cannot block physics."""

    def __init__(self, ws):
        self.ws = ws
        self.pending = asyncio.Queue(maxsize=1)

    def publish(self, payload):
        if self.pending.full():
            self.pending.get_nowait()
        self.pending.put_nowait(payload)

    async def run(self):
        try:
            while True:
                await asyncio.wait_for(self.ws.send(await self.pending.get()), timeout=2)
        except (ConnectionClosed, TimeoutError):
            # Close a failed connection, rather than silently removing it from
            # broadcasting while leaving the browser's socket apparently open.
            await self.ws.close(code=1011, reason="State connection stalled")


async def main(port):
    world = ArmWorld()
    control = ControlSession(world)
    clients = {}
    origin = f"http://127.0.0.1:{port}"

    async def request(connection, req):
        # This service cannot connect to hardware. Keep its controller and
        # local assets restricted to the local studio origin as well.
        if req.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return connection.respond(HTTPStatus.FORBIDDEN, "Local studio only\n")
        path = unquote(urlsplit(req.path).path)
        if path == "/control":
            return None
        if req.headers.get("Upgrade", "").lower() == "websocket":
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        if path.startswith("/robot/"):
            directory = REPO / "ros2_ws/src/mars_bot/mars_sim"
            relative = path.removeprefix("/robot/")
        else:
            directory = ROOT / "dist"
            relative = "index.html" if path == "/" else path.lstrip("/")
        file = (directory / relative).resolve()
        if not file.is_relative_to(directory.resolve()) or not file.is_file():
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        body = file.read_bytes()
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        headers = Headers(
            {
                "Content-Type": mime,
                "Content-Length": str(len(body)),
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
        )
        return Response(200, "OK", headers, body)

    async def client(ws):
        publisher = StatePublisher(ws)
        clients[ws] = publisher
        publishing = asyncio.create_task(publisher.run())
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "simulated": True,
                        "engine": "VirtualMars / MuJoCo",
                        "watchdog_ms": WATCHDOG_S * 1000,
                    }
                )
            )
            async for raw in ws:
                try:
                    answer = control.handle(ws, json.loads(raw))
                except (ValueError, TypeError, KeyError) as error:
                    control.stop(ws, "invalid_input")
                    answer = {"type": "error", "message": str(error)}
                if answer:
                    await ws.send(json.dumps(answer))
        except ConnectionClosed:
            pass
        finally:
            control.stop(ws, "disconnected")
            clients.pop(ws, None)
            publishing.cancel()
            await asyncio.gather(publishing, return_exceptions=True)

    allowed_origins = [origin, f"http://localhost:{port}", "http://127.0.0.1:8842"]
    async with serve(
        client,
        "127.0.0.1",
        port,
        process_request=request,
        origins=allowed_origins,
        max_size=4096,
        max_queue=2,
        ping_interval=5,
        ping_timeout=5,
    ):
        print(f"Hand control studio: {origin} · dedicated simulated MARS", flush=True)
        while True:
            start = time.monotonic()
            control.expire(start)
            world.tick(1 / 60)
            payload = json.dumps(world.snapshot(), allow_nan=False)
            for publisher in tuple(clients.values()):
                publisher.publish(payload)
            await asyncio.sleep(max(0, 1 / 60 - (time.monotonic() - start)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8840)
    args = parser.parse_args()
    if not (ROOT / "dist/index.html").is_file():
        raise SystemExit("Build the studio first: cd sim/hand_control && npm install && npm run build")
    try:
        asyncio.run(main(args.port))
    except KeyboardInterrupt:
        print("Hand control studio stopped.")
