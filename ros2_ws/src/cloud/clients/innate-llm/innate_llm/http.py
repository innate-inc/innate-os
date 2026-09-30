# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The only thing in the library that touches a socket."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping

import httpx

from innate_llm.types import Json, LlmError

_DETAIL_BYTES = 200


class Http:
    """Blocking JSON and SSE calls against one base URL, one connection pool for the process."""

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | None = None,
        timeout: float = 90.0,
    ):
        self._client = httpx.Client(
            base_url=base_url, headers=dict(headers or {}), auth=auth, timeout=timeout, follow_redirects=True
        )

    def post_json(self, path: str, body: Json, *, timeout: float | None = None) -> Json:
        return self._request("POST", path, body, timeout)

    def delete(self, path: str, *, timeout: float | None = None) -> Json:
        return self._request("DELETE", path, None, timeout)

    def sse(self, path: str, body: Json, *, timeout: float | None = None) -> Iterator[str]:
        """The ``data:`` payloads of a server-sent event stream, ending at ``[DONE]`` or EOF.

        Parsed per ``data:`` line, never per blank line: the Innate proxy's
        relay drops the blank line ending each event and re-wraps ``event:``
        lines as ``data: event: …`` (innate-cloud openai.py), and every vendor
        puts its whole payload on one ``data:`` line anyway.
        """
        try:
            deadline = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout  # None means "no deadline" to httpx
            with self._client.stream("POST", path, json=body, timeout=deadline) as response:
                if response.status_code != 200:
                    raise LlmError.http(response.status_code, response.read()[:_DETAIL_BYTES].decode(errors="replace"))
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        return
                    if payload and not payload.startswith("event:"):
                        yield payload
        except httpx.HTTPError as error:
            raise LlmError.transport(f"{type(error).__name__}: {error}") from error

    def _request(self, method: str, path: str, body: Json | None, timeout: float | None) -> Json:
        deadline = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
        try:
            response = self._client.request(method, path, json=body, timeout=deadline)
        except httpx.HTTPError as error:
            raise LlmError.transport(f"{type(error).__name__}: {error}") from error
        if response.status_code != 200:
            raise LlmError.http(response.status_code, response.text[:_DETAIL_BYTES])
        return json.loads(response.content) if response.content else {}
