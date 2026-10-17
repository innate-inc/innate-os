"""A challenge's scene has landed before its clock starts.

Setup drops are released above their rest height, and the run used to start in
the same instant: the robot's first frame of household_take_orders showed the
fallen person hanging horizontal at sofa-back height. (The person is placed now,
not dropped; the dog, the cups and the rescue items still fall.)
"""

import threading
from pathlib import Path

import pytest
from mars_sim_driver.challenges import ChallengeEngine
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.environments import Environment

SIM = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "world, challenge_id",
    [("household", "household_take_orders"), ("counter", "counter_cafe_shift"), ("blaze", "blaze_l4")],
)
def test_every_setup_prop_has_landed_when_the_run_starts(world, challenge_id, tmp_path):
    mars = VirtualMars(render_wh=(32, 24), environment=Environment.load(world, SIM / "assets"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=tmp_path / "p.json"
    )
    try:
        assert engine.start(challenge_id)
        assert engine.elapsed_s == 0.0  # the settle is not charged to the robot
        names = [d.name for d in engine.active.setup if not mars.props.props[d.name].kinematic]
        assert names
        at_start = {n: mars.object_poses()[n][2] for n in names}
        mars.step(1.0)
        later = mars.object_poses()
        fell = {n: round(at_start[n] - later[n][2], 3) for n in names if abs(at_start[n] - later[n][2]) > 0.005}
        assert not fell, f"still falling when the clock started: {fell}"
    finally:
        mars.close()
