"""Optional native backend lifecycle and real-finger regression tests.

Run with --extra xpbd and the patched native module built (see integration doc).
"""

from types import SimpleNamespace

import mujoco
import numpy as np
import pytest
import sandbox._driver_pkg  # noqa: F401

pytest.importorskip("numba")
from mars_sim_driver.cloth_xpbd import XPBDCloth, load_native


@pytest.mark.parametrize("selection", [None, "  ", "mujoco"])
def test_default_apartment_cloth_and_legacy_override(monkeypatch, selection):
    from mars_sim_driver.core import VirtualMars

    load_native()
    if selection is None:
        monkeypatch.delenv("INNATE_SIM_CLOTH_BACKEND", raising=False)
    else:
        monkeypatch.setenv("INNATE_SIM_CLOTH_BACKEND", selection)
    sim = VirtualMars()
    expected = "mujoco" if selection == "mujoco" else "xpbd"
    assert sim.cloth_backend == expected
    assert (sim._cloth is not None) == (expected == "xpbd")
    sim.step(0.02)
    assert sim.place_prop_at_robot("soft_sock")
    before = sim.props._soft["soft_sock"].vertices(sim.data).copy()
    sim.step(0.05)
    after = sim.props._soft["soft_sock"].vertices(sim.data)
    assert np.isfinite(after).all() and not np.array_equal(before, after)
    assert sim.model.opt.timestep == pytest.approx(0.002)
    sim.remove_prop("soft_sock")
    sim.step(0.02)
    assert not sim.deformable_frames()


@pytest.fixture
def fixture():
    from sandbox.cloth_fixture import make_fixture

    try:
        load_native()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    model, data, binding = make_fixture(held=True)
    model.opt.timestep = 0.002
    registry = SimpleNamespace(_soft={binding.prop.name: binding}, out={binding.prop.name})
    backend = XPBDCloth(model, data, registry)
    model.opt.timestep = backend.physics_timestep(0.002)
    return model, data, binding, registry, backend


def advance(fixture, seconds):
    model, data, _, _, backend = fixture
    end = data.time + seconds
    while data.time < end - 1e-10:
        mujoco.mj_step1(model, data)
        backend.before_step(model.opt.timestep)
        mujoco.mj_step2(model, data)
        backend.after_step()


def test_external_backend_disables_legacy_physics_and_handles_lifecycle(fixture):
    model, data, binding, registry, backend = fixture
    binding.set_active(data, True)
    assert model.flex_contype[binding.flex_id] == 0
    assert not data.eq_active[binding._equality_ids].any()
    assert binding.apply_forces(data) == (0.0, 0.0)
    advance(fixture, 0.1)
    assert np.isfinite(binding.vertices(data)).all()
    binding.set_pose(data, 0.3, 0, 0.5, 0.8)
    before = binding.vertices(data).copy()
    advance(fixture, 0.004)
    assert np.max(np.linalg.norm(binding.vertices(data) - before, axis=1)) < 0.01
    registry.out.clear()
    binding.park(data)
    advance(fixture, 0.004)
    assert not backend._was_active and backend._pending is None
    registry.out.add(binding.prop.name)
    binding.set_pose(data, -0.3, 0, 0.5, -0.8)
    before = binding.vertices(data).copy()
    advance(fixture, 0.004)
    assert np.max(np.linalg.norm(binding.vertices(data) - before, axis=1)) < 0.01
    backend.reset()
    assert not backend._was_active


def test_grasp_carry_and_release_using_integrated_adapter(fixture):
    from mars_sim_driver import world
    from mars_sim_driver.core import GRIPPER_EFFORT_LIMIT, KP_JOINT
    from sandbox.cloth_fixture import smooth

    model, data, binding, _, _ = fixture
    samples = []
    while data.time < 6 - 1e-10:
        t = data.time
        data.mocap_pos[0] = [0, 0, 0.35 + 0.08 * smooth((t - 1) / 1.0)]
        if 3 <= t < 4.5:
            data.mocap_pos[0, 0] = 0.015 * np.sin(2 * np.pi * (t - 3) / 1.5) ** 3
        target = 0.8 if t >= 4.5 else world.GRIPPER_CLOSED_ON_AIR_RAD
        for name, sign in (("joint6", 1), ("joint6M", -1)):
            joint = model.joint("robot_" + name)
            error = sign * target - data.qpos[joint.qposadr[0]]
            data.qfrc_applied[joint.dofadr[0]] = np.clip(KP_JOINT * error, -GRIPPER_EFFORT_LIMIT, GRIPPER_EFFORT_LIMIT)
        advance(fixture, model.opt.timestep)
        samples.append([data.time, binding.vertices(data)[:, 2].mean()])
    samples = np.asarray(samples)
    assert np.isfinite(samples).all()
    assert samples[(samples[:, 0] > 3) & (samples[:, 0] < 4.5), 1].min() > 0.20
    before_lift = samples[np.argmin(abs(samples[:, 0] - 0.8)), 1]
    after_lift = samples[np.argmin(abs(samples[:, 0] - 2.5)), 1]
    assert after_lift - before_lift > 0.05
    assert samples[-1, 1] < 0.05


def test_separate_worlds_do_not_share_particle_state(fixture):
    from sandbox.cloth_fixture import make_fixture

    model, data, binding = make_fixture(held=True)
    registry = SimpleNamespace(_soft={binding.prop.name: binding}, out={binding.prop.name})
    other = XPBDCloth(model, data, registry)
    model.opt.timestep = other.physics_timestep(0.002)
    first = fixture[-1]
    advance(fixture, 0.02)
    saved = np.asarray(first.particles.getVertices()).copy()
    advance((model, data, binding, registry, other), 0.02)
    np.testing.assert_array_equal(first.particles.getVertices(), saved)
    advance(fixture, 0.02)
    assert np.isfinite(first.particles.getVertices()).all()


def test_missing_native_fails_explicitly_without_path_leak(monkeypatch):
    import sys

    from mars_sim_driver import cloth_xpbd

    before = sys.path.copy()
    monkeypatch.setenv("INNATE_SIM_XPBD_MODULE_DIR", "/nonexistent/innate-xpbd-test")

    def missing(_):
        raise ImportError("missing test dependency")

    monkeypatch.setattr(cloth_xpbd.importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match="No fallback"):
        load_native()
    assert sys.path == before
