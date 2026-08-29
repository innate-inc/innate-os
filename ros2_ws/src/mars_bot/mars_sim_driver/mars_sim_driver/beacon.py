# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""LAN discovery beacon for the simulator.

The controller app finds real robots over Bluetooth; a sim has none, and the
container sits on a Docker bridge that cannot broadcast to the LAN. So the
world server -- the one always-alive host process -- announces the sim: a
JSON beacon broadcast on BEACON_PORT every BEACON_INTERVAL_S, and a unicast
reply to any probe an app sends to the same port (the probe is also what
makes iOS ask for local-network permission, and it survives access points
that drop broadcasts).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

BEACON_PORT = 19090
BEACON_INTERVAL_S = 2.0
_BROADCAST = ("255.255.255.255", BEACON_PORT)


class SimBeacon:
    """Announce this sim; ``robot_info_path`` is read on every beacon so a
    rename from the app shows up without a restart."""

    def __init__(self, robot_info_path: Path | None, rosbridge_port: int, webapp_port: int, version: str):
        self.robot_info_path = robot_info_path
        self.rosbridge_port = rosbridge_port
        self.webapp_port = webapp_port
        self.version = version
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # several checkouts' sims share the port
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind(("", BEACON_PORT))
        self._sock.settimeout(BEACON_INTERVAL_S)

    @classmethod
    def from_env(cls) -> SimBeacon | None:
        """Configured by the launcher through the environment; None when it
        opted out (INNATE_SIM_BEACON=0) or never configured a port."""
        if os.environ.get("INNATE_SIM_BEACON", "1").strip() in ("0", "false", "no"):
            return None
        rosbridge = os.environ.get("INNATE_SIM_BEACON_ROSBRIDGE_PORT", "").strip()
        if not rosbridge.isdigit():
            return None
        info = os.environ.get("INNATE_SIM_BEACON_ROBOT_INFO", "").strip()
        return cls(
            Path(info) if info else None,
            int(rosbridge),
            int(os.environ.get("INNATE_SIM_BEACON_WEBAPP_PORT", "443") or 443),
            os.environ.get("INNATE_SIM_BEACON_VERSION", ""),
        )

    def start(self) -> None:
        threading.Thread(target=self._run, name="sim-beacon", daemon=True).start()
        print(f"[world-server] discovery beacon on udp/{BEACON_PORT} (rosbridge {self.rosbridge_port})", flush=True)

    def payload(self) -> bytes:
        return json.dumps(
            {
                "innate_sim": True,
                "name": self._robot_name(),
                "rosbridge_port": self.rosbridge_port,
                "webapp_port": self.webapp_port,
                "version": self.version,
            }
        ).encode()

    def _robot_name(self) -> str:
        if self.robot_info_path is None:
            return "Simulator"
        try:
            name = json.loads(self.robot_info_path.read_text(encoding="utf-8")).get("robot_name")
        except (OSError, ValueError, AttributeError):
            return "Simulator"
        return f"{name} (sim)" if name else "Simulator"

    def _run(self) -> None:
        next_broadcast = 0.0
        while True:
            now = time.monotonic()
            if now >= next_broadcast:
                self._send(_BROADCAST)
                next_broadcast = now + BEACON_INTERVAL_S
            try:
                data, sender = self._sock.recvfrom(512)
            except TimeoutError:
                continue
            except OSError:
                return
            if b"innate_sim_probe" in data:
                self._send(sender)
                # The app may probe from an ephemeral port; it says where it listens.
                listen_port = self._probe_port(data)
                if listen_port is not None and listen_port != sender[1]:
                    self._send((sender[0], listen_port))

    @staticmethod
    def _probe_port(data: bytes) -> int | None:
        try:
            port = json.loads(data).get("port")
        except (ValueError, AttributeError):
            return None
        return port if isinstance(port, int) and 0 < port < 65536 else None

    def _send(self, target: tuple[str, int]) -> None:
        try:
            self._sock.sendto(self.payload(), target)
        except OSError:
            pass  # no route on this interface right now; the next tick retries
