"""A room built from primitives must be seen by the depth camera, not only the colour one.

Its collidable geoms went in whatever group the collision hulls take, which
without visual rooms is group 0 -- the robot's own -- and update_depth()
hides group 0 so the arm does not mark itself as an obstacle at the footprint.
So every benchmark world had walls and furniture in the camera image and none
in the depth stream the costmap's voxel layer marks and clears from. The lidar
still saw them (it hits every group when there are no visual rooms), which is
why nothing crashed: the robot simply had one sensor fewer than it does in the
apartment. Found by an adversarial review; the fix puts the room in
VISUAL_GROUP, which every sensor renders.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

from pathlib import Path

import numpy as np
import pytest
from mars_sim_driver import core as _core
from mars_sim_driver import world as _world
from mars_sim_driver.environments import Environment

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "sim" / "assets"


@pytest.fixture(scope="module")
def counter():
    """The counter world through its environment pack, small render targets."""
    patch = pytest.MonkeyPatch()
    patch.delenv("VIRTUAL_MARS_ASSETS", raising=False)
    patch.setattr(_core, "ASSETS_DIR", _world.default_assets_dir())
    mars = _core.VirtualMars(
        render_wh=(64, 48), depth_render_wh=(64, 48), environment=Environment.load("counter", ASSETS)
    )
    try:
        yield mars
    finally:
        mars.close()
        patch.undo()


def test_room_geoms_are_in_the_visual_group(counter) -> None:
    model = counter.model
    groups = {
        int(model.geom_group[i])
        for i in range(model.ngeom)
        if (model.body(int(model.geom_bodyid[i])).name or "").startswith("room_")
    }
    assert groups == {_world.VISUAL_GROUP}, groups


def test_the_depth_camera_sees_the_room(counter) -> None:
    """From the spawn the camera looks across a 4.6 m room: most of the depth
    image is floor and wall within 6 m. With the room hidden it was props and
    the far plane."""
    depth = counter.render_depth("main")
    near = np.isfinite(depth) & (depth < 6.0)
    assert near.mean() > 0.5, f"only {near.mean():.0%} of the depth image is within 6 m"
