#!/usr/bin/env python3
"""
Telemetry logger for robot data.

Sends telemetry to a Cloud Run proxy service which writes to BigQuery.
No GCP credentials needed on the robot - just the API endpoint.
"""

import json
import logging
from typing import Dict, Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


class RobotTelemetryLogger:
    """Logger for sending robot telemetry to Cloud Run service."""

    def __init__(self, url: str, robot_id: Optional[str] = None):
        self.logger = logging.getLogger(__name__)
        self.base_url = url.rstrip("/")
        self.robot_id = robot_id
        self.timeout = 5.0  # seconds

        if not self.robot_id:
            self.logger.warning("robot_id not provided. Telemetry logging disabled.")
            self.enabled = False
        else:
            self.enabled = True
            self.logger.info(f"Telemetry logger initialized: {self.base_url} (robot: {self.robot_id})")

    def _post(self, endpoint: str, data: Dict[str, Any]) -> bool:
        """POST JSON data to the telemetry service."""
        if not self.enabled:
            return False

        url = f"{self.base_url}{endpoint}"
        data["robot_id"] = self.robot_id

        try:
            req = Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=self.timeout) as response:
                return response.status == 200
        except HTTPError as e:
            self.logger.error(f"Telemetry HTTP error: {e.code} {e.reason}")
        except URLError as e:
            self.logger.error(f"Telemetry connection error: {e.reason}")
        except Exception as e:
            self.logger.error(f"Telemetry error: {e}")
        return False

    def log_battery(self, voltage: float, percentage: float,
                    status: int = 0, health: int = 0):
        """Log battery state."""
        self._post("/log/battery", {
            "voltage": voltage,
            "percentage": percentage,
            "power_supply_status": status,
            "power_supply_health": health,
        })

    def log_diagnostics(self, status: int, message: str, hardware_id: str = ""):
        """Log diagnostics event."""
        self._post("/log/diagnostics", {
            "status": status,
            "message": message,
            "hardware_id": hardware_id,
        })

    def log_directive(self, directive: str):
        """Log directive change event."""
        self._post("/log/directive", {
            "directive": directive,
        })
