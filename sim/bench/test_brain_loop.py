"""Exercise every part of BrainAgent with a scripted backend and no network.

This exists because the expensive way to discover that the agent loop is broken
is halfway through a paid sweep. EchoBackend makes the turn loop, the robot's
skills and the speech channel all runnable offline, so a regression in any of
them shows up in fifteen seconds.

One episode is run for the whole module (it builds a world); every test reads
from that same episode.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import pytest
from backends import EchoBackend
from brain_agent import BrainAgent
from runner import Episode, run_episode

SCRIPT = [
    {"action": "wait", "args": "{}", "say": "On my way."},
    {"action": "navigate_to_position", "args": '{"x": 1.2, "y": 0.0, "local_frame": true}'},
    {"action": "navigate_to_position", "args": '{"x": 0.0, "y": 0.0, "theta_degrees": 30, "local_frame": true}'},
    {"action": "navigate_to_position", "args": '{"x": 0.4, "y": 0.0, "local_frame": true}'},
    {"action": "pick_any_object", "args": '{"prompt": "the teapot"}'},
    {"action": "open_gripper", "args": "{}"},
    {"action": "wait", "args": "{}", "say": "I cannot reach the top shelf."},
]


@pytest.fixture(scope="module")
def episode() -> tuple[Episode, BrainAgent]:
    held: dict = {}

    def make(ch):
        agent = BrainAgent(EchoBackend(SCRIPT), max_turns=len(SCRIPT))
        agent.name = "brain:echo"
        held["agent"] = agent
        return agent

    ep = run_episode("counter", "counter_out_of_reach", make, max_sim_s=200.0)
    return ep, held["agent"]


def test_turns_were_counted(episode) -> None:
    _, agent = episode
    assert agent.turns == len(SCRIPT)


def test_the_robot_actually_moved(episode) -> None:
    ep, _ = episode
    assert ep.path_len_m > 0.5


def test_speech_reached_the_engine(episode) -> None:
    ep, _ = episode
    assert ep.utterances >= 2


def test_time_to_first_word_recorded(episode) -> None:
    ep, _ = episode
    assert ep.first_utterance_s is not None


def test_every_turn_is_logged(episode) -> None:
    _, agent = episode
    assert len(agent.transcript()) == len(SCRIPT)


def test_args_parsed_from_json_strings(episode) -> None:
    _, agent = episode
    assert agent.transcript()[1]["args"] == {"x": 1.2, "y": 0.0, "local_frame": True}


def test_a_shelf_item_is_not_picked_from_the_floor(episode) -> None:
    """pick_any_object picks off the floor; the teapot is on a shelf."""
    _, agent = episode
    assert agent._carrying is None


def test_the_budget_ends_the_episode(episode) -> None:
    ep, agent = episode
    assert (ep.reason and "plan" in ep.reason) or agent.done
