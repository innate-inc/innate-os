#!/usr/bin/env python3
"""Query Google Calendar via Zapier's MCP (Streamable HTTP) using fastmcp.

The ``ZAPIER_MCP_API_KEY`` / ``MCP_ZAPIER_API_KEY`` value is your Zapier MCP bearer token:
the same key works for Calendar and for any other Zapier MCP tools exposed on that
connection (not a calendar-only secret).

Robot / deployment:
  - Dependency: ``fastmcp`` is listed in ``ros2_ws/pip-requirements.txt``. The usual robot
    update path (``scripts/update/post_update.sh``) installs that file with ``pip3 install -r``.
  - Manual install (same package): ``pip3 install fastmcp`` or ``uv pip install fastmcp``.
  - Secrets: set ``ZAPIER_MCP_API_KEY`` or ``MCP_ZAPIER_API_KEY`` for the skills/brain process
    (not committed to the repo).
"""

import asyncio
import json
import os
from typing import Any

from brain_client.skill_types import Skill, SkillResult

_DEFAULT_MCP_URL = "https://mcp.zapier.com/api/v1/connect"


def _mcp_result_text(result: Any) -> str:
    chunks: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(text)
    return "\n".join(chunks) if chunks else str(result)


def _pretty_json_if_possible(text: str) -> str:
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return text


class CheckCalendar(Skill):
    """
    Fetches events or busy periods from Google Calendar through Zapier MCP.
    """

    def __init__(self, logger):
        super().__init__(logger)
        self._api_key = os.environ.get("ZAPIER_MCP_API_KEY") or os.environ.get("MCP_ZAPIER_API_KEY", "")
        self._server_url = os.environ.get("ZAPIER_MCP_URL", _DEFAULT_MCP_URL)

    @property
    def name(self):
        return "check_calendar"

    def guidelines(self):
        return (
            "Use to read the user's Google Calendar for a given time range. "
            "Provide start_time and end_time as ISO 8601 in UTC with a Z suffix (e.g. 2026-04-12T14:30:00Z). "
            "For upcoming meetings or what's next, use start_time = current time (UTC), not midnight, "
            "and end_time = now plus several days (e.g. 7 days) so nothing is missed. "
            "For 'today' in a specific timezone, convert that local day's start/end to UTC—do not assume "
            "midnight–end-of-day in UTC equals the user's calendar day. "
            "Use calendar_id 'primary' unless a specific calendar is required. "
            "Set mode to 'events' to list events (default) or 'busy' for busy blocks only. "
            "Uses the shared Zapier MCP API key (same token as other Zapier MCP skills) and fastmcp on the robot."
        )

    def cancel(self):
        self.logger.info(
            "\033[91m[BrainClient] Calendar query cannot be canceled once started\033[0m"
        )
        return "Calendar query is atomic and cannot be canceled once started"

    async def _call_zapier_calendar(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        transport = StreamableHttpTransport(
            self._server_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        client = Client(transport=transport)
        async with client:
            result = await client.call_tool(tool_name, arguments)
        return _pretty_json_if_possible(_mcp_result_text(result))

    def execute(
        self,
        start_time: str,
        end_time: str,
        calendar_id: str = "primary",
        mode: str = "events",
    ):
        if not self._api_key.strip():
            return (
                "Missing Zapier MCP API key: set ZAPIER_MCP_API_KEY or MCP_ZAPIER_API_KEY "
                "(one bearer token for all Zapier MCP tools on this connection).",
                SkillResult.FAILURE,
            )

        m = mode.lower().strip()
        if m == "events":
            tool_name = "google_calendar_find_events"
        elif m in ("busy", "busy_periods"):
            tool_name = "google_calendar_find_busy_periods_in_calendar"
        else:
            return (
                f"Invalid mode '{mode}': use 'events' or 'busy'.",
                SkillResult.FAILURE,
            )

        self.logger.info(
            f"\033[96m[BrainClient] check_calendar ({tool_name}) "
            f"{start_time} .. {end_time} calendar={calendar_id}\033[0m"
        )

        base_args: dict[str, Any] = {
            "instructions": (
                f"Execute the Google Calendar tool '{tool_name}' with the provided parameters."
            ),
            "calendarid": calendar_id,
            "start_time": start_time,
            "end_time": end_time,
        }

        try:
            message = asyncio.run(self._call_zapier_calendar(tool_name, base_args))
        except ModuleNotFoundError as e:
            err = (
                "fastmcp is not installed. Install with: "
                f"pip install fastmcp ({e})"
            )
            self.logger.error(err)
            return err, SkillResult.FAILURE
        except Exception as e:
            err = f"Calendar MCP request failed: {e}"
            self.logger.error(err)
            return err, SkillResult.FAILURE

        self.logger.info("\033[92m[BrainClient] check_calendar completed\033[0m")
        return message, SkillResult.SUCCESS
