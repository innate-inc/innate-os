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
from collections.abc import Iterable
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "ros2_ws/src/mars_bot/mars_sim_driver"))

from engine import WATCHDOG_S, ArmWorld  # noqa: E402
from mars_sim_driver.world import default_urdf_path  # noqa: E402
from personal_profile import load_profile  # noqa: E402
from pose_study import StudyStore, catalogue  # noqa: E402
from websockets.asyncio.server import ServerConnection, serve  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.http11 import Request, Response  # noqa: E402
from websockets.typing import Origin  # noqa: E402


class ControlSession:
    def __init__(self, world: ArmWorld, poses: Iterable[dict[str, Any]] = ()) -> None:
        self.world = world
        self.owner = None
        self.token = None
        self.sequence = -1
        self.deadline = 0
        self.poses = {p["id"]: p for p in poses}
        self.mode = "control"
        self.study_pose_id = None

    def renew(self) -> None:
        # Encoding/review can stall a study tab briefly. This is ownership
        # only; the independent 350 ms motion watchdog still halts the arm.
        self.deadline = time.monotonic() + (10.0 if self.mode == "study" else 2.0)

    def stop(self, client: object, reason: str) -> None:
        if self.owner is client:
            self.world.hold(reason)
            self.owner = self.token = None

    def expire(self, now: float | None = None) -> None:
        if self.owner is not None and (time.monotonic() if now is None else now) > self.deadline:
            self.stop(self.owner, "input_timeout")

    def handle(self, client: object, message: object, wall_ms: float | None = None) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            raise ValueError("Expected a command object")
        op = message.get("op")
        if op == "begin":
            if self.owner is not None and self.owner is not client:
                raise ValueError("Another tab is controlling this simulation")
            self.world.hold("ready")
            self.mode = "study" if message.get("mode") == "study" else "control"
            self.study_pose_id = None
            self.owner, self.token, self.sequence = client, secrets.token_urlsafe(18), -1
            self.renew()
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
            self.renew()
            if op == "hold" and self.world.active:
                self.world.hold("tracking_lost")
            return None
        if op not in ("move", "study_pose"):
            raise ValueError("Unknown control command")
        sequence = message.get("sequence")
        if type(sequence) is not int or sequence <= self.sequence:
            raise ValueError("Old or invalid movement sequence")
        stamp = message.get("captured_at")
        now = time.time() * 1000 if wall_ms is None else wall_ms
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            raise ValueError("Invalid camera timestamp")
        if not -100 <= now - stamp <= 450:
            self.world.hold("stale_frame")
            self.sequence = sequence
            return {"type": "held", "reason": "stale_frame"}
        if op == "study_pose":
            pose_id = message.get("pose_id")
            if self.mode != "study" or pose_id not in self.poses:
                raise ValueError("Unknown pose or matching session not started")
            self.study_pose_id = pose_id
            self.world.show_pose(self.poses[pose_id])
        else:
            if self.mode == "study":
                raise ValueError("Hand control is paused during pose matching")
            self.world.move(
                message.get("horizontal"),
                message.get("vertical"),
                message.get("reach"),
                message.get("grip"),
                wrist=message.get("wrist"),
            )
        self.renew()
        self.sequence = sequence
        return None


class StatePublisher:
    """One pending snapshot per viewer; slow viewers cannot block physics."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.pending = asyncio.Queue(maxsize=1)

    def publish(self, payload: str) -> None:
        if self.pending.full():
            self.pending.get_nowait()
        self.pending.put_nowait(payload)

    async def run(self) -> None:
        try:
            while True:
                await asyncio.wait_for(self.ws.send(await self.pending.get()), timeout=2)
        except (ConnectionClosed, asyncio.TimeoutError):  # a distinct class before 3.11
            # Close a failed connection, rather than silently removing it from
            # broadcasting while leaving the browser's socket apparently open.
            await self.ws.close(code=1011, reason="State connection stalled")


async def main(port: int, study_dir: Path | None = None, profile_path: Path | None = None) -> None:
    profile = load_profile(profile_path or ROOT / "study-data/active-profile.json")
    # Catalogue generation must stay independent of the live calibration so
    # existing recordings retain the same catalogue hash after a restart.
    world = ArmWorld()
    poses = catalogue(world)
    floor_poses = catalogue(world, "floor")
    if profile:
        world = ArmWorld(profile["workspace"])
    control = ControlSession(world, [*poses, *floor_poses])
    study_root = study_dir or ROOT / "study-data"
    studies = {
        "general": StudyStore(study_root, poses),
        "floor": StudyStore(Path(study_root) / "floor", floor_poses),
    }
    study_lock = asyncio.Lock()
    clients = {}
    origin = f"http://127.0.0.1:{port}"

    async def request(connection: ServerConnection, req: Request) -> Response | None:
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
            directory = default_urdf_path().parent.parent
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

    async def client(ws: ServerConnection) -> None:
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
                        "personal_profile": profile,
                    }
                )
            )
            async for raw in ws:
                message = None
                try:
                    message = json.loads(raw)
                    if isinstance(message, dict) and message.get("op") == "study_open":
                        study = studies.get(message.get("study", "general"))
                        if study is None:
                            raise ValueError("Unknown pose exercise")
                        async with study_lock:
                            manifest = await asyncio.to_thread(study.open, message.get("session"))
                        answer = {
                            "type": "study_opened",
                            "session": manifest["session"],
                            "matches": manifest["matches"],
                            "poses": list(study.poses.values()),
                        }
                    elif isinstance(message, dict) and message.get("op") == "study_save":
                        study = studies.get(message.get("study", "general"))
                        if study is None:
                            raise ValueError("Unknown pose exercise")
                        if (
                            control.owner is not ws
                            or message.get("token") != control.token
                            or control.mode != "study"
                            or message.get("pose_id") != control.study_pose_id
                        ):
                            raise ValueError("Reconnect to this pose before saving your match")
                        async with study_lock:
                            saved = await asyncio.to_thread(study.save, message)
                        answer = {"type": "study_saved", **saved}
                    else:
                        answer = control.handle(ws, message)
                except (ValueError, TypeError, KeyError, OSError) as error:
                    if not isinstance(message, dict) or message.get("op") not in ("study_open", "study_save"):
                        control.stop(ws, "invalid_input")
                    answer = {"type": "error", "message": str(error)}
                if answer:
                    if isinstance(message, dict) and "request" in message:
                        answer["request"] = message["request"]
                    await ws.send(json.dumps(answer))
        except ConnectionClosed:
            pass
        finally:
            control.stop(ws, "disconnected")
            clients.pop(ws, None)
            publishing.cancel()
            await asyncio.gather(publishing, return_exceptions=True)

    allowed_origins = [Origin(origin), Origin(f"http://localhost:{port}"), Origin("http://127.0.0.1:8842")]
    async with serve(
        client,
        "127.0.0.1",
        port,
        process_request=request,
        origins=allowed_origins,
        max_size=12_000_000,
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
    parser.add_argument("--study-dir", type=Path, help="Local pose-study output directory")
    parser.add_argument(
        "--profile", type=Path, help="Calibration JSON; pass a missing path to use the default controller"
    )
    args = parser.parse_args()
    if not (ROOT / "dist/index.html").is_file():
        raise SystemExit("Build the studio first: cd sim/hand_control && npm install && npm run build")
    try:
        asyncio.run(main(args.port, args.study_dir, args.profile))
    except KeyboardInterrupt:
        print("Hand control studio stopped.")
