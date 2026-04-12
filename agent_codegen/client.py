"""
MiniMax client for agent_codegen.

Wraps the Anthropic SDK pointed at the MiniMax Anthropic-compatible endpoint.
Implements a two-round agentic tool loop:

  Round 1 — model calls ``write_agent_file`` (tool_choice="any").
  Round 2 — library sends the tool_result so the model can acknowledge.

Falls back to regex extraction of a markdown ``python`` code block when the
model does not call the tool (should be rare with tool_choice="any").
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import anthropic

from agent_codegen.models import AgentCodegenError

_LOG = logging.getLogger(__name__)

MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
DEFAULT_MODEL = "MiniMax-M2.7"

_WRITE_TOOL: dict[str, Any] = {
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
        """Run the agentic tool loop and return ``(code, via_tool)``.

        Args:
            prompt: The full user prompt built by :mod:`agent_codegen.prompt`.

        Returns:
            A tuple ``(code, via_tool)`` where *code* is the complete Python
            source and *via_tool* is ``True`` when the model used the
            ``write_agent_file`` tool (``False`` for regex fallback).

        Raises:
            AgentCodegenError: When the model produces neither a tool call nor
                a Python code block.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        _LOG.info("Sending prompt to MiniMax (%d chars, model=%s)", len(prompt), self._model)

        # Round 1 — ask the model to call write_agent_file.
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
            tools=[_WRITE_TOOL],
            tool_choice={"type": "any"},
            temperature=0.7,
        )

        _LOG.debug("Round-1 stop_reason=%s, content blocks=%d", response.stop_reason, len(response.content))

        tool_block = _find_tool_use(response)
        if tool_block is not None:
            agent_id_from_model = tool_block.input.get("agent_id", "")
            code = tool_block.input.get("code", "")
            _LOG.info(
                "Tool call received: agent_id=%r, code length=%d chars",
                agent_id_from_model,
                len(code),
            )

            # Round 2 — send tool_result so the model can acknowledge completion.
            messages = messages + [
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
                    messages=messages,
                    tools=[_WRITE_TOOL],
                    temperature=0.7,
                )
            except Exception as exc:  # noqa: BLE001
                # Acknowledgement failure is non-fatal — we already have the code.
                _LOG.warning("Round-2 acknowledgement failed (non-fatal): %s", exc)

            return code, True

        # Fallback — try to extract a ```python block from plain text.
        _LOG.warning(
            "Model did not call write_agent_file (stop_reason=%s); attempting regex fallback",
            response.stop_reason,
        )
        code = _extract_python_block(response)
        if code:
            _LOG.info("Regex fallback succeeded (%d chars)", len(code))
            return code, False

        raise AgentCodegenError(
            "MiniMax model produced neither a tool call nor a Python code block. "
            f"stop_reason={response.stop_reason!r}. "
            "Check the prompt or try again."
        )
