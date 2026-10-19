"""pick_any_object and open_gripper, as the probe's robot has them.

Played levels found the old harness pick taking whatever was nearest in a
circle -- once behind the robot -- and naming it by id ("workshop_gauge_040"
ranks the oil cans by size). The real skill finds the object the prompt
describes with the head camera, picks only off the floor, and reports what it
was asked for. These run the stand-in on real worlds (innate_roster.py says
what stands in for Gemini).
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

from types import SimpleNamespace

import innate_roster as R
from backends import EchoBackend
from brain_agent import BrainAgent, _kind
from runner import run_episode


def _play(world, challenge, script, seconds):
    held = {}

    def make(ch):
        agent = BrainAgent(EchoBackend(script), max_turns=len(script))
        agent.name = "brain:echo"
        held["agent"] = agent
        return agent

    ep = run_episode(world, challenge, make, max_sim_s=seconds, render_wh=(64, 48))
    return ep, held["agent"]


def _results(agent):
    return [turn.result for turn in agent.log]


def test_a_floor_jar_dead_ahead_is_picked_and_counts_as_the_skill():
    ep, agent = _play(
        "counter", "counter_floor_within_reach", [{"action": "pick_any_object", "args": {"prompt": "the jar"}}], 60
    )
    assert agent._carrying == "counter_jar_jam"
    assert ep.goals_done >= 1, "SkillDone('pick_any_object') did not latch"


def test_the_prompt_chooses_between_cups():
    """Red, blue and green cups on the floor: 'the red cup' gets the red one,
    which is not the one dead ahead."""
    ep, agent = _play(
        "counter", "counter_floor_serve", [{"action": "pick_any_object", "args": {"prompt": "the red cup"}}], 60
    )
    assert agent._carrying == "counter_cup_red"
    assert ep.goals_done >= 2  # reached it, picked it


def test_a_cup_on_the_counter_is_not_a_floor_pick():
    _ep, agent = _play(
        "counter", "counter_within_reach", [{"action": "pick_any_object", "args": {"prompt": "the cup"}}], 60
    )
    assert agent._carrying is None


def test_open_gripper_drops_what_is_held_and_waits_for_it_to_land():
    agent = BrainAgent(SimpleNamespace(wants_image=False))
    dropped = []

    class Mars:
        def pose(self):
            return (1.0, 2.0, 0.0)

        def set_cmd_vel(self, v, w):
            pass

        def drop_prop_at(self, name, x, y):
            dropped.append((name, round(x, 2), round(y, 2)))

    mars = Mars()
    agent._carrying = "counter_cup_red"
    agent._dispatch(mars, 0.0, "open_gripper", {})
    t = 0.0
    while agent._skill is not None:
        t += 0.05
        agent._step_skill(mars, t)
    assert dropped == [("counter_cup_red", 1.0 + R.DROP_AHEAD_M, 2.0)]
    assert t >= R.DROP_SETTLE_S, "the next frame would show the cup still falling"
    assert agent._events == ["Skill open_gripper completed: Gripper opened"]


def test_what_the_camera_can_tell_never_includes_the_id():
    desc = R.descriptor("pantry_jar_stray", "Jar Stray", _kind("pantry_jar_stray"))
    assert R.match_score("the stray jar", desc) == 1.0  # a jar, nothing more
    oil = R.descriptor("workshop_gauge_040", "Small oil can", _kind("workshop_gauge_040"))
    assert R.match_score("the paint can", oil) is None
    assert R.match_score("the oil can", oil) == 3.0


def test_colour_rules_out_the_wrong_cup():
    red = R.descriptor("counter_cup_red", "Cup Red", _kind("counter_cup_red"))
    assert R.match_score("the blue cup", red) is None
    assert R.match_score("a red mug", red) == 3.0


def test_a_person_is_not_graspable():
    person = SimpleNamespace(kinematic=True, size=(0.25, 0.85, 0.15))
    cup = SimpleNamespace(kinematic=False, size=(0.03, 0.03, 0.04))
    mars = SimpleNamespace(props=SimpleNamespace(props={"human": person, "cup": cup}))
    assert not R.graspable(mars, "human")
    assert R.graspable(mars, "cup")


def test_a_released_jar_lands_upright_and_stays():
    """Released from the prop's drop_z (0.19 m, a shelf-drop height), the
    pantry jar landed on its side and rolled under the shelf, out of reach."""
    import math
    from pathlib import Path

    from mars_sim_driver.core import VirtualMars
    from mars_sim_driver.environments import Environment

    mars = VirtualMars(
        render_wh=(32, 24), environment=Environment.load("pantry", Path(__file__).resolve().parents[1] / "assets")
    )
    try:
        R.move_arm(mars, R.arm_poses()["carry"])
        mars.step(3.0)
        agent = BrainAgent(SimpleNamespace(wants_image=False))
        mars.drop_prop_at("pantry_jar_stray", 0, 0)
        mars.step(0.2)
        R.set_held(mars, "pantry_jar_stray", True)
        agent._carrying = "pantry_jar_stray"
        for _ in range(10):
            agent.act(mars, 0.0)
            mars.step(0.05)
        agent._release(mars)
        mars.step(0.5)
        first = mars.object_poses()["pantry_jar_stray"]
        mars.step(3.0)
        last = mars.object_poses()["pantry_jar_stray"]
        assert abs(last[2] - mars.props.props["pantry_jar_stray"].rest_z) < 0.005, "it is lying on its side"
        assert math.hypot(last[0] - first[0], last[1] - first[1]) < 0.01, "it rolled"
    finally:
        mars.close()
