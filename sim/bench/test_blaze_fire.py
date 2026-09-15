"""Fire cues follow the judge, are visible to RGB, and never become physics."""

from pathlib import Path

import mujoco
import numpy as np
import pytest
from mars_sim_driver.challenges import After, AnyOf, Challenge, InRect, load_challenges
from mars_sim_driver.fire import FireEffect, flame_triangles


@pytest.mark.parametrize(
    "challenge",
    list(load_challenges([Path(__file__).parents[1] / "bundles/blaze/challenges"]).values()),
    ids=lambda c: c.id,
)
def test_fire_follows_each_challenges_real_deadlines(challenge):
    fire = FireEffect(True)
    fire.sync(challenge, 0)
    starts = 2 if challenge.id == "blaze_l4" else 1
    assert len(fire.sources) == starts
    assert all(s[3] == pytest.approx(0.38) for s in fire.sources)
    for after in challenge.fail_if.preds:
        fire.sync(challenge, after.seconds)
        r = after.inner
        in_region = [s for s in fire.sources if r.x0 <= s[0] <= r.x1 and r.y0 <= s[1] <= r.y1]
        assert len(in_region) == 7
        assert all(s[3] == 1 for s in in_region)
    fire.sync(None, 0)
    assert fire.sources == [[-1.30, 1.96, 0.25, 0.38, 0]]


def test_fire_uses_edited_predicate_deadline_and_stays_inside_the_room():
    challenge = Challenge(
        id="custom",
        title="custom",
        brief="custom",
        setup=[],
        goals=[],
        fail_if=AnyOf([After(20, InRect("robot", -3.2, 0.7, -0.35, 2.3))]),
    )
    fire = FireEffect(True)
    fire.sync(challenge, 20)
    assert len(fire.sources) == 7
    assert all(s[3] == 1 for s in fire.sources)
    assert all(-3.2 <= s[0] <= -0.35 and 0.7 <= s[1] <= 2.3 for s in fire.sources)
    off = FireEffect(False)
    off.sync(challenge, 30)
    assert off.public() is None


def test_rgb_flame_geometry_is_animated_and_does_not_enter_the_model():
    model = mujoco.MjModel.from_xml_string('<mujoco><worldbody><geom type="plane" size="5 5 .1"/></worldbody></mujoco>')
    data = mujoco.MjData(model)
    scene = mujoco.MjvScene(model, maxgeom=500)
    camera = mujoco.MjvCamera()
    mujoco.mjv_updateScene(model, data, mujoco.MjvOption(), None, camera, mujoco.mjtCatBit.mjCAT_ALL, scene)
    physics_count, rendered_count = model.ngeom, scene.ngeom
    fire = FireEffect(True)
    fire.draw(scene, 2)
    assert scene.ngeom > rendered_count
    assert model.ngeom == physics_count
    assert not data.ncon
    assert all(g.category == mujoco.mjtCatBit.mjCAT_DECOR for g in scene.geoms[rendered_count : scene.ngeom])
    a = np.array([p for tri in flame_triangles(fire.sources[0], 1) for p in tri[:3]])
    b = np.array([p for tri in flame_triangles(fire.sources[0], 1.2) for p in tri[:3]])
    assert np.isfinite(a).all()
    assert not np.allclose(a, b)
    fire.draw(scene, 2)  # respects a renderer's finite extra-geometry budget
    fire.draw(scene, 2)
    fire.draw(scene, 2)
    fire.draw(scene, 2)
    assert scene.ngeom <= scene.maxgeom


