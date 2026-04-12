"""
MiniMax client for agent_codegen.

Wraps the Anthropic SDK pointed at the MiniMax Anthropic-compatible endpoint.
Implements a two-round agentic tool loop:

  Round 1 — model calls the provided tool (tool_choice="any").
  Round 2 — library sends the tool_result so the model can acknowledge.

Falls back to regex extraction of a markdown ``python`` code block when the
model does not call the tool (should be rare with tool_choice="any").
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import anthropic

from agent_codegen.models import AgentCodegenError

_LOG = logging.getLogger(__name__)

MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
DEFAULT_MODEL = "MiniMax-M2.7"

_WRITE_AGENT_TOOL: dict[str, Any] = {
    "name": "write_agent_file",
    "description": (
        "Submit the complete Python source for the new agent. "
        "Call this exactly once with the full file content."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Snake-case agent identifier matching the spec key.",
            },
            "code": {
                "type": "string",
                "description": "Complete Python source file content (no markdown fences).",
            },
        },
        "required": ["agent_id", "code"],
    },
}

_WRITE_SKILL_TOOL: dict[str, Any] = {
    "name": "write_skill_file",
    "description": (
        "Submit the complete Python source for the new skill. "
        "Call this exactly once with the full file content."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Snake-case skill identifier matching the spec entry.",
            },
            "code": {
                "type": "string",
                "description": "Complete Python source file content (no markdown fences).",
            },
        },
        "required": ["skill_name", "code"],
    },
}

_CODE_FENCE_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_tool_use(response: anthropic.types.Message) -> Optional[Any]:
    """Return the first ``tool_use`` content block in *response*, or ``None``."""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return block
    return None


def _extract_python_block(response: anthropic.types.Message) -> Optional[str]:
    """Regex-extract the first `` ```python … ``` `` block from assistant text.

    Returns the code string (without fences), or ``None`` if not found.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            match = _CODE_FENCE_RE.search(block.text)
            if match:
                return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class MiniMaxClient:
    """Thin wrapper around the Anthropic SDK that routes to MiniMax.

    Args:
        api_key:    MiniMax API key.
        model:      Model identifier (default: ``"MiniMax-M2.7"``).
        max_tokens: Maximum tokens in the model response (default: 4096).
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=MINIMAX_BASE_URL,
        )

    def generate(self, prompt: str) -> tuple[str, bool]:
        """Generate agent code. Returns ``(code, via_tool)``."""
        return self._generate_with_tool(prompt, _WRITE_AGENT_TOOL)

    def generate_skill_code(self, prompt: str) -> tuple[str, bool]:
        """Generate skill code. Returns ``(code, via_tool)``."""
        return self._generate_with_tool(prompt, _WRITE_SKILL_TOOL)

    def _generate_with_tool(
        self,
        prompt: str,
        tool: dict[str, Any],
    ) -> tuple[str, bool]:
        """Run the agentic tool loop with *tool* and return ``(code, via_tool)``.

        Args:
            prompt: Full user prompt built by :mod:`agent_codegen.prompt`.
            tool:   Tool definition dict (``_WRITE_AGENT_TOOL`` or ``_WRITE_SKILL_TOOL``).

        Returns:
            ``(code, via_tool)`` where *code* is the complete Python source and
            *via_tool* is ``True`` when the model used the tool call path.

        Raises:
            AgentCodegenError: When neither a tool call nor a code block is found.
        """
        tool_name = tool["name"]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        _LOG.info(
            "→ MiniMax API  model=%s  tool=%s  prompt=%d chars",
            self._model,
            tool_name,
            len(prompt),
        )
        _LOG.debug("Prompt preview:\n%s\n…", prompt[:600])

        # Round 1 — ask the model to call the tool.
        t0 = time.monotonic()
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "any"},
            temperature=0.7,
        )
        elapsed = time.monotonic() - t0

        _LOG.info(
            "← MiniMax API  stop_reason=%s  blocks=%d  %.1fs",
            response.stop_reason,
            len(response.content),
            elapsed,
        )

        tool_block = _find_tool_use(response)
        if tool_block is not None:
            code = tool_block.input.get("code", "")
            _LOG.info(
                "✓ tool call  tool=%s  code=%d chars  via_tool=True",
                tool_block.name,
                len(code),
            )
            _LOG.debug(
                "Generated code preview (first 20 lines):\n%s",
                "\n".join(code.splitlines()[:20]),
            )

            # Round 2 — send tool_result so the model can acknowledge completion.
            ack_messages = messages + [
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_block.id,
                            "content": "File written successfully.",
                        }
                    ],
                },
            ]
            try:
                self._client.messages.create(
                    model=self._model,
                    max_tokens=256,
                    messages=ack_messages,
                    tools=[tool],
                    temperature=0.7,
                )
            except Exception as exc:  # noqa: BLE001
                # Acknowledgement failure is non-fatal — we already have the code.
                _LOG.warning("Round-2 acknowledgement failed (non-fatal): %s", exc)

            return code, True

        # Fallback — try to extract a ```python block from plain text.
        _LOG.warning(
            "Model did not call %s (stop_reason=%s) — trying regex fallback",
            tool_name,
            response.stop_reason,
        )
        code = _extract_python_block(response)
        if code:
            _LOG.info("✓ regex fallback  code=%d chars  via_tool=False", len(code))
            _LOG.debug(
                "Generated code preview (first 20 lines):\n%s",
                "\n".join(code.splitlines()[:20]),
            )
            return code, False

        raise AgentCodegenError(
            f"MiniMax model produced neither a {tool_name!r} tool call nor a Python "
            f"code block. stop_reason={response.stop_reason!r}. "
            "Check the prompt or try again."
        )
