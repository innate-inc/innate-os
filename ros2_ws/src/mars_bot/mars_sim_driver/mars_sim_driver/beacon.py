# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""LAN discovery for the simulator.

The controller app finds real robots over Bluetooth; a sim has none, and the
container sits on a Docker bridge that multicast does not cross. So the world
server -- the one always-alive host process -- advertises the sim over
mDNS/DNS-SD: the one LAN discovery both phone platforms browse without a
special entitlement. The SRV port is rosbridge's, so a checkout on its own
port block, or several sims on one host, each resolve to the right socket.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import ifaddr
from zeroconf import IPVersion, ServiceInfo, Zeroconf

SERVICE_TYPE = "_innate._tcp.local."
REFRESH_S = 5.0
# Host-side ends of container networks: on the LAN they are unreachable.
_CONTAINER_ADAPTERS = ("docker", "br-", "veth", "bridge", "utun", "tun", "tap")


def lan_addresses() -> list[str]:
    return sorted(
        str(ip.ip)
        for adapter in ifaddr.get_adapters()
        if not adapter.name.startswith(_CONTAINER_ADAPTERS)
        for ip in adapter.ips
        if ip.is_IPv4 and not str(ip.ip).startswith(("127.", "169.254."))
    )


class SimBeacon:
    """Advertise this sim. The robot name and the host's addresses are
    re-read every REFRESH_S, so a rename from the app or a laptop that changed
    networks shows up without a restart."""

    def __init__(self, robot_info_path: Path | None, rosbridge_port: int, webapp_port: int, version: str):
        self.robot_info_path = robot_info_path
        self.rosbridge_port = rosbridge_port
        self.webapp_port = webapp_port
        self.version = version
        host = socket.gethostname().split(".")[0]
        self._instance = f"innate-sim-{host}-{rosbridge_port}"

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
        print(f"[world-server] advertising {SERVICE_TYPE} (rosbridge {self.rosbridge_port})", flush=True)

    def service_info(self, addresses: list[str]) -> ServiceInfo:
        return ServiceInfo(
            SERVICE_TYPE,
            f"{self._instance}.{SERVICE_TYPE}",
            port=self.rosbridge_port,
            # Its own host record: the machine's .local name belongs to the OS responder.
            server=f"{self._instance}.local.",
            parsed_addresses=addresses,
            properties={
                "sim": "1",
                "name": self._robot_name(),
                "webapp_port": str(self.webapp_port),
                "version": self.version,
            },
        )

    def _robot_name(self) -> str:
        if self.robot_info_path is None:
            return "Simulator"
        try:
            name = json.loads(self.robot_info_path.read_text(encoding="utf-8")).get("robot_name")
        except (OSError, ValueError, AttributeError):
            return "Simulator"
        return f"{name} (sim)" if name else "Simulator"

    def _run(self) -> None:
        announced: tuple[str, list[str]] | None = None
        zeroconf: Zeroconf | None = None
        while True:
            name, addresses = current = (self._robot_name(), lan_addresses())
            if current != announced:
                # Rebuilt, not updated: zeroconf's sockets are bound to the
                # addresses it started with, and those are what changed.
                if zeroconf is not None:
                    zeroconf.close()
                zeroconf = self._announce(addresses) if addresses else None
                announced = current if zeroconf is not None else None
            time.sleep(REFRESH_S)

    def _announce(self, addresses: list[str]) -> Zeroconf | None:
        try:
            zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            zeroconf.register_service(self.service_info(addresses), allow_name_change=True)
        except OSError as error:  # no usable interface right now; the next tick retries
            print(f"[world-server] discovery unavailable: {error}", flush=True)
            return None
        return zeroconf
