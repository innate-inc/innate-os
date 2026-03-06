"""HTTP telemetry client for the robot-logger cloud service.

Sends robot telemetry (vitals, directives, chat) to the logging
endpoint.  Authentication is handled via :class:`auth_client.AuthProvider`
which exchanges an innate service key for a JWT via OIDC and
auto-renews on 401.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from auth_client import AuthProvider

logger = logging.getLogger(__name__)


class TelemetryClient:
    """Authenticated HTTP client for the robot-logger service."""

    def __init__(
        self,
        url: str,
        auth: AuthProvider,
        timeout: float = 5.0,
    ) -> None:
        self.base_url: str = url.rstrip("/")
        self._auth: AuthProvider = auth
        self.timeout: float = timeout
        self.enabled: bool = bool(self.base_url)

    def _post(self, endpoint: str, data: Dict[str, Any]) -> bool:
        """POST JSON to the telemetry service with auth header.

        Retries once on 401 after forcing a token renewal.
        """
        if not self.enabled:
            return False

        url = f"{self.base_url}{endpoint}"
        body = json.dumps(data).encode("utf-8")

        for attempt in range(2):
            try:
                req = Request(
                    url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._auth.token}",
                        "User-Agent": "innate-robot",
                    },
                    method="POST",
                )
                with urlopen(req, timeout=self.timeout) as resp:
                    if resp.status == 200:
                        return True
                    return False
            except HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._auth.token_needs_renewal = True
                    continue
                logger.error("Telemetry HTTP error: %d %s", e.code, e.reason)
            except URLError as e:
                logger.error("Telemetry connection error: %s", e.reason)
            except Exception as e:
                logger.error("Telemetry error: %s", e)
            break
        return False

    def log_vitals(self, vitals: Dict[str, Any]) -> bool:
        """Log all vitals in a single call."""
        return self._post("/log/vitals", vitals)

    def log_directive(self, directive: str) -> bool:
        """Log a directive change event."""
        return self._post("/log/directive", {"directive": directive})

    def log_chat(self, message: str) -> bool:
        """Log a chat message."""
        return self._post("/log/chat", {"message": message})
