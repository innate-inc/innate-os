"""Tests for agent_codegen.client."""
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from agent_codegen.client import (
    DEFAULT_MODEL,
    MINIMAX_BASE_URL,
    MiniMaxClient,
    _extract_python_block,
    _find_tool_use,
)
from agent_codegen.models import AgentCodegenError


# ---------------------------------------------------------------------------
# Helpers for building mock Anthropic responses
# ---------------------------------------------------------------------------


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(tool_id: str, agent_id: str, code: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use",
        id=tool_id,
        name="write_agent_file",
        input={"agent_id": agent_id, "code": code},
    )


def _make_response(content: list, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason)


# ---------------------------------------------------------------------------
# _find_tool_use
# ---------------------------------------------------------------------------


class TestFindToolUse:
    def test_returns_tool_use_block(self):
        block = _tool_use_block("id1", "my_agent", "class MyAgent: pass")
        response = _make_response([block])
        result = _find_tool_use(response)
        assert result is block

    def test_returns_none_for_text_only(self):
        response = _make_response([_text_block("some text")], stop_reason="end_turn")
        result = _find_tool_use(response)
        assert result is None

    def test_returns_first_tool_block_when_multiple(self):
        block1 = _tool_use_block("id1", "agent1", "code1")
        block2 = _tool_use_block("id2", "agent2", "code2")
        response = _make_response([block1, block2])
        result = _find_tool_use(response)
        assert result is block1

    def test_returns_none_for_empty_content(self):
        response = _make_response([])
        result = _find_tool_use(response)
        assert result is None

    def test_skips_text_blocks_to_find_tool_use(self):
        text = _text_block("thinking...")
        tool = _tool_use_block("id1", "agent", "class Agent: pass")
        response = _make_response([text, tool])
        result = _find_tool_use(response)
        assert result is tool


# ---------------------------------------------------------------------------
# _extract_python_block
# ---------------------------------------------------------------------------


class TestExtractPythonBlock:
    def test_extracts_python_fenced_block(self):
        src = "class MyAgent(Agent): pass"
        response = _make_response([_text_block(f"```python\n{src}\n```")])
        result = _extract_python_block(response)
        assert result == src + "\n"

    def test_returns_none_when_no_fence(self):
        response = _make_response([_text_block("no code here")])
        result = _extract_python_block(response)
        assert result is None

    def test_ignores_non_python_fences(self):
        response = _make_response([_text_block("```json\n{}\n```")])
        result = _extract_python_block(response)
        assert result is None

    def test_returns_none_for_empty_content(self):
        response = _make_response([])
        result = _extract_python_block(response)
        assert result is None

    def test_extracts_from_mixed_content(self):
        src = "class X(Agent): pass"
        blocks = [
            _text_block("Here is the implementation:"),
            _text_block(f"```python\n{src}\n```"),
        ]
        response = _make_response(blocks)
        result = _extract_python_block(response)
        assert src in result


# ---------------------------------------------------------------------------
# MiniMaxClient.generate
# ---------------------------------------------------------------------------


class TestMiniMaxClientGenerate:
    _CODE = "from brain_client.agent_types import Agent\nclass TestAgent(Agent): pass"

    def _mock_client_cls(self, round1_response, round2_response=None):
        """Return a patched anthropic.Anthropic class whose .messages.create returns given responses."""
        mock_anthropic = MagicMock()
        mock_messages = MagicMock()
        mock_anthropic.return_value.messages = mock_messages
        if round2_response is None:
            round2_response = _make_response([_text_block("Done.")], stop_reason="end_turn")
        mock_messages.create.side_effect = [round1_response, round2_response]
        return mock_anthropic

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_generate_calls_create_with_tool(self, mock_anthropic_cls):
        tool_block = _tool_use_block("tid", "test_agent", self._CODE)
        r1 = _make_response([tool_block])
        mock_anthropic_cls.return_value.messages.create.side_effect = [
            r1,
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]

        client = MiniMaxClient(api_key="key")
        client.generate("test prompt")

        first_call = mock_anthropic_cls.return_value.messages.create.call_args_list[0]
        assert first_call.kwargs["tools"][0]["name"] == "write_agent_file"

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_generate_returns_code_via_tool(self, mock_anthropic_cls):
        tool_block = _tool_use_block("tid", "test_agent", self._CODE)
        mock_anthropic_cls.return_value.messages.create.side_effect = [
            _make_response([tool_block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]

        client = MiniMaxClient(api_key="key")
        code, via_tool = client.generate("prompt")

        assert code == self._CODE
        assert via_tool is True

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_generate_sends_tool_result_in_round2(self, mock_anthropic_cls):
        tool_block = _tool_use_block("tid123", "test_agent", self._CODE)
        mock_anthropic_cls.return_value.messages.create.side_effect = [
            _make_response([tool_block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]

        client = MiniMaxClient(api_key="key")
        client.generate("prompt")

        calls = mock_anthropic_cls.return_value.messages.create.call_args_list
        assert len(calls) == 2
        round2_messages = calls[1].kwargs["messages"]
        # Last message should be the tool_result
        tool_result_msg = round2_messages[-1]
        assert tool_result_msg["role"] == "user"
        assert tool_result_msg["content"][0]["type"] == "tool_result"
        assert tool_result_msg["content"][0]["tool_use_id"] == "tid123"

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_generate_fallback_to_python_block(self, mock_anthropic_cls):
        text = f"Here you go:\n```python\n{self._CODE}\n```"
        mock_anthropic_cls.return_value.messages.create.return_value = (
            _make_response([_text_block(text)], stop_reason="end_turn")
        )

        client = MiniMaxClient(api_key="key")
        code, via_tool = client.generate("prompt")

        assert self._CODE in code
        assert via_tool is False

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_generate_raises_when_no_code(self, mock_anthropic_cls):
        mock_anthropic_cls.return_value.messages.create.return_value = (
            _make_response([_text_block("I cannot do that.")], stop_reason="end_turn")
        )

        client = MiniMaxClient(api_key="key")
        with pytest.raises(AgentCodegenError, match="neither a tool call nor a Python code block"):
            client.generate("prompt")

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_generate_uses_tool_choice_any(self, mock_anthropic_cls):
        tool_block = _tool_use_block("t", "a", self._CODE)
        mock_anthropic_cls.return_value.messages.create.side_effect = [
            _make_response([tool_block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]

        client = MiniMaxClient(api_key="key")
        client.generate("prompt")

        first_call = mock_anthropic_cls.return_value.messages.create.call_args_list[0]
        assert first_call.kwargs["tool_choice"] == {"type": "any"}

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_generate_passes_model_and_max_tokens(self, mock_anthropic_cls):
        tool_block = _tool_use_block("t", "a", self._CODE)
        mock_anthropic_cls.return_value.messages.create.side_effect = [
            _make_response([tool_block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]

        client = MiniMaxClient(api_key="key", model="MiniMax-M2.5", max_tokens=2048)
        client.generate("prompt")

        first_call = mock_anthropic_cls.return_value.messages.create.call_args_list[0]
        assert first_call.kwargs["model"] == "MiniMax-M2.5"
        assert first_call.kwargs["max_tokens"] == 2048

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_client_instantiated_with_minimax_base_url(self, mock_anthropic_cls):
        MiniMaxClient(api_key="test_key")
        mock_anthropic_cls.assert_called_once_with(
            api_key="test_key",
            base_url=MINIMAX_BASE_URL,
        )
