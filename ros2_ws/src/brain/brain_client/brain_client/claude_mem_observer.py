#!/usr/bin/env python3
"""
Claude-Mem integration for innate-os.

Captures skill executions and agent decisions, posting them as observations
to the claude-mem worker API for persistent cross-session memory.

The observer is fire-and-forget: it never blocks skill execution, and
silently degrades if the claude-mem worker is unavailable.

Configuration via environment variables:
  CLAUDE_MEM_ENABLED=true|false   (default: true)
  CLAUDE_MEM_HOST=127.0.0.1      (default: 127.0.0.1)
  CLAUDE_MEM_PORT=37777           (default: 37777)
"""

import json
import os
import threading
import urllib.request
import urllib.error
import uuid
from typing import Optional


class ClaudeMemObserver:
    """Posts skill executions to claude-mem's worker API as observations."""

    def __init__(self, logger):
        self._logger = logger
        self._enabled = os.environ.get("CLAUDE_MEM_ENABLED", "true").lower() == "true"

        if not self._enabled:
            self._logger.info("[claude-mem] Observer disabled via CLAUDE_MEM_ENABLED=false")
            return

        host = os.environ.get("CLAUDE_MEM_HOST", "127.0.0.1")
        port = os.environ.get("CLAUDE_MEM_PORT", "37777")
        self._worker_url = f"http://{host}:{port}/api/sessions/observations"
        self._session_id = f"innate-os-{uuid.uuid4().hex[:12]}"
        self._cwd = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))

        # Track consecutive failures to avoid log spam
        self._consecutive_failures = 0
        self._max_logged_failures = 3

        self._logger.info(
            f"[claude-mem] Observer active — session={self._session_id}, "
            f"worker={self._worker_url}"
        )

    def on_skill_start(self, skill_type: str, inputs: dict):
        """Record the start of a skill execution. Non-blocking."""
        if not self._enabled:
            return
        self._post_async(
            tool_name=f"skill:{skill_type}",
            tool_input=inputs,
            tool_response={"status": "started"},
        )

    def on_skill_result(
        self,
        skill_type: str,
        inputs: dict,
        result_message: str,
        result_status: str,
        duration_seconds: float,
    ):
        """Record a completed skill execution with its result. Non-blocking."""
        if not self._enabled:
            return
        self._post_async(
            tool_name=f"skill:{skill_type}",
            tool_input=inputs,
            tool_response={
                "message": result_message,
                "status": result_status,
                "duration_seconds": round(duration_seconds, 3),
            },
        )

    def on_agent_thought(self, thought_text: str, agent_id: Optional[str] = None):
        """Record an agent's reasoning/thought. Non-blocking."""
        if not self._enabled:
            return
        self._post_async(
            tool_name="agent:thought",
            tool_input={"agent_id": agent_id or "unknown"},
            tool_response={"thought": thought_text},
        )

    def on_directive_change(self, directive_id: str, directive_name: str):
        """Record an agent directive change. Non-blocking."""
        if not self._enabled:
            return
        self._post_async(
            tool_name="agent:directive_change",
            tool_input={"directive_id": directive_id, "directive_name": directive_name},
            tool_response={"status": "activated"},
        )

    def _post_async(self, tool_name: str, tool_input: dict, tool_response: dict):
        """Fire-and-forget POST to claude-mem worker."""
        thread = threading.Thread(
            target=self._post,
            args=(tool_name, tool_input, tool_response),
            daemon=True,
        )
        thread.start()

    def _post(self, tool_name: str, tool_input: dict, tool_response: dict):
        try:
            body = json.dumps({
                "contentSessionId": self._session_id,
                "tool_name": tool_name,
                "tool_input": json.dumps(tool_input, default=str),
                "tool_response": json.dumps(tool_response, default=str),
                "cwd": self._cwd,
            }).encode("utf-8")

            req = urllib.request.Request(
                self._worker_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self._consecutive_failures = 0
                self._logger.debug(f"[claude-mem] Observation posted: {tool_name}")

        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures <= self._max_logged_failures:
                self._logger.debug(f"[claude-mem] Post failed (non-critical): {e}")
            elif self._consecutive_failures == self._max_logged_failures + 1:
                self._logger.debug(
                    "[claude-mem] Suppressing further failure logs until next success"
                )
