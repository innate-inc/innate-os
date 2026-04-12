"""Tests for agent_codegen.models."""
import pytest

from agent_codegen.models import AgentCodegenError, GenerationResult, SkillGenerationResult


class TestAgentCodegenError:
    def test_is_runtime_error(self):
        assert issubclass(AgentCodegenError, RuntimeError)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(AgentCodegenError, match="test message"):
            raise AgentCodegenError("test message")

    def test_message_preserved(self):
        err = AgentCodegenError("specific failure")
        assert "specific failure" in str(err)


class TestSkillGenerationResult:
    def test_all_fields_accessible(self):
        result = SkillGenerationResult(
            skill_name="greet_visitor",
            code="from brain_client.skill_types import Skill\nclass GreetVisitor(Skill): pass",
            file_path="/tmp/greet_visitor.py",
            via_tool=True,
        )
        assert result.skill_name == "greet_visitor"
        assert "Skill" in result.code
        assert result.file_path == "/tmp/greet_visitor.py"
        assert result.via_tool is True

    def test_optional_file_path_none(self):
        result = SkillGenerationResult(
            skill_name="my_skill",
            code="code",
            file_path=None,
            via_tool=False,
        )
        assert result.file_path is None

    def test_via_tool_false(self):
        result = SkillGenerationResult(
            skill_name="x", code="code", file_path=None, via_tool=False
        )
        assert result.via_tool is False


class TestGenerationResult:
    def test_all_fields_accessible(self):
        result = GenerationResult(
            agent_id="test_agent",
            code="class TestAgent(Agent): pass",
            file_path="/tmp/test_agent.py",
            via_tool=True,
        )
        assert result.agent_id == "test_agent"
        assert result.code == "class TestAgent(Agent): pass"
        assert result.file_path == "/tmp/test_agent.py"
        assert result.via_tool is True

    def test_skills_defaults_to_empty_list(self):
        result = GenerationResult(
            agent_id="test_agent",
            code="code",
            file_path=None,
            via_tool=True,
        )
        assert result.skills == []

    def test_skills_populated(self):
        skill = SkillGenerationResult(
            skill_name="wave", code="class Wave(Skill): pass", file_path=None, via_tool=True
        )
        result = GenerationResult(
            agent_id="demo_agent",
            code="class DemoAgent(Agent): pass",
            file_path=None,
            via_tool=True,
            skills=[skill],
        )
        assert len(result.skills) == 1
        assert result.skills[0].skill_name == "wave"

    def test_optional_file_path_none(self):
        result = GenerationResult(
            agent_id="test_agent",
            code="class TestAgent(Agent): pass",
            file_path=None,
            via_tool=False,
        )
        assert result.file_path is None

    def test_via_tool_false(self):
        result = GenerationResult(
            agent_id="x", code="code", file_path=None, via_tool=False
        )
        assert result.via_tool is False
