#!/usr/bin/env python3
"""
Telemetry logger for robot data.

Sends telemetry to a Cloud Run proxy service which writes to BigQuery.
No GCP credentials needed on the robot - just the API endpoint.
"""

import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RobotTelemetryLogger:
    """Logger for sending robot telemetry to Cloud Run service."""

    def __init__(self, url: str, robot_id: str | None = None):
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

    def _post(self, endpoint: str, data: dict[str, Any]) -> bool:
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

    def log_vitals(self, vitals: dict[str, Any]):
        """Log all vitals (battery, diagnostics, CPU, commit) in a single call."""
        self._post("/log/vitals", vitals)

    def log_directive(self, directive: str):
        """Log directive change event."""
        self._post(
            "/log/directive",
            {
                "directive": directive,
            },
        )