def test_retry_abort_and_external_reset_restore_the_fire(tmp_path):
    import threading

    from mars_sim_driver.challenges import ChallengeEngine
    from mars_sim_driver.core import VirtualMars
    from mars_sim_driver.environments import Environment

    mars = VirtualMars(render_wh=(64, 48), environment=Environment.load("blaze"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=tmp_path / "p.json"
    )
    try:
        mars.data.time = 30
        mars.step(0)
        assert len(mars.fire.sources) > 1  # free play advances without a judge
        assert engine.start("blaze_l4")
        assert len(mars.fire.sources) == 2
        mars.data.time = 100
        engine.tick(100, (0, 0, 0), mars.object_centers(), engine.world_epoch)
        assert len(mars.fire.sources) > 2
        engine.tick(101, (2, -1, 0), mars.object_centers(), engine.world_epoch)
        assert engine.state == "failed"
        frozen = mars.fire.sources
        engine.tick(450, (0, 0, 0), mars.object_centers(), engine.world_epoch)
        assert mars.fire.sources == frozen
        mars.reset()
        engine.tick(0, (0, 0, 0), mars.object_centers(), engine.world_epoch)
        assert len(mars.fire.sources) == 1
        assert engine.start("blaze_l4")
        assert len(mars.fire.sources) == 2
        engine.abort()
        assert len(mars.fire.sources) == 1
    finally:
        mars.close()


def test_free_play_spreads_on_sim_time_and_abort_restarts_from_current_time():
    fire = FireEffect(True)
    initial = fire.sources
    fire.reset(1000)
    fire.advance(1000)
    assert fire.sources == initial
    fire.advance(1030)
    assert len(fire.sources) > len(initial)
    assert fire.sources[0][3] > initial[0][3]
    fire.advance(1300)
    assert len(fire.sources) == 28
    assert all(s[3] == 1 for s in fire.sources)
    assert all(s[1] > -2.4 for s in fire.sources)  # porch always clear
    fire.reset(1300)
    fire.advance(1301)
    assert len(fire.sources) == 1 and fire.sources[0][3] < 0.4
    fire.reset()
    assert fire.sources == initial


def test_preview_cannot_override_a_challenge_and_fire_seeds_do_not_jump():
    challenge = load_challenges([Path(__file__).parents[1] / "bundles/blaze/challenges"])["blaze_l4"]
    fire = FireEffect(True)
    fire.advance(170)
    fire.sync(challenge, 0)
    initial = fire.sources
    fire.advance(900)
    assert fire.sources == initial
    fire.sync(challenge, 20)
    earlier = {tuple(s[:3]): s for s in fire.sources}
    fire.sync(challenge, 40)
    later = {tuple(s[:3]): s for s in fire.sources}
    assert len(later) > len(earlier)
    for position, source in earlier.items():
        assert later[position][3] >= source[3]
        assert later[position][4] == source[4]


def test_new_flame_patches_start_small():
    def height(strength):
        return max(p[2] for tri in flame_triangles((0, 0, 0, strength, 0), 1) for p in tri[:3])

    assert height(0.00001) < 0.003
    assert height(0.01) < height(0.25) < height(1)


@pytest.mark.parametrize("elapsed", [0, 30, 75, 150, 180, 210, 240, 270, 285, 299, 300])
def test_five_minute_timer_drives_the_same_spread_as_free_play(elapsed):
    challenge = load_challenges([Path(__file__).parents[1] / "bundles/blaze/challenges"])["blaze_l1"]
    assert challenge.time_limit_s == 300
    preview, running = FireEffect(True), FireEffect(True)
    preview.advance(elapsed)
    running.sync(challenge, elapsed)
    assert preview.sources == running.sources
    if elapsed < 300:
        assert len(running.sources) < 28 or any(s[3] < 1 for s in running.sources)
    else:
        assert len(running.sources) == 28
        assert all(s[3] == 1 for s in running.sources)


def test_medicine_rescue_has_time_for_search_and_pickup_delays(tmp_path):
    """Exercise the real route/judge with 195 s of extra thinking/pick time.

    Object placement is still oracle-assisted; this is a timing and escape
    route regression, not proof that the live vision/arm pipeline succeeds.
    """
    import threading

    from mars_sim_driver.challenges import ChallengeEngine, WorldState
    from mars_sim_driver.core import VirtualMars
    from mars_sim_driver.environments import Environment
    from navplan import NavMap
    from oracles import plan_for
    from planner_agent import PlannerAgent

    mars = VirtualMars(render_wh=(64, 48), environment=Environment.load("blaze"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=tmp_path / "p.json"
    )
    try:
        mars.props.park_all(mars.data)
        nav = NavMap.from_sim(mars)
        assert engine.start("blaze_l1", chat_cues=False)
        challenge = engine.active
        steps = [("wait", 105)]
        for step in plan_for(challenge):
            if step[0] == "grab":
                steps.append(("wait", 90))
            steps.append(step)
        agent = PlannerAgent(steps)
        agent.reset(mars, challenge, nav)
        exit_route = []
        reached_medicine = False
        while engine.state == "running":
            t = float(mars.data.time)
            agent.act(mars, t)
            mars.step(0.05)
            t, pose = float(mars.data.time), mars.pose()
            engine.tick(t, pose, mars.object_centers(), engine.world_epoch)
            reached_medicine |= pose[1] > 0.9
            if reached_medicine and pose[1] < 0.7:
                exit_route.append(pose)
            assert not agent.failed_reason, agent.failed_reason
        assert engine.state == "passed", (engine.reason, mars.pose(), t)
        assert 240 < engine.elapsed_s < 270  # still at least 30 s to spare
        assert exit_route
        # The actual return route stays clear even at the final fire stage.
        mars.fire.sync(challenge, 300)
        for pose in exit_route:
            assert not challenge.fail_if.update(WorldState(300, pose, {}, elapsed=300), [])
            assert all(np.linalg.norm(np.array(pose[:2]) - source[:2]) > 0.30 for source in mars.fire.sources)
    finally:
        mars.close()
