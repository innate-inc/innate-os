"""Household's two scenes must be physically sane and describe what is there.

Both faults these pin were found in sim/bench/ENVIRONMENT_AUDIT.md, and both
are invisible to the validity gate: the scripted oracle is handed its
destination coordinates, so it neither looks for the person the brief names
nor cares that the casualty is launched across the flat before the run starts.

`household_take_orders` dropped its 1.7 m body at (-3.4, -0.5) with the feet
in the bedroom and the head 1.23 m into the living room, straight through the
spine wall between them. Measured over the 1.5 s after a reset: a peak of
2.285 m and 1.732 m of travel. A drop is therefore checked for containment
against the settled body the simulator carries, and for motion immediately
after the reset -- a rest pose says nothing about how it got there.

`household_fetch_mug` said "bring it to the person in the living room" and
spawned nobody, judging delivery by a bare circle around a marker pad. The
third test here keeps the brief, the scene and the judge pointing at the same
place, so the circle cannot drift away from the person it stands for.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import math
import threading
from pathlib import Path

import pytest
from mars_sim_driver.challenges import AllOf, ChallengeEngine, Hold, InCircle, Near
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.environments import Environment

SIM = Path(__file__).resolve().parents[1]

# The living room, inside its own walls: the spine wall at y=0, the kitchen
# wall at x=0 and the perimeter at x=-4.5 and y=3.5, each 0.06 m half-thick.
LIVING_ROOM = (-4.44, 0.06, -0.06, 3.44)


@pytest.fixture(scope="module")
def household():
    mars = VirtualMars(render_wh=(64, 48), depth_render_wh=(64, 48),
                       environment=Environment.load("household", SIM / "assets"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=Path("/tmp/hh_progress.json")
    )
    try:
        yield mars, engine
    finally:
        mars.close()


def _drop(engine, challenge_id, prop):
    challenge = engine.challenges[challenge_id]
    return next(d for d in challenge.setup if d.name == prop)


def test_the_fallen_body_lies_wholly_inside_the_living_room(household):
    """Measured off the settled body, not off the mesh's nominal length.

    MuJoCo re-centres a mesh on its own frame, so deriving the footprint from
    the drop point and a quoted height gets the answer wrong in both
    directions. The geom's bounding sphere is what the simulator itself
    carries, and it is conservative: if the sphere fits in the room, the body
    does.
    """
    mars, engine = household
    assert engine.start("household_take_orders")
    mars.step(1.5)
    body = mars.model.body("human").id
    x0, y0, x1, y1 = LIVING_ROOM
    for g in range(mars.model.ngeom):
        if mars.model.geom_bodyid[g] != body:
            continue
        cx, cy = (float(v) for v in mars.data.geom_xpos[g][:2])
        r = float(mars.model.geom_rbound[g])
        assert x0 <= cx - r and cx + r <= x1, f"the body reaches x {cx - r:.2f}..{cx + r:.2f}, outside the living room"
        assert y0 <= cy - r and cy + r <= y1, f"the body reaches y {cy - r:.2f}..{cy + r:.2f}, outside the living room"


def test_the_fallen_body_stays_where_the_scene_puts_it(household):
    """The 1.5 s AFTER the reset, not the eventual rest pose.

    The old drop settled `somewhere` perfectly calmly, a room and 1.7 m away
    from the casualty the brief describes.
    """
    mars, engine = household
    assert engine.start("household_take_orders")
    body = mars.model.body("human").id
    mars.step(0.05)
    start = tuple(float(v) for v in mars.data.xpos[body][:2])
    peak_z = 0.0
    for _ in range(30):  # 1.5 s
        mars.step(0.05)
        peak_z = max(peak_z, float(mars.data.xpos[body][2]))
    end = tuple(float(v) for v in mars.data.xpos[body][:2])
    drift = math.dist(start, end)
    assert drift < 0.03, f"the body travelled {drift:.3f} m in the first 1.5 s"
    drop_z = mars.props.props["human"].drop_z
    assert peak_z <= drop_z + 0.02, f"the body was thrown to {peak_z:.3f} m, above its {drop_z} m drop"


def test_the_mug_is_delivered_to_someone_who_is_actually_there(household):
    """The brief names a person; the scene must spawn one, and the judge must
    measure against them rather than against a coordinate nobody can see."""
    mars, engine = household
    challenge = engine.challenges["household_fetch_mug"]
    assert "casey" in challenge.brief.lower(), challenge.brief

    recipient = _drop(engine, "household_fetch_mug", "resident_casey")
    assert engine.start("household_fetch_mug")
    mars.step(1.5)
    poses = mars.object_poses()
    assert "resident_casey" in poses, "the person the brief names is not in the scene"
    assert math.dist(poses["resident_casey"][:2], (recipient.x, recipient.y)) < 0.03

    delivery = challenge.goals[-1].predicate
    assert isinstance(delivery, Hold) and isinstance(delivery.inner, AllOf)
    circle = next(p for p in delivery.inner.preds if isinstance(p, InCircle))
    near = next(p for p in delivery.inner.preds if isinstance(p, Near))

    # The circle stands in for "by Casey", so it has to be somewhere Casey is.
    assert near.b == "resident_casey"
    assert math.dist((circle.x, circle.y), (recipient.x, recipient.y)) <= near.radius_m, (
        "the delivery circle has drifted away from the person it represents"
    )
    # And the mug has to be resting on the floor, not held, buried or airborne.
    rest_z = mars.props.props["household_mug_kitchen"].rest_z
    assert circle.min_z is not None and circle.max_z is not None
    assert circle.min_z <= rest_z <= circle.max_z
