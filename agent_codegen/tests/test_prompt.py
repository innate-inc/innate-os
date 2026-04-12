"""Tests for agent_codegen.prompt."""
import json
from pathlib import Path

import pytest

from agent_codegen.prompt import (
    _AGENT_INTERFACE_FALLBACK,
    _SKILL_INTERFACE_FALLBACK,
    _format_existing_skills,
    build_prompt,
    build_skill_prompt,
    load_agent_examples,
    load_agent_interface,
    load_skill_examples,
    load_skill_interface,
)


# ---------------------------------------------------------------------------
# load_agent_interface
# ---------------------------------------------------------------------------


class TestLoadAgentInterface:
    def test_returns_file_content_when_path_given(self, tmp_path):
        interface_file = tmp_path / "agent_types.py"
        interface_file.write_text("class Agent: pass", encoding="utf-8")
        result = load_agent_interface(str(interface_file))
        assert result == "class Agent: pass"

    def test_fallback_when_path_is_none(self):
        result = load_agent_interface(None)
        assert result == _AGENT_INTERFACE_FALLBACK

    def test_fallback_when_file_missing(self, tmp_path):
        result = load_agent_interface(str(tmp_path / "nonexistent.py"))
        assert result == _AGENT_INTERFACE_FALLBACK

    def test_fallback_when_path_is_directory(self, tmp_path):
        result = load_agent_interface(str(tmp_path))
        assert result == _AGENT_INTERFACE_FALLBACK

    def test_fallback_contains_agent_abc(self):
        assert "class Agent" in _AGENT_INTERFACE_FALLBACK
        assert "abstractmethod" in _AGENT_INTERFACE_FALLBACK


# ---------------------------------------------------------------------------
# load_skill_interface
# ---------------------------------------------------------------------------


class TestLoadSkillInterface:
    def test_returns_file_content_when_path_given(self, tmp_path):
        interface_file = tmp_path / "skill_types.py"
        interface_file.write_text("class Skill: pass", encoding="utf-8")
        result = load_skill_interface(str(interface_file))
        assert result == "class Skill: pass"

    def test_fallback_when_path_is_none(self):
        result = load_skill_interface(None)
        assert result == _SKILL_INTERFACE_FALLBACK

    def test_fallback_when_file_missing(self, tmp_path):
        result = load_skill_interface(str(tmp_path / "nonexistent.py"))
        assert result == _SKILL_INTERFACE_FALLBACK

    def test_fallback_contains_skill_abc(self):
        assert "class Skill" in _SKILL_INTERFACE_FALLBACK
        assert "SkillResult" in _SKILL_INTERFACE_FALLBACK

    def test_fallback_contains_interface_types(self):
        assert "InterfaceType" in _SKILL_INTERFACE_FALLBACK
        assert "MANIPULATION" in _SKILL_INTERFACE_FALLBACK


# ---------------------------------------------------------------------------
# load_agent_examples
# ---------------------------------------------------------------------------


class TestLoadAgentExamples:
    def _make_file(self, directory: Path, name: str, content: str) -> None:
        (directory / name).write_text(content, encoding="utf-8")

    def test_returns_up_to_max_examples(self, tmp_path):
        for i in range(5):
            self._make_file(tmp_path, f"agent_{i}.py", f"class A{i}(Agent): pass  # {'x' * i}")
        result = load_agent_examples(str(tmp_path), max_examples=2)
        assert len(result) == 2

    def test_returns_all_when_fewer_than_max(self, tmp_path):
        self._make_file(tmp_path, "agent_a.py", "class A(Agent): pass")
        result = load_agent_examples(str(tmp_path), max_examples=5)
        assert len(result) == 1

    def test_skips_init_py(self, tmp_path):
        (tmp_path / "__init__.py").write_text("# init", encoding="utf-8")
        self._make_file(tmp_path, "real_agent.py", "class RealAgent(Agent): pass")
        result = load_agent_examples(str(tmp_path))
        names = [name for name, _ in result]
        assert "__init__.py" not in names
        assert "real_agent.py" in names

    def test_skips_underscore_files(self, tmp_path):
        (tmp_path / "_private.py").write_text("# private", encoding="utf-8")
        self._make_file(tmp_path, "normal_agent.py", "class NormalAgent(Agent): pass")
        result = load_agent_examples(str(tmp_path))
        names = [name for name, _ in result]
        assert "_private.py" not in names

    def test_skips_orchestrator_agent(self, tmp_path):
        self._make_file(tmp_path, "orchestrator_agent.py", "class OrchestratorAgent(Agent): pass")
        self._make_file(tmp_path, "basic_agent.py", "class BasicAgent(Agent): pass")
        result = load_agent_examples(str(tmp_path))
        names = [name for name, _ in result]
        assert "orchestrator_agent.py" not in names
        assert "basic_agent.py" in names

    def test_returns_empty_for_none_dir(self):
        assert load_agent_examples(None) == []

    def test_returns_empty_for_missing_dir(self, tmp_path):
        assert load_agent_examples(str(tmp_path / "nonexistent")) == []

    def test_selects_shortest_files_first(self, tmp_path):
        self._make_file(tmp_path, "long_agent.py", "class Long(Agent): pass" + " " * 500)
        self._make_file(tmp_path, "short_agent.py", "class Short(Agent): pass")
        self._make_file(tmp_path, "medium_agent.py", "class Medium(Agent): pass" + " " * 100)
        result = load_agent_examples(str(tmp_path), max_examples=2)
        names = [name for name, _ in result]
        assert names[0] == "short_agent.py"
        assert names[1] == "medium_agent.py"

    def test_returns_filename_and_source_pairs(self, tmp_path):
        src = "class MyAgent(Agent): pass"
        self._make_file(tmp_path, "my_agent.py", src)
        result = load_agent_examples(str(tmp_path))
        assert len(result) == 1
        filename, source = result[0]
        assert filename == "my_agent.py"
        assert source == src


