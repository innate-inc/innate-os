"""Gallery tasks must describe props that exist, settle and can be reached."""

import math
import threading
from pathlib import Path

import numpy as np
import pytest
import trimesh
from mars_sim_driver.challenges import ChallengeEngine
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.environments import Environment
from mars_sim_driver.props import Prop, load_props
from navplan import NavMap

SIM = Path(__file__).resolve().parents[1]
PROPS = load_props([SIM / "bundles/gallery/props"])


@pytest.mark.parametrize("prop", PROPS.values(), ids=lambda p: p.name)
def test_gallery_models_match_in_both_cameras(prop):
    robot = trimesh.load(prop.mesh_path, force="mesh", process=False)
    browser = trimesh.load(prop.root.parent / "viewer" / prop.viewer["glb"].lstrip("/"), force="mesh", process=False)
    assert prop.texture_path.is_file()
    assert browser.visual.material.baseColorTexture is not None
    assert robot.visual.uv is not None
    np.testing.assert_allclose(robot.vertices * prop.mesh_scale, browser.vertices, atol=1e-7)
    np.testing.assert_array_equal(robot.faces, browser.faces)
    np.testing.assert_allclose(robot.bounds[:, 2], [-prop.size[1], prop.size[1]], atol=1e-7)


@pytest.mark.parametrize("pose", [(0, 0), (0, 0, math.nan), (math.inf, 0, 0)])
def test_invalid_exhibit_pose_is_rejected(pose):
    with pytest.raises(ValueError, match="initial_pose"):
        Prop("invalid", initial_pose=pose)


def test_gallery_is_populated_and_every_retry_restores_stable_reachable_targets(tmp_path):
    mars = VirtualMars(render_wh=(64, 48), environment=Environment.load("gallery", SIM / "assets"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=tmp_path / "progress.json"
    )

    def check_exhibits():
        mars.step(1.0)
        objects = mars.object_poses()
        assert set(objects) == set(PROPS)
        assert sum("_can_" in name for name in objects) == 8
        for name, prop in PROPS.items():
            np.testing.assert_allclose(objects[name][:2], prop.initial_pose[:2], atol=0.002)
            support = 0.5 if name == "gallery_mug_h50" else 0.0
            assert objects[name][2] - prop.rest_z == pytest.approx(support, abs=0.001)

    try:
        check_exhibits()  # Merely opening Gallery must show its objects.
        mars.props.park_all(mars.data)
        nav = NavMap.from_sim(mars)
        # Approaches stay outside the plinth and within each goal tolerance.
        for target in [(0, 2.2), (2.2, 0), (0, -2.2), (-2.2, 0), (-3, 2.85), (3, 2.75)]:
            route = nav.plan((0, 0), target)
            assert route is not None, f"unreachable Gallery approach: {target}"
            assert math.dist(route[-1], target) < 0.1
        assert len(engine.challenges) == 4
        for challenge in engine.challenges.values():
            assert all(drop.name in PROPS for drop in challenge.setup)
            for _ in range(2):
                assert engine.start(challenge.id)
                check_exhibits()
                mars.props.park_all(mars.data)
        mars.reset()
        check_exhibits()
    finally:
        mars.close()
