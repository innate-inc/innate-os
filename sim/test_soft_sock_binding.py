"""Real MuJoCo checks for stress-free rest geometry versus placement shape."""

import mujoco
import numpy as np
import pytest
import sandbox._driver_pkg  # noqa: F401
from mars_sim_driver.props import PropRegistry
from mars_sim_driver.softbody import SoftProp


def test_bending_force_is_negative_energy_gradient(tmp_path):
    from asset_tools.build_soft_sock_asset import _build_hinges

    rest = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.1, 0.1, 0.0], [0.0, 0.1, 0.0]])
    faces = np.array([[0, 1, 2], [0, 2, 3]])
    hinges, angles, _ = _build_hinges(rest, faces)
    np.savez(tmp_path / "hinge.npz", vertices=rest, faces=faces, hinges=hinges, rest_angles=angles)
    prop = SoftProp(
        name="hinge", data="hinge.npz", root=tmp_path, deformable_id=1, bend_stiffness=1e-5, bend_damping_ratio=0
    )
    model = mujoco.MjModel.from_xml_string(f"<mujoco><worldbody>{prop.body_xml(0, 0, 0, 1)}</worldbody></mujoco>")
    binding = prop.bind(model, (0, 0))
    data = mujoco.MjData(model)
    displacement = np.array([[0.003, -0.002, 0.005], [0.0, 0.002, -0.013], [0.002, 0.0, 0.004], [0.0, 0.003, 0.02]])
    data.qpos[binding._qpos_indices] = displacement
    mujoco.mj_forward(model, data)
    binding.bending.apply(data)
    force = data.qfrc_applied[binding._dof_indices].copy()

    def energy(vertices):
        _, current, _ = _build_hinges(vertices, faces)
        delta = np.arctan2(np.sin(current - angles), np.cos(current - angles))
        return 0.5 * np.sum(binding.bending._stiffness * delta**2)

    points = binding.vertices(data).copy()
    gradient = np.zeros_like(points)
    eps = 1e-7
    for i in range(4):
        for j in range(3):
            plus, minus = points.copy(), points.copy()
            plus[i, j] += eps
            minus[i, j] -= eps
            gradient[i, j] = (energy(plus) - energy(minus)) / (2 * eps)
    np.testing.assert_allclose(force, -gradient, rtol=1e-5, atol=1e-10)
    np.testing.assert_allclose(force.sum(axis=0), 0, atol=1e-12)
    np.testing.assert_allclose(np.cross(points, force).sum(axis=0), 0, atol=1e-12)


def cloth(tmp_path, initial):
    rest = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.1, 0.1, 0.0], [0.0, 0.1, 0.0]])
    np.savez(
        tmp_path / "cloth.npz",
        vertices=rest,
        initial_vertices=initial,
        faces=np.array([[0, 1, 2], [0, 2, 3]]),
        hinges=np.empty((0, 4), dtype=int),
        rest_angles=np.empty(0),
    )
    prop = SoftProp(name="test_cloth", data="cloth.npz", deformable_id=1, root=tmp_path, rest_z=0.003)
    model = mujoco.MjModel.from_xml_string(f"<mujoco><worldbody>{prop.body_xml(1, 2, 0, 1)}</worldbody></mujoco>")
    return prop, model, rest


def test_placement_uses_initial_shape_without_changing_edge_rest_lengths(tmp_path):
    initial = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.1, 0.1, 0.01], [0.0, 0.1, 0.01]])
    prop, model, rest = cloth(tmp_path, initial)
    binding = prop.bind(model, (1, 2))
    data = mujoco.MjData(model)
    rest_lengths = model.flexedge_length0.copy()
    binding.set_pose(data, 0.2, 0.3, 0.4, np.pi / 2)
    expected = initial[:, [1, 0, 2]].copy()
    expected[:, 0] *= -1
    np.testing.assert_allclose(binding.vertices(data), expected + [0.2, 0.3, 0.4], atol=1e-12)
    np.testing.assert_array_equal(model.flexedge_length0, rest_lengths)
    binding.prepare_step(data)
    np.testing.assert_allclose(binding.vertices(data), rest + [1, 2, 0.003])
    assert not data.eq_active.any()
    assert not model.flex_contype.any()


def test_invalid_initial_shape_fails_before_simulation(tmp_path):
    for initial in (np.zeros((3, 3)), np.full((4, 3), np.nan)):
        prop, model, _ = cloth(tmp_path, initial)
        with pytest.raises(ValueError):
            prop.bind(model, (1, 2))


def test_only_active_cloth_limits_physics_timestep():
    a = SoftProp(name="a", data="unused", deformable_id=1, max_timestep=0.001)
    b = SoftProp(name="b", data="unused", deformable_id=2, max_timestep=0.0005)
    registry = PropRegistry({"a": a, "b": b})
    registry._soft = {"a": object(), "b": object()}
    assert registry.physics_timestep(0.002) == 0.002
    registry.out.add("a")
    assert registry.physics_timestep(0.002) == 0.001
    registry.out.add("b")
    assert registry.physics_timestep(0.002) == 0.0005
    assert registry.physics_timestep(0.0001) == 0.0001
    registry.out.clear()
    assert registry.physics_timestep(0.002) == 0.002
    for invalid in (0, -1, np.inf, np.nan):
        with pytest.raises(ValueError):
            SoftProp(name="bad", data="unused", deformable_id=3, max_timestep=invalid)


def test_step_restores_default_timestep_even_on_failure():
    from types import SimpleNamespace

    from mars_sim_driver.core import VirtualMars

    sim = VirtualMars.__new__(VirtualMars)
    sim._cloth = None
    sim.model = SimpleNamespace(opt=SimpleNamespace(timestep=0.002))
    sim.data = SimpleNamespace(time=1.0)
    sim.props = SimpleNamespace(physics_timestep=lambda default: 0.0005)

    def advance(end, dt):
        assert end == 1.1 and dt == 0.0005
        assert sim.model.opt.timestep == 0.0005
        raise RuntimeError("test error")

    sim._step_until = advance
    with pytest.raises(RuntimeError, match="test error"):
        sim.step(0.1)
    assert sim.model.opt.timestep == 0.002
