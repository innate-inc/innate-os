"""Tests for agent_codegen.generator."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_codegen.generator import generate_agent
from agent_codegen.models import AgentCodegenError, GenerationResult

# Minimal valid generated code that passes structural validation.
_VALID_CODE = (
    "from brain_client.agent_types import Agent\n"
    "from typing import List, Optional\n\n"
    "class VisitorGreeterAgent(Agent):\n"
    "    @property\n"
    "    def id(self) -> str:\n"
    '        return "visitor_greeter_agent"\n'
    "    @property\n"
    "    def display_name(self) -> str:\n"
    '        return "Visitor Greeter"\n'
    "    def get_skills(self) -> List[str]:\n"
    '        return ["innate-os/greet_visitor"]\n'
    "    def get_prompt(self) -> Optional[str]:\n"
    '        return "Greet visitors."\n'
)

_SPEC = {
    "visitor_greeter_agent": {
        "prompt": "Greet visitors at the door.",
        "new_skills": [{"greet_visitor": "Wave and say hello"}],
    }
}


def _make_mock_client(code: str = _VALID_CODE, via_tool: bool = True):
    """Return a MiniMaxClient mock whose .generate() returns (code, via_tool)."""
    mock = MagicMock()
    mock.generate.return_value = (code, via_tool)
    return mock


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_empty_missing_capabilities_raises(self, _mock):
        with pytest.raises(AgentCodegenError, match="empty"):
            generate_agent({}, api_key="key")

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_non_dict_raises_type_error(self, _mock):
        with pytest.raises((AgentCodegenError, AttributeError, TypeError)):
            generate_agent(None, api_key="key")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Happy-path results
# ---------------------------------------------------------------------------


class TestHappyPath:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_returns_generation_result(self, mock_cls):
        mock_cls.return_value = _make_mock_client()
        result = generate_agent(_SPEC, api_key="key")
        assert isinstance(result, GenerationResult)

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_result_agent_id(self, mock_cls):
        mock_cls.return_value = _make_mock_client()
        result = generate_agent(_SPEC, api_key="key")
        assert result.agent_id == "visitor_greeter_agent"

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_result_code(self, mock_cls):
        mock_cls.return_value = _make_mock_client()
        result = generate_agent(_SPEC, api_key="key")
        assert result.code == _VALID_CODE

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_via_tool_true(self, mock_cls):
        mock_cls.return_value = _make_mock_client(via_tool=True)
        result = generate_agent(_SPEC, api_key="key")
        assert result.via_tool is True

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_via_tool_false(self, mock_cls):
        mock_cls.return_value = _make_mock_client(via_tool=False)
        result = generate_agent(_SPEC, api_key="key")
        assert result.via_tool is False


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


class TestFileIO:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_file_written_when_output_path_given(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client()
        out = tmp_path / "visitor_greeter_agent.py"
        result = generate_agent(_SPEC, api_key="key", output_path=str(out))
        assert out.exists()
        assert out.read_text(encoding="utf-8") == _VALID_CODE
        assert result.file_path == str(out)

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_no_file_when_output_path_none(self, mock_cls):
        mock_cls.return_value = _make_mock_client()
        result = generate_agent(_SPEC, api_key="key", output_path=None)
        assert result.file_path is None

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_backs_up_existing_file(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client()
        out = tmp_path / "visitor_greeter_agent.py"
        out.write_text("old content", encoding="utf-8")

        generate_agent(_SPEC, api_key="key", output_path=str(out))

        bak = out.with_suffix(".py.bak")
        assert bak.exists()
        assert bak.read_text(encoding="utf-8") == "old content"
        assert out.read_text(encoding="utf-8") == _VALID_CODE

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_output_dir_created_if_missing(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client()
        out = tmp_path / "subdir" / "nested" / "agent.py"
        generate_agent(_SPEC, api_key="key", output_path=str(out))
        assert out.exists()


# ---------------------------------------------------------------------------
# Code validation
# ---------------------------------------------------------------------------


class TestCodeValidation:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_validation_failure_raises(self, mock_cls):
        bad_code = "def hello(): return 'world'"  # missing "class", "Agent", "brain_client"
        mock_cls.return_value = _make_mock_client(code=bad_code)
        with pytest.raises(AgentCodegenError, match="structural validation"):
            generate_agent(_SPEC, api_key="key")

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_missing_agent_token_fails(self, mock_cls):
        code = "from brain_client.agent_types import X\nclass MyClass(X): pass"
        mock_cls.return_value = _make_mock_client(code=code)
        with pytest.raises(AgentCodegenError, match="structural validation"):
            generate_agent(_SPEC, api_key="key")

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_missing_brain_client_import_fails(self, mock_cls):
        code = "class MyAgent(Agent): pass"  # no brain_client
        mock_cls.return_value = _make_mock_client(code=code)
        with pytest.raises(AgentCodegenError, match="structural validation"):
            generate_agent(_SPEC, api_key="key")


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------


class TestContextLoading:
    @patch("agent_codegen.generator.load_agent_examples")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_agents_dir_passed_to_load_examples(self, mock_cls, mock_load_examples):
        mock_cls.return_value = _make_mock_client()
        mock_load_examples.return_value = []
        generate_agent(_SPEC, api_key="key", agents_dir="/some/agents")
        mock_load_examples.assert_called_once_with("/some/agents")

    @patch("agent_codegen.generator.load_agent_interface")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_agent_types_path_passed_to_load_interface(self, mock_cls, mock_load_interface):
        mock_cls.return_value = _make_mock_client()
        mock_load_interface.return_value = "class Agent: pass"
        generate_agent(_SPEC, api_key="key", agent_types_path="/path/agent_types.py")
        mock_load_interface.assert_called_once_with("/path/agent_types.py")

    @patch("agent_codegen.generator.load_agent_examples")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_agents_dir_none_is_valid(self, mock_cls, mock_load_examples):
        mock_cls.return_value = _make_mock_client()
        mock_load_examples.return_value = []
        result = generate_agent(_SPEC, api_key="key", agents_dir=None)
        mock_load_examples.assert_called_once_with(None)
        assert isinstance(result, GenerationResult)

    @patch("agent_codegen.generator.load_agent_interface")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_agent_types_path_none_is_valid(self, mock_cls, mock_load_interface):
        mock_cls.return_value = _make_mock_client()
        mock_load_interface.return_value = "class Agent: pass"
        result = generate_agent(_SPEC, api_key="key", agent_types_path=None)
        mock_load_interface.assert_called_once_with(None)
        assert isinstance(result, GenerationResult)

    @patch("agent_codegen.generator.build_prompt")
    @patch("agent_codegen.generator.load_agent_examples")
    @patch("agent_codegen.generator.load_agent_interface")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_prompt_receives_loaded_context(
        self, mock_cls, mock_interface, mock_examples, mock_build
    ):
        mock_cls.return_value = _make_mock_client()
        mock_interface.return_value = "INTERFACE_TEXT"
        mock_examples.return_value = [("agent.py", "class A: pass")]
        mock_build.return_value = "BUILT_PROMPT"

        generate_agent(_SPEC, api_key="key")

        mock_build.assert_called_once_with(
            "visitor_greeter_agent",
            _SPEC["visitor_greeter_agent"],
            agent_interface="INTERFACE_TEXT",
            agent_examples=[("agent.py", "class A: pass")],
        )
