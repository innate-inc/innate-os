# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import json
import sys

from innate_skills.explore_map import ExploreMap
from innate_skills.find_next_person import FindNextPerson
from innate_skills.mission_run import MissionRun

from brain_client.agents.types import Agent
from brain_client.skills.types import SkillOutput


def test_map_explorer_agent_uses_only_run_and_exploration_skills():
    saved_registry = dict(Agent._registry)
    from innate_agents.map_explorer_agent import MapExplorerAgent

    agent = MapExplorerAgent()
    try:
        assert agent.id == "map_explorer_agent"
        assert agent.display_name == "Map Explorer"
        assert agent.get_skills() == [MissionRun, ExploreMap]
        assert "navigate" not in " ".join(skill.__name__.lower() for skill in agent.get_skills())
        prompt = agent.get_prompt()
        assert 'mission_run(agent_id="map_explorer_agent")' in prompt
        assert "explore_map(reset=true)" in prompt
        assert "EXPLORATION_OBSERVATION" in prompt
        assert "EXPLORATION_COMPLETE" in prompt
        assert "unknown, occupied, unreachable, or keepout" in prompt
    finally:
        Agent._registry.clear()
        Agent._registry.update(saved_registry)
        sys.modules.pop("innate_agents.map_explorer_agent", None)


def test_explore_map_translates_search_contract(monkeypatch):
    source = SkillOutput(
        'SEARCH_EXHAUSTED {"coverage_fraction":0.97}',
        data={"coverage_fraction": 0.97},
        image=b"jpeg",
    )
    monkeypatch.setattr(FindNextPerson, "execute", lambda self, **_kwargs: source)

    output = ExploreMap(None).execute()

    assert output.message == 'EXPLORATION_COMPLETE {"coverage_fraction":0.97}'
    assert output.data == {"coverage_fraction": 0.97}
    assert output.image == b"jpeg"


def test_mission_run_records_requesting_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))

    output = MissionRun(None).execute(agent_id="map_explorer_agent")

    payload = output.data
    assert payload["agent_id"] == "map_explorer_agent"
    run_file = tmp_path / "workspace/skill_storage/household_orders/runs" / payload["run_id"] / "run.json"
    assert json.loads(run_file.read_text())["agent_id"] == "map_explorer_agent"
