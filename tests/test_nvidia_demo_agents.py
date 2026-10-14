"""Real shipped-agent discovery in a separate process, without starting ROS nodes.

Discovery evicts workspace modules. Keeping it in a subprocess prevents stale
class identities in other skill tests and avoids depending on user custom agents.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def inspect_roster():
    import logging
    sys.path[:0] = [str(ROOT / "workspace"), str(ROOT / "ros2_ws/src/brain/brain_client")]
    os.environ["INNATE_OS_ROOT"] = str(ROOT)
    from brain_client.agents import loader
    from brain_client.skills.workspace_import import registered_workspace_skills

    # Custom agents are operator-owned and can have unrelated dependencies.
    loader.get_custom_agents_dir = lambda: ROOT / "tests/nonexistent_custom_agents"
    logger = logging.getLogger(__name__)
    classes, errors = loader.discover_agent_classes(logger)
    assert not errors, errors
    skills, rejected = registered_workspace_skills(logger)
    assert not rejected, rejected
    # Physical refs are backed by metadata; deployment checks must also confirm
    # recording files, which are intentionally not bundled into these tests.
    available = dict(skills)
    for package, namespace in (("innate_skills", "innate-os"), ("custom_skills", "local")):
        for metadata in (ROOT / "workspace" / package).rglob("metadata.json"):
            available[f"{namespace}/{metadata.parent.name}"] = json.loads(metadata.read_text())
    agents, broken = loader.build_agent_instances(classes, logger, available_skills=available)
    assert not broken, broken
    return {name: {
        "skills": agent.skill_ids(), "initial_skills": agent.initial_skill_ids(),
        "model": agent.model, "extras": agent.model_extra_body,
        "minimal": agent.minimal_system_prompt, "wait": agent.wait_for_skill_completion,
        "history": agent.history_max_entries, "image_history": agent.history_max_image_turns,
        "inputs": agent.input_names(), "gaze": agent.uses_gaze(),
        "idle": agent.idle_turn_interval, "supervision": agent.supervision_turn_interval,
        "prompt": agent.get_prompt(), "mro": [c.__name__ for c in type(agent).__mro__],
    } for name, agent in agents.items()}


@pytest.fixture(scope="module")
def roster():
    result = subprocess.run([sys.executable, __file__, "--inspect"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("agent_id,pick,drop", [
    ("qwen_socks_demo_3", "pick_sock_pile_yoloe", "drop_in_box_aruco"),
    ("qwen_socks_demo_3_features", "pick_sock_pile_yoloe_features", "drop_in_box_features"),
])
def test_supported_agents_resolve_exact_tools_and_preserve_behavior(roster, agent_id, pick, drop):
    agent = roster[agent_id]
    assert agent["skills"] == [f"innate-os/{pick}", f"innate-os/{drop}", "innate-os/wave", "innate-os/turn_in_place"]
    assert agent["initial_skills"] == agent["skills"]
    assert agent["model"] == "openai-chat:Qwen3.8-Flash-Next"
    assert json.loads(agent["extras"]) == {"chat_template_kwargs": {"enable_thinking": False}}
    assert agent["minimal"] and agent["wait"]
    assert agent["history"] == 12 and agent["image_history"] == 0
    assert agent["inputs"] == [] and not agent["gaze"]
    assert agent["idle"] is None and agent["supervision"] is None
    prompt = agent["prompt"]
    assert pick in prompt and drop in prompt
    assert "never drop after failed pickup" in prompt
    assert "On failure, wait for instructions" in prompt
    assert "stop" in prompt and "Wave" in prompt and "No narration" in prompt
    if agent_id.endswith("_features"):
        assert "drop_in_box_aruco" not in prompt


def test_demo_three_no_longer_inherits_retired_agents(roster):
    assert roster["qwen_socks_demo_3"]["mro"] == ["QwenSocksDemo3", "Agent", "ABC", "object"]



if __name__ == "__main__" and sys.argv[1:] == ["--inspect"]:
    print(json.dumps(inspect_roster()))