# ---------------------------------------------------------------------------
# load_skill_examples
# ---------------------------------------------------------------------------


class TestLoadSkillExamples:
    def _make_file(self, directory: Path, name: str, content: str) -> None:
        (directory / name).write_text(content, encoding="utf-8")

    def test_returns_up_to_max_examples(self, tmp_path):
        for i in range(5):
            self._make_file(tmp_path, f"skill_{i}.py", f"class S{i}(Skill): pass  # {'x' * i}")
        result = load_skill_examples(str(tmp_path), max_examples=2)
        assert len(result) == 2

    def test_skips_init_py(self, tmp_path):
        (tmp_path / "__init__.py").write_text("# init", encoding="utf-8")
        self._make_file(tmp_path, "my_skill.py", "class MySkill(Skill): pass")
        names = [name for name, _ in load_skill_examples(str(tmp_path))]
        assert "__init__.py" not in names
        assert "my_skill.py" in names

    def test_skips_underscore_files(self, tmp_path):
        (tmp_path / "_helper.py").write_text("# helper", encoding="utf-8")
        self._make_file(tmp_path, "real_skill.py", "class RealSkill(Skill): pass")
        names = [name for name, _ in load_skill_examples(str(tmp_path))]
        assert "_helper.py" not in names

    def test_skips_arm_utils(self, tmp_path):
        self._make_file(tmp_path, "arm_utils.py", "def helper(): pass")
        self._make_file(tmp_path, "speak_aloud.py", "class SpeakAloud(Skill): pass")
        names = [name for name, _ in load_skill_examples(str(tmp_path))]
        assert "arm_utils.py" not in names
        assert "speak_aloud.py" in names

    def test_returns_empty_for_none_dir(self):
        assert load_skill_examples(None) == []

    def test_returns_empty_for_missing_dir(self, tmp_path):
        assert load_skill_examples(str(tmp_path / "nonexistent")) == []

    def test_selects_shortest_files_first(self, tmp_path):
        self._make_file(tmp_path, "long_skill.py", "class Long(Skill): pass" + " " * 500)
        self._make_file(tmp_path, "short_skill.py", "class Short(Skill): pass")
        result = load_skill_examples(str(tmp_path), max_examples=1)
        assert result[0][0] == "short_skill.py"


# ---------------------------------------------------------------------------
# build_prompt (agent)
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    _SPEC = {
        "prompt": "Greet visitors at the door.",
        "new_skills": [{"greet_visitor": "Wave and say hello"}],
    }

    def test_contains_agent_id(self):
        prompt = build_prompt(
            "visitor_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
        )
        assert "visitor_agent" in prompt

    def test_contains_spec_json(self):
        prompt = build_prompt(
            "visitor_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
        )
        assert json.dumps(self._SPEC, indent=2) in prompt

    def test_contains_agent_interface(self):
        interface = "class Agent(ABC): UNIQUE_MARKER = True"
        prompt = build_prompt(
            "visitor_agent", self._SPEC,
            agent_interface=interface,
            agent_examples=[],
        )
        assert interface in prompt

    def test_contains_example_filenames_and_source(self):
        prompt = build_prompt(
            "visitor_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[("basic_agent.py", "class BasicAgent(Agent): pass")],
        )
        assert "basic_agent.py" in prompt
        assert "class BasicAgent(Agent): pass" in prompt

    def test_no_examples_does_not_raise(self):
        prompt = build_prompt(
            "visitor_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
        )
        assert isinstance(prompt, str) and len(prompt) > 0

    def test_camelcase_class_name_in_prompt(self):
        prompt = build_prompt(
            "visitor_greeter_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
        )
        assert "VisitorGreeterAgent" in prompt

    def test_agent_id_in_id_property_rule(self):
        prompt = build_prompt(
            "my_custom_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
        )
        assert '"my_custom_agent"' in prompt


