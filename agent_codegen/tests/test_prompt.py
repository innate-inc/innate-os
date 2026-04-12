"""Tests for agent_codegen.prompt."""
import json
from pathlib import Path

import pytest

from agent_codegen.prompt import (
    _AGENT_INTERFACE_FALLBACK,
    build_prompt,
    load_agent_examples,
    load_agent_interface,
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
        missing = str(tmp_path / "nonexistent.py")
        result = load_agent_interface(missing)
        assert result == _AGENT_INTERFACE_FALLBACK

    def test_fallback_when_path_is_directory(self, tmp_path):
        result = load_agent_interface(str(tmp_path))
        assert result == _AGENT_INTERFACE_FALLBACK

    def test_fallback_contains_agent_abc(self):
        assert "class Agent" in _AGENT_INTERFACE_FALLBACK
        assert "abstractmethod" in _AGENT_INTERFACE_FALLBACK


# ---------------------------------------------------------------------------
# load_agent_examples
# ---------------------------------------------------------------------------


class TestLoadAgentExamples:
    def _make_agent(self, directory: Path, name: str, content: str) -> None:
        (directory / name).write_text(content, encoding="utf-8")

    def test_returns_up_to_max_examples(self, tmp_path):
        for i in range(5):
            self._make_agent(tmp_path, f"agent_{i}.py", f"class A{i}(Agent): pass  # {'x' * i}")
        result = load_agent_examples(str(tmp_path), max_examples=2)
        assert len(result) == 2

    def test_returns_all_when_fewer_than_max(self, tmp_path):
        self._make_agent(tmp_path, "agent_a.py", "class A(Agent): pass")
        result = load_agent_examples(str(tmp_path), max_examples=5)
        assert len(result) == 1

    def test_skips_init_py(self, tmp_path):
        (tmp_path / "__init__.py").write_text("# init", encoding="utf-8")
        self._make_agent(tmp_path, "real_agent.py", "class RealAgent(Agent): pass")
        result = load_agent_examples(str(tmp_path))
        names = [name for name, _ in result]
        assert "__init__.py" not in names
        assert "real_agent.py" in names

    def test_skips_underscore_files(self, tmp_path):
        (tmp_path / "_private.py").write_text("# private", encoding="utf-8")
        self._make_agent(tmp_path, "normal_agent.py", "class NormalAgent(Agent): pass")
        result = load_agent_examples(str(tmp_path))
        names = [name for name, _ in result]
        assert "_private.py" not in names

    def test_skips_orchestrator_agent(self, tmp_path):
        self._make_agent(tmp_path, "orchestrator_agent.py", "class OrchestratorAgent(Agent): pass")
        self._make_agent(tmp_path, "basic_agent.py", "class BasicAgent(Agent): pass")
        result = load_agent_examples(str(tmp_path))
        names = [name for name, _ in result]
        assert "orchestrator_agent.py" not in names
        assert "basic_agent.py" in names

    def test_returns_empty_for_none_dir(self):
        result = load_agent_examples(None)
        assert result == []

    def test_returns_empty_for_missing_dir(self, tmp_path):
        result = load_agent_examples(str(tmp_path / "nonexistent"))
        assert result == []

    def test_selects_shortest_files_first(self, tmp_path):
        self._make_agent(tmp_path, "long_agent.py", "class Long(Agent): pass" + " " * 500)
        self._make_agent(tmp_path, "short_agent.py", "class Short(Agent): pass")
        self._make_agent(tmp_path, "medium_agent.py", "class Medium(Agent): pass" + " " * 100)
        result = load_agent_examples(str(tmp_path), max_examples=2)
        names = [name for name, _ in result]
        assert names[0] == "short_agent.py"
        assert names[1] == "medium_agent.py"

    def test_returns_filename_and_source_pairs(self, tmp_path):
        src = "class MyAgent(Agent): pass"
        self._make_agent(tmp_path, "my_agent.py", src)
        result = load_agent_examples(str(tmp_path))
        assert len(result) == 1
        filename, source = result[0]
        assert filename == "my_agent.py"
        assert source == src


# ---------------------------------------------------------------------------
# build_prompt
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
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_multiple_examples_all_included(self):
        examples = [
            ("agent_a.py", "class AgentA(Agent): pass"),
            ("agent_b.py", "class AgentB(Agent): pass"),
        ]
        prompt = build_prompt(
            "visitor_agent", self._SPEC,
            agent_interface="class Agent: pass",
            agent_examples=examples,
        )
        assert "agent_a.py" in prompt
        assert "agent_b.py" in prompt
        assert "class AgentA" in prompt
        assert "class AgentB" in prompt

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
