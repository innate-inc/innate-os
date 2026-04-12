"""Tests for agent_codegen.generator."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_codegen.generator import (
    _build_skill_desc_map,
    _load_capabilities,
    _parse_skill_entries,
    generate_agent,
    generate_skill,
)
from agent_codegen.models import AgentCodegenError, GenerationResult, SkillGenerationResult

_VALID_AGENT_CODE = (
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

_VALID_SKILL_CODE = (
    "from brain_client.skill_types import Skill, SkillResult\n"
    "from typing import Optional\n\n"
    "class GreetVisitor(Skill):\n"
    "    @property\n"
    "    def name(self) -> str:\n"
    '        return "greet_visitor"\n'
    "    def execute(self, **kwargs):\n"
    '        return "Greeted visitor", SkillResult.SUCCESS\n'
    "    def cancel(self) -> str:\n"
    '        return "Cancelled"\n'
)

_MISSING_CAPABILITIES = {
    "visitor_greeter_agent": {
        "prompt": "Greet visitors at the door.",
        "new_skills": [{"greet_visitor": "Wave and say hello"}],
    }
}


def _make_mock_client(code: str, via_tool: bool = True):
    mock = MagicMock()
    mock.generate.return_value = (code, via_tool)
    mock.generate_skill_code.return_value = (_VALID_SKILL_CODE, via_tool)
    return mock


# ---------------------------------------------------------------------------
# _parse_skill_entries
# ---------------------------------------------------------------------------


class TestParseSkillEntries:
    def test_single_key_dict_format(self):
        entries = _parse_skill_entries([{"greet_visitor": "Wave hello"}])
        assert entries == [("greet_visitor", "Wave hello")]

    def test_two_key_dict_format(self):
        entries = _parse_skill_entries([
            {"skill_name": "head_emotion", "description": "Express emotion"}
        ])
        assert entries == [("head_emotion", "Express emotion")]

    def test_multiple_entries(self):
        entries = _parse_skill_entries([
            {"wave": "Wave arm"},
            {"skill_name": "speak", "description": "Say something"},
        ])
        assert entries == [("wave", "Wave arm"), ("speak", "Say something")]

    def test_skips_non_dict_items(self):
        entries = _parse_skill_entries(["not_a_dict", {"wave": "Wave"}])
        assert entries == [("wave", "Wave")]

    def test_empty_list_returns_empty(self):
        assert _parse_skill_entries([]) == []

    def test_fallback_for_multi_key_with_skill_name(self):
        entries = _parse_skill_entries([
            {"skill_name": "wave", "extra_key": "extra_value", "another": "val"}
        ])
        assert len(entries) == 1
        assert entries[0][0] == "wave"

    def test_skips_multi_key_without_skill_name(self):
        entries = _parse_skill_entries([{"a": "1", "b": "2"}])
        # No skill_name key and len > 1 — should be skipped
        assert entries == []


# ---------------------------------------------------------------------------
# generate_skill
# ---------------------------------------------------------------------------


class TestGenerateSkill:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_returns_skill_generation_result(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE)
        result = generate_skill("greet_visitor", "Wave and say hello", api_key="key")
        assert isinstance(result, SkillGenerationResult)
        assert result.skill_name == "greet_visitor"

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_returns_generated_code(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE)
        result = generate_skill("greet_visitor", "desc", api_key="key")
        assert result.code == _VALID_SKILL_CODE

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_via_tool_true(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE, via_tool=True)
        result = generate_skill("greet_visitor", "desc", api_key="key")
        assert result.via_tool is True

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_via_tool_false(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE, via_tool=False)
        result = generate_skill("greet_visitor", "desc", api_key="key")
        assert result.via_tool is False

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_no_file_when_output_path_none(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE)
        result = generate_skill("greet_visitor", "desc", api_key="key")
        assert result.file_path is None

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_file_written_when_output_path_given(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE)
        out = tmp_path / "greet_visitor.py"
        result = generate_skill("greet_visitor", "desc", api_key="key", output_path=str(out))
        assert out.exists()
        assert out.read_text(encoding="utf-8") == _VALID_SKILL_CODE
        assert result.file_path == str(out)

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_backs_up_existing_file(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE)
        out = tmp_path / "greet_visitor.py"
        out.write_text("old content", encoding="utf-8")
        generate_skill("greet_visitor", "desc", api_key="key", output_path=str(out))
        assert out.with_suffix(".py.bak").read_text(encoding="utf-8") == "old content"
        assert out.read_text(encoding="utf-8") == _VALID_SKILL_CODE

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_empty_skill_name_raises(self, _mock):
        with pytest.raises(AgentCodegenError, match="skill_name"):
            generate_skill("", "desc", api_key="key")

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_empty_description_raises(self, _mock):
        with pytest.raises(AgentCodegenError, match="description"):
            generate_skill("my_skill", "", api_key="key")

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_validation_failure_raises(self, mock_cls):
        bad_code = "def not_a_skill(): pass"
        mock_client = _make_mock_client(_VALID_AGENT_CODE)
        mock_client.generate_skill_code.return_value = (bad_code, True)
        mock_cls.return_value = mock_client
        with pytest.raises(AgentCodegenError, match="structural validation"):
            generate_skill("my_skill", "desc", api_key="key")

    @patch("agent_codegen.generator.load_skill_examples")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_skills_dir_passed_to_load_examples(self, mock_cls, mock_load):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE)
        mock_load.return_value = []
        generate_skill("greet_visitor", "desc", api_key="key", skills_dir="/my/skills")
        mock_load.assert_called_once_with("/my/skills")

    @patch("agent_codegen.generator.load_skill_interface")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_skill_types_path_passed_to_load_interface(self, mock_cls, mock_load):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE)
        mock_load.return_value = "class Skill: pass"
        generate_skill(
            "greet_visitor", "desc", api_key="key",
            skill_types_path="/path/skill_types.py",
        )
        mock_load.assert_called_once_with("/path/skill_types.py")

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_output_dir_created_if_missing(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client(_VALID_SKILL_CODE)
        out = tmp_path / "nested" / "skill.py"
        generate_skill("greet_visitor", "desc", api_key="key", output_path=str(out))
        assert out.exists()

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_generate_skill_code_called(self, mock_cls):
        mock_client = _make_mock_client(_VALID_SKILL_CODE)
        mock_cls.return_value = mock_client
        generate_skill("greet_visitor", "desc", api_key="key")
        mock_client.generate_skill_code.assert_called_once()


# ---------------------------------------------------------------------------
# generate_agent
# ---------------------------------------------------------------------------


class TestGenerateAgentInputValidation:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_empty_missing_capabilities_raises(self, _mock):
        with pytest.raises(AgentCodegenError, match="empty"):
            generate_agent({}, api_key="key")


class TestGenerateAgentHappyPath:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_returns_generation_result(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        result = generate_agent(_MISSING_CAPABILITIES, api_key="key")
        assert isinstance(result, GenerationResult)

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_result_agent_id(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        result = generate_agent(_MISSING_CAPABILITIES, api_key="key")
        assert result.agent_id == "visitor_greeter_agent"

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_no_skills_generated_without_skills_dir(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        result = generate_agent(_MISSING_CAPABILITIES, api_key="key")
        assert result.skills == []

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_skills_generated_when_skills_dir_provided(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        result = generate_agent(
            _MISSING_CAPABILITIES,
            api_key="key",
            skills_dir=str(tmp_path),
        )
        assert len(result.skills) == 1
        assert result.skills[0].skill_name == "greet_visitor"

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_skill_file_written_to_skills_dir(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        generate_agent(
            _MISSING_CAPABILITIES,
            api_key="key",
            skills_dir=str(tmp_path),
        )
        skill_file = tmp_path / "greet_visitor.py"
        assert skill_file.exists()

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_agent_file_written_when_output_path_given(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        out = tmp_path / "visitor_greeter_agent.py"
        result = generate_agent(
            _MISSING_CAPABILITIES, api_key="key", output_path=str(out)
        )
        assert out.exists()
        assert result.file_path == str(out)

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_no_agent_file_when_output_path_none(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        result = generate_agent(_MISSING_CAPABILITIES, api_key="key")
        assert result.file_path is None


class TestGenerateAgentSkillErrors:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_skill_error_does_not_abort_agent_generation(self, mock_cls, tmp_path):
        """A skill generation failure should not raise — it appends an empty result."""
        mock_client = MagicMock()
        mock_client.generate.return_value = (_VALID_AGENT_CODE, True)
        mock_client.generate_skill_code.side_effect = AgentCodegenError("skill failed")
        mock_cls.return_value = mock_client

        result = generate_agent(
            _MISSING_CAPABILITIES,
            api_key="key",
            skills_dir=str(tmp_path),
        )
        # Agent still generated correctly
        assert result.agent_id == "visitor_greeter_agent"
        # Skill result recorded with empty code
        assert len(result.skills) == 1
        assert result.skills[0].code == ""
        assert result.skills[0].file_path is None

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_multiple_skills_generated(self, mock_cls, tmp_path):
        spec = {
            "multi_skill_agent": {
                "prompt": "An agent with multiple skills.",
                "new_skills": [
                    {"wave": "Wave the arm"},
                    {"speak": "Say something"},
                ],
            }
        }
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        result = generate_agent(spec, api_key="key", skills_dir=str(tmp_path))
        assert len(result.skills) == 2
        skill_names = {s.skill_name for s in result.skills}
        assert skill_names == {"wave", "speak"}


class TestGenerateAgentContextLoading:
    @patch("agent_codegen.generator.load_agent_examples")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_agents_dir_passed_to_load_examples(self, mock_cls, mock_load):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        mock_load.return_value = []
        generate_agent(_MISSING_CAPABILITIES, api_key="key", agents_dir="/agents")
        mock_load.assert_called_once_with("/agents")

    @patch("agent_codegen.generator.load_agent_interface")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_agent_types_path_passed_to_load_interface(self, mock_cls, mock_load):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        mock_load.return_value = "class Agent: pass"
        generate_agent(
            _MISSING_CAPABILITIES, api_key="key",
            agent_types_path="/path/agent_types.py",
        )
        mock_load.assert_called_once_with("/path/agent_types.py")

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_skills_dir_none_generates_no_skills(self, mock_cls):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        result = generate_agent(_MISSING_CAPABILITIES, api_key="key", skills_dir=None)
        assert result.skills == []


class TestGenerateAgentCodeValidation:
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_validation_failure_raises(self, mock_cls):
        mock_cls.return_value = _make_mock_client("def hello(): return 'world'")
        with pytest.raises(AgentCodegenError, match="structural validation"):
            generate_agent(_MISSING_CAPABILITIES, api_key="key")

    @patch("agent_codegen.generator.MiniMaxClient")
    def test_backs_up_existing_agent_file(self, mock_cls, tmp_path):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        out = tmp_path / "agent.py"
        out.write_text("old content", encoding="utf-8")
        generate_agent(_MISSING_CAPABILITIES, api_key="key", output_path=str(out))
        assert out.with_suffix(".py.bak").read_text(encoding="utf-8") == "old content"


# ---------------------------------------------------------------------------
# _load_capabilities
# ---------------------------------------------------------------------------


class TestLoadCapabilities:
    def test_returns_dict_from_valid_file(self, tmp_path):
        caps_file = tmp_path / "capabilities.json"
        caps_file.write_text('{"demo_agent": {"skills": ["wave"]}}', encoding="utf-8")
        result = _load_capabilities(str(caps_file))
        assert "demo_agent" in result

    def test_returns_empty_for_none_path(self):
        assert _load_capabilities(None) == {}

    def test_returns_empty_for_missing_file(self, tmp_path):
        assert _load_capabilities(str(tmp_path / "nonexistent.json")) == {}

    def test_returns_empty_for_invalid_json(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json", encoding="utf-8")
        assert _load_capabilities(str(bad_file)) == {}


# ---------------------------------------------------------------------------
# _build_skill_desc_map
# ---------------------------------------------------------------------------


class TestBuildSkillDescMap:
    def test_extracts_name_and_description(self):
        caps = {"demo_agent": {"skills": ["wave: Wave the arm"]}}
        result = _build_skill_desc_map(caps)
        assert result["wave"] == "Wave the arm"

    def test_skill_without_description(self):
        caps = {"demo_agent": {"skills": ["wave"]}}
        result = _build_skill_desc_map(caps)
        assert result["wave"] == ""

    def test_merges_skills_across_agents(self):
        caps = {
            "agent_a": {"skills": ["skill_x: desc x"]},
            "agent_b": {"skills": ["skill_y: desc y"]},
        }
        result = _build_skill_desc_map(caps)
        assert "skill_x" in result
        assert "skill_y" in result

    def test_empty_capabilities_returns_empty(self):
        assert _build_skill_desc_map({}) == {}

    def test_agent_with_no_skills_key(self):
        caps = {"agent_a": {}}
        assert _build_skill_desc_map(caps) == {}


# ---------------------------------------------------------------------------
# generate_agent — capabilities_path integration
# ---------------------------------------------------------------------------


class TestGenerateAgentCapabilitiesPath:
    @patch("agent_codegen.generator.build_prompt")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_capabilities_path_none_passes_empty_existing_skills(
        self, mock_cls, mock_build_prompt
    ):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        mock_build_prompt.return_value = "prompt"
        generate_agent(_MISSING_CAPABILITIES, api_key="key", capabilities_path=None)
        _, kwargs = mock_build_prompt.call_args
        assert kwargs["existing_skills"] == []

    @patch("agent_codegen.generator.build_prompt")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_capabilities_path_loads_descriptions(
        self, mock_cls, mock_build_prompt, tmp_path
    ):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        mock_build_prompt.return_value = "prompt"

        caps_file = tmp_path / "capabilities.json"
        caps_file.write_text(
            '{"demo_agent": {"skills": ["greet_visitor: Wave and say hello"]}}',
            encoding="utf-8",
        )
        spec = {
            "visitor_greeter_agent": {
                "prompt": "Greet visitors.",
                "new_skills": [{"greet_visitor": "Wave and say hello"}],
                "existing_skills": ["greet_visitor"],
            }
        }
        generate_agent(spec, api_key="key", capabilities_path=str(caps_file))
        _, kwargs = mock_build_prompt.call_args
        existing = dict(kwargs["existing_skills"])
        assert "greet_visitor" in existing
        assert existing["greet_visitor"] == "Wave and say hello"

    @patch("agent_codegen.generator.build_prompt")
    @patch("agent_codegen.generator.MiniMaxClient")
    def test_missing_capabilities_file_still_generates(
        self, mock_cls, mock_build_prompt, tmp_path
    ):
        mock_cls.return_value = _make_mock_client(_VALID_AGENT_CODE)
        mock_build_prompt.return_value = "prompt"
        result = generate_agent(
            _MISSING_CAPABILITIES,
            api_key="key",
            capabilities_path=str(tmp_path / "nonexistent.json"),
        )
        assert result.agent_id == "visitor_greeter_agent"
