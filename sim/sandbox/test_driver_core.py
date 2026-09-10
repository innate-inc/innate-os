"""Headless check of mars_sim_driver.core (no ROS needed): settle, render
both cameras, drive via cmd_vel, verify motion and the stale-command
watchdog, then judge a challenge against the same world. Saves the rendered
frames to sim/assets/virtual_mars_test/ for eyeballing.

Usage: uv run sandbox/test_driver_core.py
"""

import json
import math
import threading
import time

import _driver_pkg  # noqa: F401
from mars_sim_driver import world
from mars_sim_driver.challenges import AllOf, Challenge, ChallengeEngine, Drop, Goal, Hold, Near, SkillDone, WorldState
from mars_sim_driver.core import VirtualMars, joint2_min_target

OUT_DIR = world.default_assets_dir() / "virtual_mars_test"


def main() -> None:
    sim = VirtualMars()
    sim.step(1.0)  # settle from spawn drop

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for cam in ("main", "wrist"):
        jpeg = sim.render_jpeg(cam)
        assert jpeg[:2] == b"\xff\xd8", f"{cam}: not a JPEG"
        (OUT_DIR / f"{cam}.jpg").write_bytes(jpeg)
        print(f"{cam}: {len(jpeg)} byte JPEG -> {OUT_DIR / f'{cam}.jpg'}")

    x0, y0, yaw0 = sim.pose()
    for _ in range(20):  # re-send like a teleop publisher so the watchdog stays fed
        sim.set_cmd_vel(0.3, 0.0)
        sim.step(0.1)
    x1, y1, _ = sim.pose()
    moved = math.hypot(x1 - x0, y1 - y0)
    assert moved > 0.3, f"drove only {moved:.2f}m"
    print(f"cmd_vel drive: moved {moved:.2f}m")

    sim.step(1.0)  # no new commands -> watchdog stops the base
    x2, y2, _ = sim.pose()
    sim.step(0.5)
    x3, y3, _ = sim.pose()
    drift = math.hypot(x3 - x2, y3 - y2)
    assert drift < 0.05, f"base still moving {drift:.2f}m after cmd_vel went stale"
    print(f"watchdog: base stopped (drift {drift * 100:.1f}cm)")

    assert abs(sim.head_pitch_deg()) < 5.0
    print(f"head pitch: {sim.head_pitch_deg():.1f} deg")

    # Body guard: while joint1's target crosses the front arc, joint2 may not
    # stay folded past -0.5 (arm_control.cpp's floor) -- the arm ducks under
    # the head. Ramp math first, then the symptom that matters: a teleop-like
    # streamed joint1 sweep from home across the front must not jam.
    assert joint2_min_target(-1.4, -1.57) == -1.57  # outside the arc: full range
    assert joint2_min_target(0.0, -1.57) == -0.25  # front arc: duck
    assert abs(joint2_min_target(1.125, -1.57) - (-0.91)) < 1e-9  # mid-ramp
    assert joint2_min_target(1.25, -1.57) == -1.57
    home_j1 = world.ARM_HOME["joint1"]
    for leg_target in (-1.4, home_j1):  # across the front and back
        start = sim.joint_positions()["joint1"]
        for i in range(1, 41):  # ~0.07 rad per 100ms, teleop cadence
            sim.set_joint_target("joint1", start + (leg_target - start) * i / 40)
            sim.step(0.1)
        sim.step(3.0)
        p = sim.joint_positions()
        assert abs(p["joint1"] - leg_target) < 0.15, f"joint1 stalled at {p['joint1']:.2f} sweeping to {leg_target:.2f}"
    print(f"joint2 body guard: front-arc sweep ok both ways (j2 back at {p['joint2']:+.2f})")
    sim.reset()

    depth = sim.render_depth("main")
    center = float(depth[depth.shape[0] // 2, depth.shape[1] // 2])
    assert 0.1 < center < 15.0, f"implausible center depth {center}"
    print(f"depth: center pixel {center:.2f}m, range {depth.min():.2f}-{depth.max():.2f}m")

    scan = sim.lidar_scan(n_rays=360, max_range=12.0)
    hits = (scan < 12.0).sum()
    assert hits > 180, f"only {hits}/360 lidar rays hit indoors"
    assert scan.min() > 0.05, f"lidar sees something at {scan.min():.3f}m (inside the robot?)"
    print(f"lidar: {hits}/360 rays hit, nearest {scan.min():.2f}m, farthest hit {scan[scan < 12].max():.2f}m")

    check_challenges(sim)  # last: it moves the sim clock and leaves a prop out
    check_concurrent_start(sim)
    check_stale_tick(sim)
    check_poisoned_progress(sim)
    check_external_reset(sim)
    check_event_ordering(sim)
    print("PASS")


def check_challenges(sim: VirtualMars) -> None:
    """The two ways the challenge judge (challenges.py) has been wrong.

    No GL and no rosbridge here -- predicates are pure functions of the world
    state, and the engine is fed by hand rather than by the physics thread."""
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=OUT_DIR / "challenges.json")
    engine.challenges["probe"] = Challenge(
        id="probe",
        title="Probe",
        brief="",
        # On the spawn point, where start()'s world reset puts the robot back:
        # goal 0 is true from the first tick that is allowed to judge. Goal 1
        # never is, so the run stays open for the assertions below.
        setup=[Drop("human", world.SPAWN_X, world.SPAWN_Y)],
        goals=[Goal("reach", Near("robot", "human", 1.5)), Goal("never", Near("robot", "nothing_here", 1.0))],
        time_limit_s=60.0,
    )

    # A tick landing between the world reset and the run being published must
    # not judge anything: it would measure the PREVIOUS run's world, and time
    # the new run from a started_t that is still whatever it was. On a server
    # up longer than the limit, that failed a run at birth. The physics thread
    # produces this by taking the sim lock between slices; a tick from inside
    # the drop is the same interleaving, deterministically.
    sim.data.time = 900.0
    mid_ticks = []
    real_reset, real_drop = sim.reset, sim.drop_prop_at

    def tick_now():
        mid_ticks.append(engine.tick(float(sim.data.time), sim.pose(), sim.object_centers(), engine.world_epoch))

    def reset_and_tick():
        tick_now()  # before the reset: the OLD world, on the OLD clock (900s)
        real_reset()

    def drop_and_tick(*args, **kwargs):
        ok = real_drop(*args, **kwargs)
        tick_now()  # after the reset: a scene that is still being built
        return ok

    sim.reset, sim.drop_prop_at = reset_and_tick, drop_and_tick
    engine.start("probe")
    sim.reset, sim.drop_prop_at = real_reset, real_drop
    assert len(mid_ticks) >= 2, "the setup window was never ticked on both sides of the reset"
    assert all(tick["active"] is None for tick in mid_ticks), "judged a run that had not started"

    block = engine.tick(float(sim.data.time), sim.pose(), sim.object_centers(), engine.world_epoch)["active"]
    assert block["state"] == "running", f"fresh run born {block['state']} ({block['reason']})"
    assert block["elapsed_s"] == 0.0, f"fresh run started at {block['elapsed_s']}s"
    assert block["goals"][0]["done"], "judging never resumed after the setup window"
    print(f"challenge start: clean through {len(mid_ticks)} ticks in the setup window (900s uptime, 60s limit)")

    # AllOf must update every child, not stop at the first False: a Hold only
    # advances when it is asked, so short-circuiting restarts its dwell.
    dwell = Hold(Near("robot", "human", 1.5), seconds=5.0)
    both = AllOf([Near("robot", "nothing_here", 1.0), dwell])  # first child never true
    here = {"human": (0.0, 0.0)}
    for t in (0.0, 2.0, 6.0):
        both.update(WorldState(t=t, robot=(0.0, 0.0, 0.0), centers=here), [])
    assert dwell.update(WorldState(t=6.0, robot=(0.0, 0.0, 0.0), centers=here), []), "Hold behind AllOf lost its dwell"
    print("challenge predicates: Hold keeps its dwell behind a decided sibling")

    engine.abort()
    sim.remove_all_props()
    sim.reset()


def check_concurrent_start(sim: VirtualMars) -> None:
    """Two observer connections starting a challenge at the same time.

    start() is three critical sections (deactivate, build the scene, publish)
    and each observer connection commands on its own thread, so without one
    lock across all three the second starter tears the first down mid-build
    and the published run is judged against the other's world."""
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=OUT_DIR / "challenges_concurrent.json")
    for cid, prop in (("a", "cube"), ("b", "can")):
        engine.challenges[cid] = Challenge(
            id=cid,
            title=cid,
            brief="",
            setup=[Drop(prop, world.SPAWN_X, world.SPAWN_Y)],
            goals=[Goal("never", Near("robot", "nothing_here", 1.0))],  # never passes: the run stays open
        )

    # Engine teardowns, counted: the tell is whether the second start reaches
    # _deactivate() while the first is still inside its scene build.
    deactivations = []
    real_deactivate = engine._deactivate

    def counting_deactivate():
        deactivations.append(1)
        real_deactivate()

    engine._deactivate = counting_deactivate
    b_running, b_done = threading.Event(), threading.Event()

    def start_b():
        b_running.set()
        engine.start("b")
        b_done.set()

    real_drop = sim.drop_prop_at
    raced = []

    def drop_and_race(*args, **kwargs):
        ok = real_drop(*args, **kwargs)
        if not raced:  # once, from inside challenge a's scene build
            raced.append(1)
            threading.Thread(target=start_b, daemon=True).start()
            b_running.wait(5.0)  # b's thread is alive and about to call start()
            time.sleep(0.3)  # ample for an unserialized start to get in
            assert len(deactivations) == 1, "a second start tore down the run being built"
            assert not b_done.is_set(), "a second start published while the first was mid-build"
        return ok

    sim.drop_prop_at = drop_and_race
    engine.start("a")
    sim.drop_prop_at = real_drop

    assert b_done.wait(5.0), "the queued start never ran"
    assert engine.active is not None and engine.active.id == "b", "the later start did not win"
    out = set(sim.props.out)
    assert out == {"can"}, f"world does not match the active run (expected just b's can, got {sorted(out)})"
    print("challenge start: concurrent starts serialize, and the world matches the run that won")

    engine.abort()
    sim.remove_all_props()
    sim.reset()


