"""Grocery recognition depends on the same textured shape reaching both views."""

from pathlib import Path

import mujoco
import numpy as np
import pytest
import trimesh
from mars_sim_driver.props import load_props
from mars_sim_driver.statics import load_rooms

SIM = Path(__file__).resolve().parents[1]
PROPS = load_props([SIM / "bundles/pantry/props", SIM / "bundles/counter/props"])
GROCERIES = [p for p in PROPS.values() if p.name.startswith("pantry_") or p.name == "counter_jar_jam"]


@pytest.mark.parametrize("prop", GROCERIES, ids=lambda p: p.name)
def test_robot_and_browser_have_the_same_textured_shape(prop):
    assert prop.mesh_path is not None
    assert prop.texture_path is not None
    assert prop.viewer["preNormalized"] is True
    glb = prop.root.parent / "viewer" / prop.viewer["glb"].lstrip("/")
    robot = trimesh.load(prop.mesh_path, force="mesh", process=False)
    browser = trimesh.load(glb, force="mesh", process=False)
    assert browser.visual.material.baseColorTexture is not None
    assert robot.visual.uv is not None
    np.testing.assert_allclose(robot.bounds, browser.bounds, atol=1e-7)
    # Neither format may silently rotate, re-centre, or scale a small jar.
    np.testing.assert_allclose(robot.vertices * prop.mesh_scale, browser.vertices, atol=1e-7)
    np.testing.assert_array_equal(robot.faces, browser.faces)
    half = np.array(prop.size if len(prop.size) == 3 else (prop.size[0], prop.size[0], prop.size[1]))
    np.testing.assert_allclose(robot.bounds, [-half, half], atol=1e-7)

    xml = (
        "<mujoco><asset>"
        + prop.assets_xml(1)
        + "</asset><worldbody>"
        + prop.body_xml(0, 0, 1, 2)
        + "</worldbody></mujoco>"
    )
    model = mujoco.MjModel.from_xml_string(xml)
    assert model.ntex == 1
    visual = model.geom(f"{prop.name}_visual").id
    assert model.geom_matid[visual] >= 0
    assert model.geom_contype[visual] == model.geom_conaffinity[visual] == 0
    collider = model.geom(f"{prop.name}_geom").id
    np.testing.assert_allclose(model.geom_size[collider][: len(prop.size)], prop.size)


def test_shelves_fit_the_jars_without_moving_the_support_surfaces():
    room = load_rooms([SIM / "bundles/pantry/rooms"])["pantry"]
    shelves = sorted((g for g in room.geoms if g.name == "deck" and g.pos[0] < -2), key=lambda g: g.pos[2])
    np.testing.assert_allclose([g.pos[2] + g.size[2] for g in shelves], [0.13, 0.26, 0.39])
    tallest_jar = max(2 * p.size[1] for p in GROCERIES if "_jar_" in p.name)
    for lower, upper in zip(shelves, shelves[1:], strict=False):
        clearance = upper.pos[2] - upper.size[2] - lower.pos[2] - lower.size[2]
        assert clearance >= tallest_jar + 0.005
