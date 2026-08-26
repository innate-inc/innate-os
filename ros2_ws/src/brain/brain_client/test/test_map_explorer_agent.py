# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import json
import sys

import numpy as np
from innate_skills.explore_map import ExploreMap, _plan_route
from innate_skills.find_next_person import (
    FindNextPerson,
    _choose_view,
    _grid_travel_distances,
    _PlanningGrid,
)
from innate_skills.mission_run import MissionRun

from brain_client.agents.types import Agent
from brain_client.skills.types import SkillOutput, SkillStorage
from brain_client.state.pose import Pose


def _room_plan() -> _PlanningGrid:
    traversable = np.ones((18, 28), dtype=bool)
    traversable[[0, -1], :] = False
    traversable[:, [0, -1]] = False
    # A dividing wall with one doorway forces route distance to respect topology.
    traversable[1:14, 14] = False
    blocked = ~traversable
    return _PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=traversable,
        blocked=blocked,
        safe=traversable.copy(),
        reachable=traversable.copy(),
        navigable=traversable.copy(),
    )


def _route_length(plan: _PlanningGrid, start: Pose, route) -> float:
    distance = 0.0
    pose = start
    for view in route:
        distance += float(_grid_travel_distances(plan, pose)[view.row, view.col])
        pose = Pose(view.x, view.y, view.theta)
    return distance


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


def test_global_route_covers_room_with_small_complementary_view_set():
    plan = _room_plan()
    pose = Pose(0.625, 0.625, 0.0)

    route = _plan_route(plan, pose, set(), [])

    covered = set().union(*(view.visible for view in route))
    reachable = set(np.flatnonzero((plan.traversable & plan.reachable).reshape(-1)).tolist())
    assert len(covered & reachable) / len(reachable) >= 0.95
    assert len(route) < 20
    assert all(plan.navigable[view.row, view.col] for view in route)


def test_global_route_removes_stops_and_shortens_greedy_route_without_losing_coverage():
    plan = _room_plan()
    start = Pose(0.625, 0.625, 0.0)
    pose = start
    observations = []
    greedy_route = []
    while True:
        view, _ = _choose_view(plan, pose, observations, [])
        if view is None:
            break
        greedy_route.append(view)
        observations.append(
            {
                "x": view.x,
                "y": view.y,
                "theta": view.theta,
                "target_x": view.x,
                "target_y": view.y,
                "stopped_short": False,
            }
        )
        pose = Pose(view.x, view.y, view.theta)

    optimized = _plan_route(plan, start, set(), [])

    assert set().union(*(view.visible for view in optimized)) == set().union(*(view.visible for view in greedy_route))
    assert len(optimized) < len(greedy_route)
    assert _route_length(plan, start, optimized) < _route_length(plan, start, greedy_route)


def test_explorer_persists_whole_route_and_advances_one_segment(tmp_path):
    plan = _room_plan()
    pose = Pose(0.625, 0.625, 0.0)
    skill = ExploreMap(None)
    skill._storage = SkillStorage(tmp_path / "explore_map.json")
    state = {
        "observations": [],
        "unreachable": [],
        "navigation_failures": [],
        "planned_route": [],
    }

    first, covered = skill._select_view(plan, pose, state)

    assert first is not None
    assert covered == set()
    assert len(state["planned_route"]) > 1
    route_length = len(state["planned_route"])
    skill._complete_selected_view(state, first)
    assert len(state["planned_route"]) == route_length - 1
