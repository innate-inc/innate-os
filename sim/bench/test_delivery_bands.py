"""A delivery's height band must accept the item however it comes to rest.

The bands exist to tell an item resting on the floor from one that is held,
airborne or buried. They were drawn from each item's UPRIGHT rest height
(rest_z - 4 mm to rest_z + 15 mm), so a jar, carton, medicine bottle, document
box or oil can set down on the right mat and tipped onto its side -- centre
25 mm up, or 20 mm for the can -- failed, while the brief only said "put it
down". The book and the phone also have a stable pose on their edge, above
their old bands.

So every band is checked against the item itself: settled on the band's own
mat upright, on each side and upside down, through the same object pose the
judge reads, each rest must lie inside it.
"""

import math
import threading
from collections import defaultdict
from pathlib import Path

import mujoco
import pytest
from mars_sim_driver.challenges import ChallengeEngine, InCircle, InRect
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.environments import Environment

SIM = Path(__file__).resolve().parents[1]
WORLDS = ["blaze", "counter", "household", "pantry", "rounds", "workshop"]
R = math.sqrt(0.5)
POSES = {"upright": (1, 0, 0, 0), "on side (x)": (R, R, 0, 0), "on side (y)": (R, 0, R, 0), "upside down": (0, 1, 0, 0)}


def _bands(predicate, out):
    if isinstance(predicate, (InCircle, InRect)) and predicate.min_z is not None:
        out.append(predicate)
    for child in [getattr(predicate, "inner", None), *(getattr(predicate, "preds", None) or [])]:
        if child is not None:
            _bands(child, out)


def _centre(band):
    if isinstance(band, InCircle):
        return band.x, band.y
    return (band.x0 + band.x1) / 2, (band.y0 + band.y1) / 2


@pytest.mark.parametrize("world", WORLDS)
def test_every_stable_rest_is_a_delivery(world, tmp_path):
    mars = VirtualMars(render_wh=(32, 24), environment=Environment.load(world, SIM / "assets"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=tmp_path / "p.json"
    )
    try:
        bands = defaultdict(list)
        for challenge in engine.challenges.values():
            for goal in challenge.goals:
                found = []
                _bands(goal.predicate, found)
                for band in found:
                    bands[band.target].append((challenge.id, band))
        assert bands, f"{world} has no delivery bands"
        m, d = mars.model, mars.data
        failures = []
        for target, uses in bands.items():
            prop = mars.props.props[target]
            for challenge_id, band in uses:
                x, y = _centre(band)
                for label, quat in POSES.items():
                    mars.reset()
                    mars.props.park_all(d)
                    # Some deliveries go to the spawn pad; settle on the floor, not the robot.
                    d.qpos[mars._base["x"][0]] += 50.0
                    assert mars.drop_prop_at(target, x, y, z=prop.rest_z + 0.15)
                    joint = m.body(target).jntadr[0]
                    adr, dof = m.jnt_qposadr[joint], m.jnt_dofadr[joint]
                    d.qpos[adr + 3 : adr + 7] = quat
                    d.qvel[dof : dof + 6] = 0
                    mujoco.mj_forward(m, d)
                    mars.step(2.5)
                    z = mars.object_poses()[target][2]
                    if not band.min_z <= z <= band.max_z:
                        failures.append(
                            f"{challenge_id}: {target} {label} rests at {z:.4f}, band {band.min_z}..{band.max_z}"
                        )
        assert not failures, "\n".join(failures)
    finally:
        mars.close()