def check_stale_tick(sim: VirtualMars) -> None:
    """A snapshot gathered before start() must not be judged after it.

    publish_state() gathers (t, pose, centers) under the sim lock and ticks
    once it has let go, so a start() completing in that gap resets the world
    and the clock under a snapshot already in flight: t=900 against a
    started_t of ~0 is an instant "time limit" on a 60s challenge, and goal 0
    would be judged against the previous run's props. The gap is between two
    calls this test makes itself, so making them in that order -- snapshot,
    start, stale tick -- reproduces it without threads."""
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=OUT_DIR / "challenges_stale.json")
    engine.challenges["probe"] = Challenge(
        id="probe",
        title="Probe",
        brief="",
        setup=[Drop("human", world.SPAWN_X, world.SPAWN_Y)],
        goals=[Goal("reach", Near("robot", "human", 1.5))],
        time_limit_s=60.0,
    )

    sim.data.time = 900.0  # server up 15 minutes, against a 60s limit
    stale = (float(sim.data.time), sim.pose(), sim.object_centers(), engine.world_epoch)
    engine.start("probe")  # lands in publish_state's snapshot -> tick gap
    engine.post_event({"status": "completed", "skill_id": "x"})  # belongs to THIS run

    block = engine.tick(*stale)["active"]
    assert block is not None and block["state"] == "running", f"stale snapshot killed the fresh run: {block}"
    assert block["elapsed_s"] == 0.0, f"stale snapshot aged the fresh run to {block['elapsed_s']}s"
    assert engine._events, "the skipped tick swallowed this run's events"

    live = engine.tick(float(sim.data.time), sim.pose(), sim.object_centers(), engine.world_epoch)["active"]
    assert live["state"] == "passed", f"judging never resumed after the stale tick ({live['state']}: {live['reason']})"
    print("challenge start: a snapshot from before the run is rendered, never judged")

    engine.abort()
    sim.remove_all_props()
    sim.reset()


