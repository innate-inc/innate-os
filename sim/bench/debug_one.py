#!/usr/bin/env python3
"""Trace one episode with the same agent main.py would build.

Usage: debug_one.py <map> <challenge_id>
"""

import os
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

MAP = sys.argv[1] if len(sys.argv) > 1 else "household"
CID = sys.argv[2] if len(sys.argv) > 2 else "household_tour"

from runner import sources  # noqa: E402

assets, ch_root = sources()[MAP]
if assets is not None:
    os.environ["VIRTUAL_MARS_ASSETS"] = str(assets)

import autoplan  # noqa: E402
from mars_sim_driver.challenges import ChallengeEngine  # noqa: E402
from mars_sim_driver.core import VirtualMars  # noqa: E402
from navplan import NavMap  # noqa: E402
from oracles import ORACLES  # noqa: E402
from planner_agent import PlannerAgent  # noqa: E402


# In main() so that importing this module does not build a world and run
# an episode. The argv/env/import order above stays at module level on
# purpose: core.py resolves ASSETS_DIR at import time, so VIRTUAL_MARS_ASSETS
# has to be set before mars_sim_driver is imported at all.
def main() -> int:
    mars = VirtualMars(render_wh=(160, 120))
    lock = threading.Lock()
    engine = ChallengeEngine(mars, lock, roots=[ch_root], progress_path=Path("/tmp/dbg_progress.json"))
    ch = engine.challenges[CID]
    print(f"{CID}: {len(ch.goals)} goals, limit {ch.time_limit_s}s, class={autoplan.classify(ch)}")
    for g in ch.goals:
        print(f"   - {g.label}: {g.predicate}")

    steps = ORACLES.get(CID) or autoplan.plan_for(CID and ch)
    print(f"\nplan ({'hand' if CID in ORACLES else 'auto'}), {len(steps)} steps:")
    for s in steps:
        print(f"   {s}")

    nav = NavMap.from_sim(mars)
    print(f"\nnav: {nav.h}x{nav.w} cells, {int(nav.blocked.sum())} blocked")
    engine.start(CID)
    agent = PlannerAgent(steps)
    agent.reset(mars, ch, nav=nav)
    agent.bind_events(engine.post_event)

    print("\n  t      pose                 step  goals")
    last = -1
    for i in range(40000):
        agent.act(mars, float(mars.data.time))
        mars.step(0.05)
        if i % 2 == 0:
            with lock:
                t, pose, centers, epoch = float(mars.data.time), mars.pose(), mars.object_centers(), engine.world_epoch
            engine.tick(t, pose, centers, epoch)
            if agent.i != last:
                last = agent.i
                s = steps[agent.i] if agent.i < len(steps) else ("done",)
                print(f"  {t:6.1f} ({pose[0]:6.2f},{pose[1]:6.2f})  {agent.i:>2} {str(s)[:44]:<46} {engine.goal_done}")
            if engine.state != "running" or agent.done or t > (ch.time_limit_s or 900):
                break

    print(
        f"\nfinal: {engine.state} goals={engine.goal_done} reason={engine.reason!r} "
        f"agent_step={agent.i}/{len(steps)} failed={agent.failed_reason!r}"
    )
    print(f"pose={mars.pose()}  sim_t={float(mars.data.time):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
