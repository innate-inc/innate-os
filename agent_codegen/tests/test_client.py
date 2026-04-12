"""Tests for agent_codegen.client."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _tool_use_block(tool_id: str, tool_name: str, **input_kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use",
        id=tool_id,
        name=tool_name,
        input=input_kwargs,
    )


def _agent_tool_block(tool_id: str, agent_id: str, code: str) -> SimpleNamespace:
    return _tool_use_block(tool_id, "write_agent_file", agent_id=agent_id, code=code)


def _skill_tool_block(tool_id: str, skill_name: str, code: str) -> SimpleNamespace:
    return _tool_use_block(tool_id, "write_skill_file", skill_name=skill_name, code=code)


def _make_response(content: list, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason=stop_reason)


_AGENT_CODE = (
    "from brain_client.agent_types import Agent\n"
    "class TestAgent(Agent): pass"
)
_SKILL_CODE = (
    "from brain_client.skill_types import Skill, SkillResult\n"
    "class TestSkill(Skill): pass"
)


# ---------------------------------------------------------------------------
# _find_tool_use
# ---------------------------------------------------------------------------


class TestFindToolUse:
    def test_returns_tool_use_block(self):
        block = _agent_tool_block("id1", "my_agent", _AGENT_CODE)
        result = _find_tool_use(_make_response([block]))
        assert result is block

    def test_returns_none_for_text_only(self):
        result = _find_tool_use(_make_response([_text_block("text")], stop_reason="end_turn"))
        assert result is None

    def test_returns_first_tool_block_when_multiple(self):
        b1 = _agent_tool_block("id1", "a1", "code1")
        b2 = _agent_tool_block("id2", "a2", "code2")
        assert _find_tool_use(_make_response([b1, b2])) is b1

    def test_returns_none_for_empty_content(self):
        assert _find_tool_use(_make_response([])) is None

    def test_skips_text_blocks_to_find_tool_use(self):
        text = _text_block("thinking...")
        tool = _agent_tool_block("id1", "agent", _AGENT_CODE)
        assert _find_tool_use(_make_response([text, tool])) is tool


# ---------------------------------------------------------------------------
# _extract_python_block
# ---------------------------------------------------------------------------


class TestExtractPythonBlock:
    def test_extracts_python_fenced_block(self):
        src = "class MyAgent(Agent): pass"
        result = _extract_python_block(_make_response([_text_block(f"```python\n{src}\n```")]))
        assert src in result

    def test_returns_none_when_no_fence(self):
        assert _extract_python_block(_make_response([_text_block("no code")])) is None

    def test_ignores_non_python_fences(self):
        assert _extract_python_block(_make_response([_text_block("```json\n{}\n```")])) is None

    def test_returns_none_for_empty_content(self):
        assert _extract_python_block(_make_response([])) is None

    def test_extracts_from_mixed_content(self):
        src = "class X(Agent): pass"
        blocks = [_text_block("Here:"), _text_block(f"```python\n{src}\n```")]
        assert src in _extract_python_block(_make_response(blocks))


# ---------------------------------------------------------------------------
# MiniMaxClient — shared _generate_with_tool path
# ---------------------------------------------------------------------------


class TestMiniMaxClientShared:
    """Tests that apply to both generate() and generate_skill_code() via the shared internal."""

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_client_instantiated_with_minimax_base_url(self, mock_cls):
        MiniMaxClient(api_key="test_key")
        mock_cls.assert_called_once_with(api_key="test_key", base_url=MINIMAX_BASE_URL)

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_uses_tool_choice_any(self, mock_cls):
        block = _agent_tool_block("t", "a", _AGENT_CODE)
        mock_cls.return_value.messages.create.side_effect = [
            _make_response([block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]
        MiniMaxClient(api_key="key").generate("prompt")
        first_call = mock_cls.return_value.messages.create.call_args_list[0]
        assert first_call.kwargs["tool_choice"] == {"type": "any"}

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_passes_model_and_max_tokens(self, mock_cls):
        block = _agent_tool_block("t", "a", _AGENT_CODE)
        mock_cls.return_value.messages.create.side_effect = [
            _make_response([block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]
        MiniMaxClient(api_key="key", model="MiniMax-M2.5", max_tokens=2048).generate("prompt")
        first_call = mock_cls.return_value.messages.create.call_args_list[0]
        assert first_call.kwargs["model"] == "MiniMax-M2.5"
        assert first_call.kwargs["max_tokens"] == 2048

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_fallback_to_python_block(self, mock_cls):
        text = f"Here:\n```python\n{_AGENT_CODE}\n```"
        mock_cls.return_value.messages.create.return_value = (
            _make_response([_text_block(text)], stop_reason="end_turn")
        )
        code, via_tool = MiniMaxClient(api_key="key").generate("prompt")
        assert _AGENT_CODE in code
        assert via_tool is False

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_raises_when_no_code(self, mock_cls):
        mock_cls.return_value.messages.create.return_value = (
            _make_response([_text_block("I cannot do that.")], stop_reason="end_turn")
        )
        with pytest.raises(AgentCodegenError, match="neither a"):
            MiniMaxClient(api_key="key").generate("prompt")


# ---------------------------------------------------------------------------
# MiniMaxClient.generate (agent tool)
# ---------------------------------------------------------------------------


class TestMiniMaxClientGenerateAgent:
    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_calls_write_agent_file_tool(self, mock_cls):
        block = _agent_tool_block("t", "a", _AGENT_CODE)
        mock_cls.return_value.messages.create.side_effect = [
            _make_response([block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]
        MiniMaxClient(api_key="key").generate("prompt")
        first_call = mock_cls.return_value.messages.create.call_args_list[0]
        assert first_call.kwargs["tools"][0]["name"] == "write_agent_file"

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_returns_code_and_via_tool_true(self, mock_cls):
        block = _agent_tool_block("t", "agent", _AGENT_CODE)
        mock_cls.return_value.messages.create.side_effect = [
            _make_response([block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]
        code, via_tool = MiniMaxClient(api_key="key").generate("prompt")
        assert code == _AGENT_CODE
        assert via_tool is True

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_sends_tool_result_in_round2(self, mock_cls):
        block = _agent_tool_block("tid123", "agent", _AGENT_CODE)
        mock_cls.return_value.messages.create.side_effect = [
            _make_response([block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]
        MiniMaxClient(api_key="key").generate("prompt")
        calls = mock_cls.return_value.messages.create.call_args_list
        assert len(calls) == 2
        round2_messages = calls[1].kwargs["messages"]
        tool_result_msg = round2_messages[-1]
        assert tool_result_msg["role"] == "user"
        assert tool_result_msg["content"][0]["type"] == "tool_result"
        assert tool_result_msg["content"][0]["tool_use_id"] == "tid123"


# ---------------------------------------------------------------------------
# MiniMaxClient.generate_skill_code (skill tool)
# ---------------------------------------------------------------------------


class TestMiniMaxClientGenerateSkill:
    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_calls_write_skill_file_tool(self, mock_cls):
        block = _skill_tool_block("t", "greet_visitor", _SKILL_CODE)
        mock_cls.return_value.messages.create.side_effect = [
            _make_response([block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]
        MiniMaxClient(api_key="key").generate_skill_code("prompt")
        first_call = mock_cls.return_value.messages.create.call_args_list[0]
        assert first_call.kwargs["tools"][0]["name"] == "write_skill_file"

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_returns_code_and_via_tool_true(self, mock_cls):
        block = _skill_tool_block("t", "greet_visitor", _SKILL_CODE)
        mock_cls.return_value.messages.create.side_effect = [
            _make_response([block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]
        code, via_tool = MiniMaxClient(api_key="key").generate_skill_code("prompt")
        assert code == _SKILL_CODE
        assert via_tool is True

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_sends_tool_result_in_round2(self, mock_cls):
        block = _skill_tool_block("sid99", "my_skill", _SKILL_CODE)
        mock_cls.return_value.messages.create.side_effect = [
            _make_response([block]),
            _make_response([_text_block("ok")], stop_reason="end_turn"),
        ]
        MiniMaxClient(api_key="key").generate_skill_code("prompt")
        calls = mock_cls.return_value.messages.create.call_args_list
        round2_messages = calls[1].kwargs["messages"]
        tool_result_msg = round2_messages[-1]
        assert tool_result_msg["content"][0]["tool_use_id"] == "sid99"

    @patch("agent_codegen.client.anthropic.Anthropic")
    def test_raises_when_no_code(self, mock_cls):
        mock_cls.return_value.messages.create.return_value = (
            _make_response([_text_block("nope")], stop_reason="end_turn")
        )
        with pytest.raises(AgentCodegenError):
            MiniMaxClient(api_key="key").generate_skill_code("prompt")
