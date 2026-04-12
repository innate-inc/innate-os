"""
Unit tests for semantic_skill_analyzer.

All tests run without a live ollama instance or any innate-os/ROS dependency.
ollama.Client is replaced with a MagicMock before the module is imported.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the package root is importable when run from any directory
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_SAMPLE_CAPABILITIES = {
    "demo_agent": {"skills": ["navigate_to_position", "wave"]},
    "chess_agent": {"skills": ["pick_up_piece_simple", "detect_opponent_move"]},
}

_SAMPLE_MISSING = {
    "visitor_greeter_agent": {
        "prompt": "Agent that greets visitors at the door and logs arrival.",
        "new_skills": [
            {"detect_face": "Detect and identify a human face using the camera."},
            {"log_arrival": "Write a timestamped visitor entry to a local log file."},
        ],
    }
}

_SAMPLE_EXISTING = ["demo_agent"]

_FULL_RESULT = {
    "existing_agents": _SAMPLE_EXISTING,
    "missing_capabilities": _SAMPLE_MISSING,
}

_COVERED_RESULT = {
    "existing_agents": ["demo_agent"],
    "missing_capabilities": {},
}

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_tool_call(name: str, arguments) -> SimpleNamespace:
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(function=fn)


def make_tool_response(existing_agents: list, missing_capabilities: dict) -> SimpleNamespace:
    """Simulate an ollama response with a save_capability_analysis tool call."""
    tc = _make_tool_call(
        "save_capability_analysis",
        {
            "existing_agents": existing_agents,
            "missing_capabilities": missing_capabilities,
        },
    )
    msg = SimpleNamespace(tool_calls=[tc], content="")
    return SimpleNamespace(message=msg)


def make_content_response(content: str) -> SimpleNamespace:
    """Simulate an ollama response with plain text and no tool calls."""
    msg = SimpleNamespace(tool_calls=None, content=content)
    return SimpleNamespace(message=msg)


def make_empty_response() -> SimpleNamespace:
    msg = SimpleNamespace(tool_calls=None, content="Sorry, I cannot help.")
    return SimpleNamespace(message=msg)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def caps_file(tmp_path: Path) -> Path:
    f = tmp_path / "capabilities.json"
    f.write_text(json.dumps(_SAMPLE_CAPABILITIES), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run_analyze(response, tmp_path: Path, caps_file: Path, host: str = "http://localhost:11434") -> str:
    mock_client = MagicMock()
    mock_client.chat.return_value = response
    with patch("ollama.Client", return_value=mock_client):
        from semantic_skill_analyzer.analyzer import analyze
        return analyze(
            prompt="test prompt",
            capabilities_path=str(caps_file),
            output_dir=str(tmp_path / "output"),
            ollama_host=host,
        )


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def test_analyze_writes_file(tmp_path, caps_file):
    path = _run_analyze(make_tool_response(_SAMPLE_EXISTING, _SAMPLE_MISSING), tmp_path, caps_file)
    assert Path(path).exists()


def test_analyze_returns_absolute_path(tmp_path, caps_file):
    path = _run_analyze(make_tool_response([], {}), tmp_path, caps_file)
    assert Path(path).is_absolute()


def test_analyze_filename_is_uuid_missing_skills(tmp_path, caps_file):
    path = _run_analyze(make_tool_response([], {}), tmp_path, caps_file)
    name = Path(path).name
    assert name.endswith("-missing-skills.json")
    uuid_part = name.replace("-missing-skills.json", "")
    parsed = uuid.UUID(uuid_part, version=4)
    assert str(parsed) == uuid_part


def test_analyze_creates_output_dir(tmp_path, caps_file):
    nested = tmp_path / "does" / "not" / "exist"
    mock_client = MagicMock()
    mock_client.chat.return_value = make_tool_response([], {})
    with patch("ollama.Client", return_value=mock_client):
        from semantic_skill_analyzer.analyzer import analyze
        analyze(prompt="test", capabilities_path=str(caps_file), output_dir=str(nested))
    assert nested.exists()


def test_multiple_calls_produce_unique_filenames(tmp_path, caps_file):
    paths = {_run_analyze(make_tool_response([], {}), tmp_path, caps_file) for _ in range(3)}
    assert len(paths) == 3


# ---------------------------------------------------------------------------
# Output format — existing_agents + missing_capabilities
# ---------------------------------------------------------------------------


def test_output_contains_existing_agents_key(tmp_path, caps_file):
    path = _run_analyze(make_tool_response(_SAMPLE_EXISTING, {}), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "existing_agents" in data


def test_output_contains_missing_capabilities_key(tmp_path, caps_file):
    path = _run_analyze(make_tool_response([], _SAMPLE_MISSING), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "missing_capabilities" in data


def test_output_existing_agents_populated(tmp_path, caps_file):
    path = _run_analyze(make_tool_response(["demo_agent"], {}), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["existing_agents"] == ["demo_agent"]


def test_output_missing_capabilities_populated(tmp_path, caps_file):
    path = _run_analyze(make_tool_response([], _SAMPLE_MISSING), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "visitor_greeter_agent" in data["missing_capabilities"]


def test_existing_agents_filtered_to_valid_ids(tmp_path, caps_file):
    """existing_agents containing skill descriptions or junk is stripped to real agent IDs only."""
    tc = _make_tool_call(
        "save_capability_analysis",
        {
            "existing_agents": [
                "demo_agent",                                          # valid — keep
                "Use when you need to navigate the robot...",          # skill description — remove
                "Wave hello at the person standing there.",            # skill description — remove
                "nonexistent_agent",                                   # unknown — remove
            ],
            "missing_capabilities": {},
        },
    )
    msg = SimpleNamespace(tool_calls=[tc], content="")
    response = SimpleNamespace(message=msg)
    path = _run_analyze(response, tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["existing_agents"] == ["demo_agent"]


def test_skill_name_removed_from_missing(tmp_path, caps_file):
    """Skill basenames listed as agent names in missing_capabilities are stripped."""
    # "wave" and "navigate_to_position" are skills in _SAMPLE_CAPABILITIES, not agents
    hallucinated = {
        "wave": {"prompt": "...", "new_skills": []},
        "navigate_to_position": {"prompt": "...", "new_skills": []},
        "truly_new_agent": {"prompt": "A real new agent.", "new_skills": []},
    }
    path = _run_analyze(make_tool_response([], hallucinated), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "wave" not in data["missing_capabilities"]
    assert "navigate_to_position" not in data["missing_capabilities"]
    assert "truly_new_agent" in data["missing_capabilities"]


def test_skill_with_description_basename_removed_from_missing(tmp_path, caps_file):
    """Skills stored as 'basename: description' — basename must still be recognised."""
    caps_with_desc = {
        "demo_agent": {"skills": ["wave: Make the robot wave its arm."]},
    }
    caps_path = tmp_path / "caps_desc.json"
    caps_path.write_text(json.dumps(caps_with_desc), encoding="utf-8")
    hallucinated = {"wave": {"prompt": "...", "new_skills": []}}
    mock_client = MagicMock()
    mock_client.chat.return_value = make_tool_response([], hallucinated)
    with patch("ollama.Client", return_value=mock_client):
        from semantic_skill_analyzer.analyzer import analyze
        path = analyze(
            prompt="test",
            capabilities_path=str(caps_path),
            output_dir=str(tmp_path / "out"),
        )
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "wave" not in data["missing_capabilities"]


def test_existing_agent_removed_from_missing(tmp_path, caps_file):
    """
    The LLM sometimes lists an existing agent in missing_capabilities.
    It must be stripped out deterministically — the capabilities index is ground truth.
    """
    # chess_agent already exists in _SAMPLE_CAPABILITIES
    hallucinated_missing = {
        "chess_agent": {"prompt": "...", "new_skills": []},  # already exists — must be removed
        "brand_new_agent": {"prompt": "A genuinely new agent.", "new_skills": []},
    }
    path = _run_analyze(make_tool_response([], hallucinated_missing), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "chess_agent" not in data["missing_capabilities"]
    assert "brand_new_agent" in data["missing_capabilities"]


def test_output_fully_covered_scenario(tmp_path, caps_file):
    """When existing agents cover everything, missing_capabilities is empty."""
    path = _run_analyze(make_tool_response(["demo_agent", "chess_agent"], {}), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["existing_agents"] == ["demo_agent", "chess_agent"]
    assert data["missing_capabilities"] == {}


# ---------------------------------------------------------------------------
# Tool-call result extraction
# ---------------------------------------------------------------------------


def test_extract_tool_result_agent_as_dict(tmp_path, caps_file):
    """Model sometimes returns existing_agents as [{"name": "demo_agent"}] — must normalize."""
    tc = _make_tool_call(
        "save_capability_analysis",
        {"existing_agents": [{"name": "demo_agent"}], "missing_capabilities": {}},
    )
    msg = SimpleNamespace(tool_calls=[tc], content="")
    response = SimpleNamespace(message=msg)
    path = _run_analyze(response, tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["existing_agents"] == ["demo_agent"]


def test_extract_tool_result_string_args(tmp_path, caps_file):
    """Tool arguments as JSON string (not dict) must be parsed."""
    tc = _make_tool_call(
        "save_capability_analysis",
        json.dumps({"existing_agents": ["demo_agent"], "missing_capabilities": {}}),
    )
    msg = SimpleNamespace(tool_calls=[tc], content="")
    response = SimpleNamespace(message=msg)
    path = _run_analyze(response, tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["existing_agents"] == ["demo_agent"]


def test_extract_tool_result_no_matching_tool(tmp_path, caps_file):
    """Unknown tool name → fallback to empty result."""
    tc = _make_tool_call("some_other_tool", {"foo": "bar"})
    msg = SimpleNamespace(tool_calls=[tc], content="")
    response = SimpleNamespace(message=msg)
    path = _run_analyze(response, tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data == {"existing_agents": [], "missing_capabilities": {}}


# ---------------------------------------------------------------------------
# Content fallback
# ---------------------------------------------------------------------------


def test_analyze_content_fallback_full_envelope(tmp_path, caps_file):
    content = json.dumps(_FULL_RESULT)
    path = _run_analyze(make_content_response(content), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["existing_agents"] == _SAMPLE_EXISTING
    assert "visitor_greeter_agent" in data["missing_capabilities"]


def test_analyze_content_fallback_legacy_flat(tmp_path, caps_file):
    """Legacy format {agent_name: {...}} — wrapped under missing_capabilities."""
    content = json.dumps(_SAMPLE_MISSING)
    path = _run_analyze(make_content_response(content), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "visitor_greeter_agent" in data["missing_capabilities"]
    assert data["existing_agents"] == []


def test_analyze_content_fallback_strips_markdown_fences(tmp_path, caps_file):
    content = f"```json\n{json.dumps(_FULL_RESULT)}\n```"
    path = _run_analyze(make_content_response(content), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "existing_agents" in data


def test_analyze_content_fallback_strips_plain_fences(tmp_path, caps_file):
    content = f"```\n{json.dumps(_FULL_RESULT)}\n```"
    path = _run_analyze(make_content_response(content), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "existing_agents" in data


def test_analyze_content_fallback_strips_think_blocks(tmp_path, caps_file):
    """<think>…</think> from qwen3 thinking mode must be stripped before JSON parse."""
    content = f"<think>\nLet me reason about this carefully.\n</think>\n{json.dumps(_FULL_RESULT)}"
    path = _run_analyze(make_content_response(content), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert "existing_agents" in data
    assert data["existing_agents"] == _SAMPLE_EXISTING


def test_analyze_empty_on_bad_response(tmp_path, caps_file):
    path = _run_analyze(make_empty_response(), tmp_path, caps_file)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data == {"existing_agents": [], "missing_capabilities": {}}


# ---------------------------------------------------------------------------
# LLM call parameters
# ---------------------------------------------------------------------------


def test_analyze_passes_correct_model(tmp_path, caps_file):
    mock_client = MagicMock()
    mock_client.chat.return_value = make_tool_response([], {})
    with patch("ollama.Client", return_value=mock_client):
        from semantic_skill_analyzer.analyzer import analyze, MODEL
        analyze(prompt="test", capabilities_path=str(caps_file), output_dir=str(tmp_path / "out"))
    call_kwargs = mock_client.chat.call_args[1]
    assert call_kwargs.get("model") == MODEL


def test_analyze_passes_tool_schema(tmp_path, caps_file):
    mock_client = MagicMock()
    mock_client.chat.return_value = make_tool_response([], {})
    with patch("ollama.Client", return_value=mock_client):
        from semantic_skill_analyzer.analyzer import analyze, _TOOL
        analyze(prompt="test", capabilities_path=str(caps_file), output_dir=str(tmp_path / "out"))
    tools_arg = mock_client.chat.call_args[1].get("tools")
    assert tools_arg == [_TOOL]


def test_analyze_disables_thinking_mode(tmp_path, caps_file):
    """think=False must be passed to suppress qwen3 <think> blocks."""
    mock_client = MagicMock()
    mock_client.chat.return_value = make_tool_response([], {})
    with patch("ollama.Client", return_value=mock_client):
        from semantic_skill_analyzer.analyzer import analyze
        analyze(prompt="test", capabilities_path=str(caps_file), output_dir=str(tmp_path / "out"))
    assert mock_client.chat.call_args[1].get("think") is False


def test_analyze_user_message_contains_prompt(tmp_path, caps_file):
    mock_client = MagicMock()
    mock_client.chat.return_value = make_tool_response([], {})
    user_prompt = "unique-scenario-xyz-99"
    with patch("ollama.Client", return_value=mock_client):
        from semantic_skill_analyzer.analyzer import analyze
        analyze(prompt=user_prompt, capabilities_path=str(caps_file), output_dir=str(tmp_path / "out"))
    messages = mock_client.chat.call_args[1]["messages"]
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert any(user_prompt in m["content"] for m in user_msgs)


def test_analyze_user_message_contains_capabilities(tmp_path, caps_file):
    mock_client = MagicMock()
    mock_client.chat.return_value = make_tool_response([], {})
    with patch("ollama.Client", return_value=mock_client):
        from semantic_skill_analyzer.analyzer import analyze
        analyze(prompt="test", capabilities_path=str(caps_file), output_dir=str(tmp_path / "out"))
    messages = mock_client.chat.call_args[1]["messages"]
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert any("demo_agent" in m["content"] for m in user_msgs)


def test_analyze_custom_ollama_host(tmp_path, caps_file):
    custom_host = "http://192.168.1.50:11434"
    with patch("ollama.Client") as MockClient:
        MockClient.return_value.chat.return_value = make_tool_response([], {})
        from semantic_skill_analyzer.analyzer import analyze
        analyze(prompt="test", capabilities_path=str(caps_file), output_dir=str(tmp_path / "out"), ollama_host=custom_host)
    MockClient.assert_called_once_with(host=custom_host)


# ---------------------------------------------------------------------------
# load_capabilities
# ---------------------------------------------------------------------------


def test_load_capabilities_returns_dict(tmp_path):
    caps = {"agent_a": {"skills": ["skill_1"]}}
    f = tmp_path / "capabilities.json"
    f.write_text(json.dumps(caps), encoding="utf-8")
    from semantic_skill_analyzer.analyzer import load_capabilities
    assert load_capabilities(str(f)) == caps


def test_load_capabilities_returns_correct_structure(tmp_path):
    f = tmp_path / "capabilities.json"
    f.write_text(json.dumps(_SAMPLE_CAPABILITIES), encoding="utf-8")
    from semantic_skill_analyzer.analyzer import load_capabilities
    result = load_capabilities(str(f))
    assert result["demo_agent"]["skills"] == ["navigate_to_position", "wave"]