def check_poisoned_progress(sim: VirtualMars) -> None:
    """A broken challenge layer must not take the physics thread with it.

    tick() runs on the physics thread and physics_loop has no guard, so
    anything that raises in there -- here a hand-edited challenges.json whose
    entry is not a dict, but equally a predicate bug -- would stop the world
    for every viewer rather than degrade one challenge."""
    from mars_sim_driver.world_server import WorldServer

    path = OUT_DIR / "challenges_poisoned.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "challenges": {"probe": 5}}))  # entry is not a dict

    server = WorldServer(sim)
    server.challenges = ChallengeEngine(sim, server.lock, roots=[], progress_path=path)
    server.challenges.challenges["probe"] = Challenge(id="probe", title="Probe", brief="", setup=[], goals=[])

    # The engine really does blow up on it: _block() reads entry.get("passed")
    # on every broadcast, active challenge or not.
    try:
        server.challenges.tick(0.0, sim.pose(), {}, server.challenges.world_epoch)
        raised = False
    except Exception:  # noqa: BLE001 -- the point of the test
        raised = True
    assert raised, "expected a non-dict progress entry to raise inside the engine"

    # ...and publish_state has to contain it, leaving the world stepping.
    server.publish_state()
    payload = json.loads(server.state_payload)
    assert payload["challenge"] is None, f"expected the block to go missing, got {payload['challenge']}"
    assert payload["pose"], "the state broadcast lost its world"
    print("challenge layer: a poisoned progress file drops the block, not the physics thread")


