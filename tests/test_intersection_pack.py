"""One clean build through the shipped asset formats and the real physics world."""

import json
import sys
from pathlib import Path

import mujoco
import numpy as np
import pytest
import trimesh
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sim/tools"))
sys.path.insert(0, str(ROOT / "ros2_ws/src/mars_bot/mars_sim_driver"))

from build_intersection import build  # noqa: E402
from mars_sim_driver.core import VirtualMars  # noqa: E402
from mars_sim_driver.environments import Environment  # noqa: E402
from mars_sim_driver.traffic import LANES, SIGNAL_MATERIALS  # noqa: E402


def test_clean_crossroads_build_loads_visuals_physics_and_static_map(tmp_path):
    assets, viewer = tmp_path / "assets", tmp_path / "viewer"
    stale = assets / "intersection_visual/retired-part/retired-part.obj"
    stale.parent.mkdir(parents=True)
    stale.write_text("obsolete mesh from an earlier build")
    sibling = assets / "other_visual/keep.obj"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("another environment's mesh")
    build(viewer, assets)
    assert not stale.parent.exists()
    assert sibling.read_text() == "another environment's mesh"
    manifest = json.loads((ROOT / "sim/environments/intersection/manifest.json").read_text())
    scene = trimesh.load(viewer / manifest["viewer"]["model"], force="scene")
    signals = {name for aspects in SIGNAL_MATERIALS.values() for name in aspects.values()}
    assert signals <= {mesh.visual.material.name for mesh in scene.geometry.values()}
    expected_parts = {mesh.visual.material.name.lower().replace("_", "-") for mesh in scene.geometry.values()}
    assert {path.name for path in (assets / "intersection_visual").iterdir()} == expected_parts
    assert all(np.isfinite(mesh.vertices).all() for mesh in scene.geometry.values())
    for name in ("asphalt", "paving", "terracotta", "blue", "sage", "cream", "wood"):
        mesh = scene.geometry[name]
        texture = np.asarray(mesh.visual.material.baseColorTexture)
        assert texture.shape == (256, 256, 3) and texture[:, :, 0].std() > 2
        assert np.isfinite(mesh.visual.uv).all() and np.ptp(mesh.visual.uv) > 1
        directory = assets / f"intersection_visual/{name}"
        np.testing.assert_array_equal(texture, np.asarray(Image.open(directory / f"{name}.png")))
        uv = [
            line.split()[1:] for line in (directory / f"{name}.obj").read_text().splitlines() if line.startswith("vt ")
        ]
        np.testing.assert_allclose(np.asarray(uv, dtype=float), mesh.visual.uv, atol=1e-6)
    # Asphalt must not extend under the paving: millimetre-separated layers
    # turned into whole-map stripes when the overview exhausted depth precision.
    road = scene.geometry["asphalt"]
    top = road.triangles_center[road.face_normals[:, 1] > 0.9]
    assert np.all(np.min(np.abs(top[:, (0, 2)]), axis=1) <= 3.5)
    hulls = sorted((assets / manifest["physics"]["collision_dir"]).glob("*/*_collision_*.obj"))
    meshes = [trimesh.load(path, force="mesh") for path in hulls]
    assert meshes and all(mesh.is_watertight and mesh.volume > 0 for mesh in meshes)
    for mesh in meshes:
        # Convexity directly from supporting planes; no optional scipy extra.
        for normal, point in zip(mesh.face_normals, mesh.triangles[:, 0], strict=True):
            assert np.max((mesh.vertices - point) @ normal) < 1e-6
    soup = np.fromfile(viewer / manifest["viewer"]["collision_dir"] / "hulls.f32", dtype="<f4")
    np.testing.assert_allclose(soup, np.concatenate([mesh.triangles.ravel() for mesh in meshes]), atol=1e-6)

    sim = VirtualMars(environment=Environment.load("intersection", assets), render_wh=(160, 120))
    assert sim.traffic.enabled and len(sim.traffic.cars) == 4
    sim.step(0.5)
    x, y, _ = sim.pose()
    assert (x, y) == pytest.approx((3, -9.5), abs=0.05)
    assert np.isfinite(sim.data.qpos).all()
    # Drive the real planar chassis from the sidewalk through a curb cut.
    # A raised slab/ramp can look correct and map as free, yet trap this base.
    sim.data.qpos[sim.model.joint("robot_base_x").qposadr[0]] = 4.5
    sim.data.qpos[sim.model.joint("robot_base_y").qposadr[0]] = -5.3
    sim.data.qpos[sim.model.joint("robot_base_yaw").qposadr[0]] = np.pi
    mujoco.mj_forward(sim.model, sim.data)
    for _ in range(80):
        sim.set_cmd_vel(0.25, 0)
        sim.step(0.1)
    assert sim.pose()[0] < 3.2
    sim.reset()
    # Real material bindings, not just the controller's intended state.
    sim.traffic.advance(0.01, 5.0)
    assert sim.model.mat("mat_signal-ns-green").rgba[0] == 1
    assert sim.model.mat("mat_signal-ew-green").rgba[0] == pytest.approx(0.1)
    mujoco.mj_forward(sim.model, sim.data)

    # Every road mouth is open to camera/lidar, but its original hull still
    # blocks the real chassis. Perimeter fencing beside the roads stays visible.
    visual = np.zeros(6, dtype=np.uint8)
    visual[1] = 1
    hit = np.zeros(1, dtype=np.int32)
    for axis, sign in ((0, 1), (0, -1), (1, 1), (1, -1)):
        sim.reset()
        origin, direction = np.array([0.0, 0.0, 0.35]), np.zeros(3)
        origin[axis], direction[axis] = sign * 19, sign
        assert mujoco.mj_ray(sim.model, sim.data, origin, direction, visual, 1, -1, hit) < 0
        origin[1 - axis] = 4
        assert mujoco.mj_ray(sim.model, sim.data, origin, direction, visual, 1, -1, hit) == pytest.approx(1)
        position = np.zeros(2)
        position[axis] = sign * 19.6
        for joint, value in zip(("x", "y", "yaw"), (*position, np.arctan2(direction[1], direction[0])), strict=True):
            sim.data.qpos[sim.model.joint(f"robot_base_{joint}").qposadr[0]] = value
        mujoco.mj_forward(sim.model, sim.data)
        for _ in range(30):
            sim.set_cmd_vel(0.25, 0)
            sim.step(0.1)
        assert 19.7 < sign * sim.pose()[axis] < 20
        assert any(
            sim.model.geom_bodyid[contact.geom1] == sim.model.body("apartment").id
            or sim.model.geom_bodyid[contact.geom2] == sim.model.body("apartment").id
            for contact in sim.data.contact
        )

    metadata = (assets / "map/intersection.yaml").read_text()
    origin = json.loads(next(line.split(": ", 1)[1] for line in metadata.splitlines() if line.startswith("origin:")))
    grid = np.asarray(Image.open(assets / "map/intersection.pgm"))[::-1]

    def cell(x, y):
        return grid[int((y - origin[1]) / 0.05), int((x - origin[0]) / 0.05)]

    assert cell(3, -9.5) == 254  # spawn
    for lane in LANES:
        xy = (lane.initial_position, lane.fixed) if lane.axis == "x" else (lane.fixed, lane.initial_position)
        assert cell(*xy) == 254  # no baked-in car-shaped obstacles
    for sign in (-1, 1):
        for along in np.arange(-4.4, 4.5, 0.2):
            assert cell(along, sign * 5.3) == 254  # crossing + both curb cuts
            assert cell(sign * 5.3, along) == 254
    assert cell(-8.5, -13) != 254  # solid building face, not navigable road
    # Invisible walls must be occupied, not merely unknown to the planner.
    for axis in (0, 1):
        for sign in (-1, 1):
            assert cell(*((sign * 20, 0) if axis == 0 else (0, sign * 20))) == 0
    assert not list(tmp_path.rglob("*low-poly-town*"))