# ---------------------------------------------------------------------------
# build_skill_prompt
# ---------------------------------------------------------------------------


class TestBuildSkillPrompt:
    def test_contains_skill_name(self):
        prompt = build_skill_prompt(
            "greet_visitor", "Wave and say hello",
            skill_interface="class Skill: pass",
            skill_examples=[],
        )
        assert "greet_visitor" in prompt

    def test_contains_description(self):
        prompt = build_skill_prompt(
            "greet_visitor", "Wave and say hello to detected visitors",
            skill_interface="class Skill: pass",
            skill_examples=[],
        )
        assert "Wave and say hello to detected visitors" in prompt

    def test_contains_skill_interface(self):
        interface = "class Skill(ABC): UNIQUE_MARKER = 99"
        prompt = build_skill_prompt(
            "greet_visitor", "desc",
            skill_interface=interface,
            skill_examples=[],
        )
        assert interface in prompt

    def test_contains_example_filenames_and_source(self):
        prompt = build_skill_prompt(
            "greet_visitor", "desc",
            skill_interface="class Skill: pass",
            skill_examples=[("speak_aloud.py", "class SpeakAloud(Skill): pass")],
        )
        assert "speak_aloud.py" in prompt
        assert "class SpeakAloud(Skill): pass" in prompt

    def test_no_examples_does_not_raise(self):
        prompt = build_skill_prompt(
            "my_skill", "do something",
            skill_interface="class Skill: pass",
            skill_examples=[],
        )
        assert isinstance(prompt, str) and len(prompt) > 0

    def test_camelcase_class_name_in_prompt(self):
        prompt = build_skill_prompt(
            "head_emotion", "Express emotion via head tilt",
            skill_interface="class Skill: pass",
            skill_examples=[],
        )
        assert "HeadEmotion" in prompt

    def test_skill_name_in_name_property_rule(self):
        prompt = build_skill_prompt(
            "confirm_delivery", "Confirm delivery time",
            skill_interface="class Skill: pass",
            skill_examples=[],
        )
        assert '"confirm_delivery"' in prompt

    def test_execute_return_type_rule_in_prompt(self):
        prompt = build_skill_prompt(
            "my_skill", "desc",
            skill_interface="class Skill: pass",
            skill_examples=[],
        )
        assert "SkillResult" in prompt


# ---------------------------------------------------------------------------
# _format_existing_skills
# ---------------------------------------------------------------------------


class TestFormatExistingSkills:
    def test_empty_returns_empty_string(self):
        assert _format_existing_skills([]) == ""

    def test_skill_with_description(self):
        result = _format_existing_skills([("wave", "Wave the arm")])
        assert "wave" in result
        assert "Wave the arm" in result

    def test_skill_without_description(self):
        result = _format_existing_skills([("wave", "")])
        assert "wave" in result

    def test_section_header_present(self):
        result = _format_existing_skills([("wave", "desc")])
        assert "EXISTING SKILLS" in result

    def test_multiple_skills(self):
        result = _format_existing_skills([("wave", "Wave"), ("speak", "Say something")])
        assert "wave" in result
        assert "speak" in result


# ---------------------------------------------------------------------------
# build_prompt — existing_skills integration
# ---------------------------------------------------------------------------


class TestBuildPromptExistingSkills:
    _SPEC = {
        "prompt": "Draw a triangle on the floor.",
        "new_skills": [{"draw_triangle": "Draw a triangle"}],
        "existing_skills": ["navigate_to_position", "wave"],
    }

    def test_existing_skills_section_present(self):
        prompt = build_prompt(
            "draw_triangle_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
            existing_skills=[("navigate_to_position", "Navigate to coords"), ("wave", "")],
        )
        assert "EXISTING SKILLS" in prompt
        assert "navigate_to_position" in prompt
        assert "Navigate to coords" in prompt
        assert "wave" in prompt

    def test_no_existing_skills_no_section(self):
        prompt = build_prompt(
            "draw_triangle_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
            existing_skills=[],
        )
        assert "EXISTING SKILLS" not in prompt

    def test_default_existing_skills_is_empty(self):
        # existing_skills defaults to () — no section added
        prompt = build_prompt(
            "draw_triangle_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
        )
        assert "EXISTING SKILLS" not in prompt

    def test_rule_mentions_existing_skills(self):
        prompt = build_prompt(
            "draw_triangle_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=[],
            existing_skills=[("wave", "")],
        )
        assert "existing_skills" in prompt or "existing skills" in prompt.lower()
