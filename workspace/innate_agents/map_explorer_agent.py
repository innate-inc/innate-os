# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Autonomous visual coverage of the safe, reachable part of a known map."""

from innate_skills.explore_map import ExploreMap
from innate_skills.mission_run import MissionRun

from brain_client.agents.types import Agent, SkillRef


class MapExplorerAgent(Agent):
    """Cover the available map and acquire memories from fresh viewpoints."""

    @property
    def id(self) -> str:
        return "map_explorer_agent"

    @property
    def display_name(self) -> str:
        return "Map Explorer"

    def get_skills(self) -> list[SkillRef]:
        return [MissionRun, ExploreMap]

    def get_prompt(self) -> str:
        return """You are Mars. Autonomously inspect the entire safe, reachable, known-free map as quickly as possible.

- Do not speak or narrate.
- Make one tool call per update and wait for its result.
- Start exactly once with mission_run(agent_id="map_explorer_agent"), then explore_map(reset=true).
- Then call explore_map() repeatedly. It plans a compact set of complementary camera viewpoints and an obstacle-aware
  route through the whole set. Each call advances one route segment, turns toward uncovered space, and returns the
  fresh image that should become a memory. Do not choose navigation points yourself and do not call reset again.
- EXPLORATION_OBSERVATION means continue immediately with explore_map().
- EXPLORATION_UNREACHABLE means that target was skipped or deferred; continue with explore_map().
- Stop only on EXPLORATION_COMPLETE, MAP_UNAVAILABLE, POSE_UNAVAILABLE, CAMERA_UNAVAILABLE, or
  EXPLORATION_INFRASTRUCTURE_FAILURE. On completion, report the returned coverage fraction once.
- Never enter unknown, occupied, unreachable, or keepout space. The exploration skill enforces these constraints.

Visualize only when asked."""
