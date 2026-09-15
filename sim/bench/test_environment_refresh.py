"""Populated worlds must reset to exact, stable and reachable challenge scenes."""

import math
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import trimesh
from capabilities import needs_move
from mars_sim_driver.challenges import Challenge, ChallengeEngine, Drop, Goal, Hold, InCircle, InRect, WorldState
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.environments import Environment
from mars_sim_driver.props import load_props
from navplan import NavMap

SIM = Path(__file__).resolve().parents[1]
WORLDS = {"rounds": (1, 5), "workshop": (9, 4), "pantry": (9, 4), "counter": (6, 18), "bridge": (0, 3), "blaze": (5, 4)}


@pytest.mark.parametrize("name", WORLDS)
def test_world_population_and_every_challenge_floor_pickup(name, tmp_path):
    mars = VirtualMars(render_wh=(64, 48), environment=Environment.load(name, SIM / "assets"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=tmp_path / "p.json"
    )
    try:
        defaults = {p.name: p for p in mars.props.props.values() if p.initial_pose}
        assert len(defaults) == WORLDS[name][0]
        assert len(engine.challenges) == WORLDS[name][1]
        mars.props.park_all(mars.data)
        nav = NavMap.from_sim(mars)
        for ch in engine.challenges.values():
            assert engine.start(ch.id)
            mars.step(1.5)
            objects = mars.object_poses()
            assert set(objects) == {d.name for d in ch.setup}, ch.id
            for drop in ch.setup:
                prop = mars.props.props[drop.name]
                actual = objects[drop.name]
                assert math.dist(actual[:2], (drop.x, drop.y)) < 0.03, (ch.id, drop.name, actual)
                assert actual[2] >= prop.rest_z - 0.001
                if needs_move(ch, drop.name):
                    # A required pickup must rest on the floor, with a base
                    # approach within the current floor skill's reach.
                    assert actual[2] - prop.rest_z == pytest.approx(0, abs=0.002), (ch.id, drop.name, actual)
                    approaches = [
                        (drop.x + 0.23 * math.cos(a), drop.y + 0.23 * math.sin(a))
                        for a in np.linspace(0, 2 * math.pi, 16, endpoint=False)
                    ]
                    assert any(
                        (route := nav.plan(mars._spawn[:2], target)) is not None
                        and math.dist(route[-1], actual[:2]) < 0.29
                        for target in approaches
                    ), (ch.id, drop.name)
        if name == "blaze":
            # Several deliveries share one porch. They must settle on the
            # floor together, rather than stacking and passing on x/y alone.
            from oracles import plan_for
            from planner_agent import PlannerAgent

            engine.start("blaze_l4")
            ch = engine.challenges["blaze_l4"]
            agent = PlannerAgent(plan_for(ch))
            agent.reset(mars, ch, nav)
            puts = [step for step in agent.steps if step[0] == "put"]
            assert len(puts) == 2 and all(len(step) == 5 for step in puts)
            for _, target, x, y, z in puts:
                mars.drop_prop_at(target, x, y, z=z)
                mars.step(1)
            for _, target, x, y, _ in puts:
                actual = mars.object_poses()[target]
                assert math.dist(actual[:2], (x, y)) < 0.005
                assert actual[2] - mars.props.props[target].rest_z == pytest.approx(0, abs=0.001)
        mars.reset()
        mars.step(1)
        assert set(mars.object_poses()) == set(defaults)
        for key, prop in defaults.items():
            np.testing.assert_allclose(mars.object_poses()[key][:2], prop.initial_pose[:2], atol=0.003)
    finally:
        mars.close()


@pytest.mark.parametrize("name", ["rounds", "workshop", "counter", "blaze"])
def test_new_prop_meshes_match_between_robot_and_browser(name):
    for prop in load_props([SIM / f"bundles/{name}/props"]).values():
        assert prop.texture_path.is_file()
        assert prop.viewer.get("preNormalized")
        robot = trimesh.load(prop.mesh_path, force="mesh", process=False)
        browser = trimesh.load(
            prop.root.parent / "viewer" / prop.viewer["glb"].lstrip("/"), force="mesh", process=False
        )
        assert robot.visual.uv is not None
        assert browser.visual.material.baseColorTexture is not None
        np.testing.assert_allclose(robot.vertices * prop.mesh_scale, browser.vertices, atol=1e-7)
        np.testing.assert_array_equal(robot.faces, browser.faces)


@pytest.mark.parametrize(
    "predicate",
    [InCircle("cup", 0, 0, 0.3, min_z=0.02, max_z=0.04), InRect("cup", -0.3, -0.3, 0.3, 0.3, min_z=0.02, max_z=0.04)],
)
def test_floor_delivery_requires_known_floor_height_and_time(predicate):
    hold = Hold(predicate, 0.75)

    def state(t, height):
        return WorldState(t, (1, 1, 0), {"cup": (0, 0)}, heights={} if height is None else {"cup": height})

    for height in (None, -0.1, 0.2):
        assert not hold.update(state(0, height), [])
        assert not hold.update(state(1, height), [])
    assert not hold.update(state(2, 0.03), [])
    assert hold.update(state(3, 0.03), [])
    hold.reset()
    assert not hold.update(state(4, 0.03), [])


def test_missing_prop_refuses_to_start_without_scoring_the_agent(tmp_path):
    sim = SimpleNamespace(data=SimpleNamespace(time=0), reset=lambda **kw: None, drop_prop_at=lambda *a, **kw: False)
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=tmp_path / "p.json")
    engine.challenges["missing"] = Challenge(
        "missing", "Missing", "Find the can.", [Drop("absent", 0, 0)], [Goal("Can", InCircle("absent", 1, 1, 0.3))]
    )
    assert engine.start("missing") is False
    assert engine.active is None
    assert "missing props: absent" in engine.reason
    assert "missing" not in engine.progress
    sim.drop_prop_at = lambda *a, **kw: True
    assert engine.start("missing") is True