def check_external_reset(sim: VirtualMars) -> None:
    """A reset that bypasses the engine must end the run, not strand it.

    /virtual_mars/reset, the observer reset op and core.step()'s NaN recovery
    all call sim.reset() directly: props re-park and data.time zeroes without
    the engine hearing about it. started_t is then ahead of the clock, so
    elapsed_s clamps to 0.0 and the time limit can never fire, while the
    parked props mean no goal can pass -- a 0:00 run only a manual abort ends.
    A backwards sim clock is the one signal that catches all three paths."""
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=OUT_DIR / "challenges_reset.json")
    engine.challenges["probe"] = Challenge(
        id="probe",
        title="Probe",
        brief="",
        setup=[Drop("human", world.SPAWN_X, world.SPAWN_Y)],
        goals=[Goal("never", Near("robot", "nothing_here", 1.0))],
        time_limit_s=600.0,
    )
    engine.start("probe")

    block = engine.tick(50.0, sim.pose(), sim.object_centers(), engine.world_epoch)["active"]
    assert block["state"] == "running", f"the run should still be going at 50s: {block}"

    sim.reset()  # what /virtual_mars/reset does, behind the engine's back
    block = engine.tick(float(sim.data.time), sim.pose(), sim.object_centers(), engine.world_epoch)["active"]
    assert block["state"] == "failed", f"a reset left the run {block['state']} at {block['elapsed_s']}s"
    assert block["reason"] == "the sim was reset", block["reason"]
    print("challenge reset: a world rebuilt behind the engine fails the run instead of stranding it")

    engine.abort()
    sim.remove_all_props()
    sim.reset()


def check_event_ordering(sim: VirtualMars) -> None:
    """Two skill completions in one batch must advance both goals.

    Ticks only happen when the physics thread stepped, so a stall or a
    rosbridge reconnect hands one tick several completions. Judging only the
    first unmet goal per tick would drop the second even though the robot did
    both things in the right order -- which is not what "strictly ordered"
    means. An event that arrives while an EARLIER goal is still open is a
    different case and is still discarded, by design."""
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=OUT_DIR / "challenges_events.json")
    engine.challenges["probe"] = Challenge(
        id="probe",
        title="Probe",
        brief="",
        setup=[],
        goals=[Goal("wave", SkillDone("wave")), Goal("emote", SkillDone("head_emotion"))],
    )

    engine.start("probe")
    engine.post_event({"status": "completed", "skill_id": "wave"})
    engine.post_event({"status": "completed", "skill_id": "head_emotion"})  # same batch
    block = engine.tick(float(sim.data.time), sim.pose(), sim.object_centers(), engine.world_epoch)["active"]
    assert block["state"] == "passed", f"one batch did not advance both ordered goals: {block['goals']}"

    # The out-of-order case stays discarded: emote first, while "wave" is open.
    engine.start("probe")
    engine.post_event({"status": "completed", "skill_id": "head_emotion"})
    engine.tick(float(sim.data.time), sim.pose(), sim.object_centers(), engine.world_epoch)
    engine.post_event({"status": "completed", "skill_id": "wave"})
    block = engine.tick(float(sim.data.time), sim.pose(), sim.object_centers(), engine.world_epoch)["active"]
    assert block["goals"][0]["done"] and not block["goals"][1]["done"], block["goals"]
    print("challenge events: one batch advances goals in order; an early completion is dropped and logged")

    engine.abort()
    sim.reset()


if __name__ == "__main__":
    main()
