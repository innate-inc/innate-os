# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Environment packs: one directory per world the simulator can load, holding
a manifest.json that binds its MuJoCo geometry, Nav2 map, browser assets and
spawn pose. Tracked packs live in sim/environments; licensed ones the repo
must not ship go in sim/environments.local (gitignored)."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import world

DEFAULT_ENVIRONMENT_ID = "apartment"
MANIFEST_ROOTS = ("environments", "environments.local")


def manifest_path(environment_id: str) -> Path:
    sim = world.repo_root() / "sim"
    candidates = [sim / root / environment_id / "manifest.json" for root in MANIFEST_ROOTS]
    return next((path for path in candidates if path.is_file()), candidates[0])


def available_ids() -> list[str]:
    sim = world.repo_root() / "sim"
    return sorted({path.parent.name for root in MANIFEST_ROOTS for path in (sim / root).glob("*/manifest.json")})


@dataclass(frozen=True)
class Environment:
    id: str
    display_name: str
    collision_dir: Path
    visual_dir: Path
    map_name: str
    spawn: tuple[float, float, float]
    viewer: dict[str, str]

    @classmethod
    def load(cls, environment_id: str, assets_dir: Path | None = None) -> Environment:
        path = manifest_path(environment_id)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError(
                f"unknown environment {environment_id!r}; available: {', '.join(available_ids()) or 'none'}"
            ) from None
        assets = assets_dir or world.default_assets_dir()
        physics, navigation, spawn = manifest["physics"], manifest["navigation"], manifest["spawn"]
        pose = (float(spawn["x"]), float(spawn["y"]), float(spawn["yaw_degrees"]))
        if not all(math.isfinite(value) for value in pose):
            raise ValueError(f"{path}: spawn pose must be finite")
        return cls(
            id=environment_id,
            display_name=str(manifest.get("display_name", environment_id)),
            collision_dir=assets / physics["collision_dir"],
            visual_dir=assets / physics["visual_dir"],
            map_name=Path(navigation["map_yaml"]).name,
            spawn=pose,
            viewer={key: str(value) for key, value in manifest["viewer"].items()},
        )

    @classmethod
    def load_all(cls) -> list[Environment]:
        loaded = []
        for environment_id in available_ids():
            try:
                loaded.append(cls.load(environment_id))
            except (OSError, KeyError, TypeError, ValueError) as exc:  # one broken pack must not hide the rest
                print(f"[environments] skipping {environment_id!r}: {exc!r}", flush=True)
        return loaded

    def summary(self) -> dict:
        return {"id": self.id, "display_name": self.display_name}

    def public(self) -> dict:
        return {**self.summary(), "viewer": self.viewer, "spawn": list(self.spawn)}


class NavMapBridge:
    """Keeps Nav2 on the active environment's map: watches /nav/current_map
    over the stack's rosbridge and calls /nav/change_navigation_map whenever it disagrees
    with what the world server wants. Reconnects forever and never blocks the
    sim, like the challenge bridges."""

    TOPIC = "/nav/current_map"
    SERVICE = "/nav/change_navigation_map"
    RETRY_S = 20.0  # a map switch relocalizes; give it time before asking again
    SWITCH_RETRY_S = 3.0  # while a switch waits, "already in progress" replies are retried this often
    # The mode manager reports a map well before its stack is up, and a map
    # change landing mid-boot leaves Nav2 half-activated. The launcher seeds
    # the boot map, so this only guards a mismatch it could not prevent.
    BOOT_GRACE_S = 30.0

    def __init__(self, url: str, map_name: str):
        self.url = url
        self.wanted = map_name
        self.current: str | None = None
        self._requested_at = -math.inf
        self._first_report_at: float | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def switch_to(self, map_name: str, timeout_s: float) -> bool:
        """Ask for a map and wait for Nav2 to report it. True at once when no
        Nav2 has been seen yet (a world server running alone), so a switch
        never waits on a stack that is not there."""
        previous, self.wanted = self.wanted, map_name
        self._requested_at = -math.inf
        if self.current is None:
            return True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.current == map_name:
                return True
            if time.monotonic() - self._requested_at >= self.SWITCH_RETRY_S:
                self._requested_at = -math.inf
            time.sleep(0.25)
        self.wanted = previous  # the world stays as it was; so must the map it is reconciled to
        self._requested_at = -math.inf
        return False

    def _run(self) -> None:
        try:
            from websockets.sync.client import connect
        except ImportError:
            print("[nav-map] `websockets` client unavailable -- Nav2 map will not follow the environment", flush=True)
            return
        while True:
            try:
                with connect(self.url, open_timeout=5) as ws:
                    ws.send(json.dumps({"op": "subscribe", "topic": self.TOPIC, "type": "std_msgs/String"}))
                    self._first_report_at = None
                    self._reconcile(ws)
            except Exception:  # noqa: BLE001,S110 -- rosbridge down/restarting; retry
                pass
            time.sleep(5)

    def _reconcile(self, ws) -> None:
        while True:
            try:
                frame = json.loads(ws.recv(timeout=1.0))
            except TimeoutError:
                frame = {}
            if frame.get("topic") == self.TOPIC:
                self.current = str(frame["msg"]["data"])
                if self._first_report_at is None:
                    self._first_report_at = time.monotonic()
            elif frame.get("op") == "service_response":
                print(f"[nav-map] {self.SERVICE} -> {frame.get('values', frame)}", flush=True)
            if self.current in (None, self.wanted) or time.monotonic() - self._requested_at < self.RETRY_S:
                continue
            if self._first_report_at is None or time.monotonic() - self._first_report_at < self.BOOT_GRACE_S:
                continue
            self._requested_at = time.monotonic()
            ws.send(
                json.dumps(
                    {
                        "op": "call_service",
                        "id": f"nav-map-{int(self._requested_at)}",
                        "service": self.SERVICE,
                        "args": {"map_name": self.wanted},
                    }
                )
            )
