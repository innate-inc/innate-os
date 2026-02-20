"""HTTP client wrapper for the Training Orchestrator REST API."""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Any, Generator

import requests

from auth_client import AuthProvider
from .types import ClientConfig, SkillInfo, RunInfo

logger = logging.getLogger(__name__)

_UPLOAD_CHUNK = 1024 * 1024  # 1 MB — callback fires once per chunk


class APIError(Exception):
    """Raised when the orchestrator returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class OrchestratorClient:
    """Thin HTTP wrapper around the orchestrator REST API.

    When ``config.auth_issuer_url`` is set the ``auth_token`` is treated as
    a service key, exchanged for a JWT via OIDC, and automatically renewed
    whenever the server responds with *401 Unauthorized* +
    ``WWW-Authenticate: Bearer error="invalid_token"`` (per RFC 6750).
    """

    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._base = config.server_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"

        # Set up auth: either OIDC (service-key → JWT) or plain bearer token
        if config.auth_issuer_url:
            self._auth = AuthProvider(
                issuer_url=config.auth_issuer_url,
                service_key=config.auth_token,
            )
            self._update_auth_header()
        else:
            self._auth = None
            self._session.headers["Authorization"] = f"Bearer {config.auth_token}"

    def _update_auth_header(self) -> None:
        """Refresh the session Authorization header from the AuthProvider."""
        if self._auth is not None:
            self._session.headers["Authorization"] = f"Bearer {self._auth.token}"

    @staticmethod
    def _is_token_expired(resp: requests.Response) -> bool:
        """Check for RFC 6750 invalid_token on a 401 response."""
        if resp.status_code != 401:
            return False
        www_auth = resp.headers.get("WWW-Authenticate", "")
        return "invalid_token" in www_auth

    # ── Skills ──────────────────────────────────────────────────────

    def create_skill(
        self,
        name: str,
    ) -> SkillInfo:
        """POST /skills — create a new skill."""
        body: dict[str, Any] = {"name": name}
        resp = self._post("/skills", json=body)
        return SkillInfo.from_api(resp)

    def get_skill(self, skill_id: str) -> SkillInfo:
        """GET /skills/{skill_id} — full skill details."""
        data = self._get(f"/skills/{skill_id}")
        return SkillInfo.from_api(data)

    def list_skills(self) -> list[SkillInfo]:
        """GET /skills — all skills for the authenticated user."""
        data = self._get("/skills")
        return [SkillInfo.from_api(s) for s in data]

    def request_file_urls(
        self,
        skill_id: str,
        filenames: list[str],
    ) -> dict[str, Any]:
        """
        POST /skills/{skill_id}/files — get signed upload+download URLs.

        Returns ``{"upload_urls": {name: url}, "download_urls": {name: url}}``.
        """
        return self._post(
            f"/skills/{skill_id}/files",
            json={"filenames": filenames},
        )

    # ── Runs ────────────────────────────────────────────────────────

    def create_run(
        self,
        skill_id: str,
        training_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        POST /skills/{skill_id}/runs — create a new run.

        Returns the run dict including ``agent_secret`` (shown only once).
        """
        body: dict[str, Any] = {}
        if training_params:
            body["training_params"] = training_params
        return self._post(f"/skills/{skill_id}/runs", json=body)

    def get_run(self, skill_id: str, run_id: int) -> RunInfo:
        """GET /skills/{skill_id}/runs/{run_id} — full run details."""
        data = self._get(f"/skills/{skill_id}/runs/{run_id}")
        return RunInfo.from_api(data)

    def list_runs(self, skill_id: str) -> list[RunInfo]:
        """GET /skills/{skill_id}/runs — all runs for a skill."""
        data = self._get(f"/skills/{skill_id}/runs")
        return [RunInfo.from_api(r) for r in data]

    def update_run(
        self,
        skill_id: str,
        run_id: int,
        *,
        status: str,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """PUT /skills/{skill_id}/runs/{run_id} — transition run status."""
        body: dict[str, Any] = {"status": status}
        if error_message is not None:
            body["error_message"] = error_message
        return self._put(f"/skills/{skill_id}/runs/{run_id}", json=body)

    # ── Storage ─────────────────────────────────────────────────────

    def list_skill_files(self, skill_id: str) -> dict[str, str]:
        """
        GET /storage/list/{skill_id}

        Returns ``{filename: signed_download_url, ...}`` for skill data files.
        """
        data = self._get(f"/storage/list/{skill_id}")
        return data.get("files", {})

    def list_run_files(self, skill_id: str, run_id: int) -> dict[str, str]:
        """
        GET /storage/list/{skill_id}/{run_id}

        Returns ``{filename: signed_download_url, ...}`` for run output files.
        """
        data = self._get(f"/storage/list/{skill_id}/{run_id}")
        return data.get("files", {})

    # ── Presigned URL helpers ───────────────────────────────────────

    def upload_to_signed_url(
        self,
        signed_url: str,
        file_path: str,
        content_type: str = "application/octet-stream",
    ) -> Generator[tuple[str, int, int], None, None]:
        """HTTP PUT a file to a presigned GCS URL.

        Yields ``(filename, bytes_sent, bytes_total)`` as the upload
        streams data.  The caller **must** fully consume the generator
        to complete the upload.
        """
        file_size = os.path.getsize(file_path)
        name = os.path.basename(file_path)

        q: queue.Queue[int | BaseException] = queue.Queue()

        class _ProgressFile:
            def __init__(self) -> None:
                self.file = open(file_path, "rb")
                self.bytes_read: int = 0

            def read(self, size: int = -1) -> bytes:
                chunk = self.file.read(size)
                if chunk:
                    self.bytes_read += len(chunk)
                    q.put(self.bytes_read)
                return chunk

            def __len__(self) -> int:
                return file_size

        def _do_upload() -> None:
            try:
                resp = requests.put(
                    signed_url,
                    data=_ProgressFile(),
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(file_size),
                    },
                    timeout=self._config.upload_timeout_seconds,
                )
                if resp.status_code not in (200, 201):
                    q.put(
                        APIError(resp.status_code, f"Upload failed: {resp.text[:500]}")
                    )
                    return
            except BaseException as e:
                q.put(e)
                return
            q.put(file_size)

        t = threading.Thread(target=_do_upload, daemon=True)
        t.start()

        last_yielded = 0
        while True:
            item = q.get()
            if isinstance(item, BaseException):
                raise item
            if item >= file_size or item - last_yielded >= 5 * _UPLOAD_CHUNK:
                last_yielded = item
                yield name, item, file_size
            if item >= file_size:
                break

        t.join()

    def head_signed_url(self, signed_url: str) -> int | None:
        """
        HEAD request on a signed download URL.

        Returns Content-Length or None if the file doesn't exist.
        """
        try:
            resp = requests.head(
                signed_url, timeout=self._config.request_timeout_seconds
            )
            if resp.status_code == 200:
                cl = resp.headers.get("Content-Length")
                return int(cl) if cl else None
            return None
        except Exception as e:
            logger.warning("HEAD request failed for signed URL: %s", e)
            return None

    def download_signed_url(
        self,
        signed_url: str,
        dest_path: str,
    ) -> Generator[tuple[str, int, int], None, None]:
        """Download a file from a signed GCS URL.

        Yields ``(filename, bytes_received, bytes_total)`` after every
        ~1 MB.  *bytes_total* is 0 if the server omitted Content-Length.
        The caller **must** fully consume the generator to complete the
        download.
        """
        name = os.path.basename(dest_path)
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with requests.get(
            signed_url, stream=True, timeout=self._config.download_timeout_seconds
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            received = 0
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=_UPLOAD_CHUNK):
                    f.write(chunk)
                    received += len(chunk)
                    yield name, received, total

    # ── Internal HTTP helpers ───────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Send an authenticated request with automatic JWT renewal.

        If the server responds 401 with ``WWW-Authenticate: Bearer
        error="invalid_token"`` (RFC 6750) and we have an
        :class:`AuthProvider`, we renew the JWT and retry once.
        """
        url = f"{self._base}{path}"
        resp = self._session.request(
            method,
            url,
            params=params,
            json=json,
            timeout=self._config.request_timeout_seconds,
        )

        if self._auth is not None and self._is_token_expired(resp):
            logger.info("JWT expired — renewing and retrying %s %s", method, path)
            self._auth.token_needs_renewal = True
            self._update_auth_header()
            resp = self._session.request(
                method,
                url,
                params=params,
                json=json,
                timeout=self._config.request_timeout_seconds,
            )

        self._raise_for_status(resp)
        return resp.json()

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, json: dict[str, Any]) -> Any:
        return self._request("POST", path, json=json)

    def _put(self, path: str, json: dict[str, Any]) -> Any:
        return self._request("PUT", path, json=json)

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise APIError(resp.status_code, str(detail))
