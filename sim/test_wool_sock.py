"""Independent wool preset, multi-prop lifecycle, and measured bending response."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import sandbox._driver_pkg  # noqa: F401
from mars_sim_driver.cloth_xpbd import TO_PBD, XPBDCloth, load_native
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.props import load_props
from mars_sim_driver.softbody import SoftProp


def test_both_socks_have_independent_state_and_stream_ids(monkeypatch):
    monkeypatch.setenv("INNATE_SIM_CLOTH_BACKEND", "xpbd")
    sim = VirtualMars()
    try:
        cotton, wool = [sim.props.props[n] for n in ("soft_sock", "wool_sock")]
        assert cotton.mass == 0.025 and wool.mass == 0.060
        assert cotton.xpbd_bend_stiffness == 1e-5
        assert wool.xpbd_bend_stiffness > cotton.xpbd_bend_stiffness
        assert wool.xpbd_contact_thickness > cotton.xpbd_contact_thickness
        assert sim._cloth.physics_timestep(0.002) == 0.002
        assert sim.place_prop_at_robot("soft_sock")
        sim.step(0.02)
        cotton_native = sim._cloth.cloths["soft_sock"].native
        assert sim.place_prop_at_robot("wool_sock")
        wool_binding = sim.props._soft["wool_sock"]
        # Keep the two props apart: cross-prop cloth contact is not implemented.
        wool_binding.set_pose(sim.data, 0, 0, 0.5, 0)
        sim.step(0.03)
        assert {frame[0] for frame in sim.props.deformable_frames(sim.data)} == {1, 2}
        assert sim._cloth.cloths["wool_sock"].native is not cotton_native
        sim.remove_prop("wool_sock")
        sim.step(0.01)
        assert {frame[0] for frame in sim.props.deformable_frames(sim.data)} == {1}
        assert not sim._cloth.cloths["wool_sock"]._was_active
        assert sim.place_prop_at_robot("wool_sock")
        sim.step(0.01)
        assert np.isfinite(wool_binding.vertices(sim.data)).all()
        sim.reset()
        assert not sim.deformable_frames()
        assert sim._cloth.physics_timestep(0.002) == 0.002
    finally:
        sim.close()


def cantilever_sag(name):
    from sandbox.cloth_fixture import make_fixture

    props = load_props([Path(__file__).parent / "props"])
    model, data, binding = make_fixture(prop=props[name])
    backend = XPBDCloth(model, data, SimpleNamespace(_soft={name: binding}, out={name}))
    rest = binding.prop.cloth_data()["vertices"]
    horizontal = np.column_stack((rest[:, 2], rest[:, 0], 0.4 + rest[:, 1]))
    backend.particles.setStateArrays(horizontal @ TO_PBD.T, np.zeros_like(horizontal))
    pinned = rest[:, 2] < rest[:, 2].min() + 0.025
    for i in np.flatnonzero(pinned):
        backend.particles.setMass(int(i), 0)
    backend.pbd.TimeManager.getCurrent().setTimeStepSize(1 / 300)
    for _ in range(300):
        backend.stepper.step(backend.native)
    points = np.asarray(backend.particles.getVertices()) @ TO_PBD
    tip = rest[:, 2] > rest[:, 2].max() - 0.025
    return float(0.4 - points[tip, 2].mean())


def test_wool_resists_gravity_bending_more_despite_extra_mass():
    load_native()
    cotton, wool = cantilever_sag("soft_sock"), cantilever_sag("wool_sock")
    print(f"cantilever tip sag: cotton={cotton:.5f}m wool={wool:.5f}m")
    assert cotton > 0.01
    assert wool < cotton * 0.8


def crease_recovery(name, panel=None):
    """Release a 150-degree fold in the actual sewn pattern for 33 ms."""
    from sandbox.cloth_fixture import make_fixture

    props = load_props([Path(__file__).parent / "props"])
    if panel is not None:
        props[name].xpbd_panel_bend_stiffness = panel
    model, data, binding = make_fixture(prop=props[name])
    backend = XPBDCloth(model, data, SimpleNamespace(_soft={name: binding}, out={name}))
    rest = binding.prop.cloth_data()["vertices"]
    p = rest.copy()
    mid = np.median(p[:, 2])
    mask = p[:, 2] > mid
    y, z = p[mask, 1].copy(), p[mask, 2] - mid
    angle = np.deg2rad(150)
    p[mask, 1] = y * np.cos(angle) - z * np.sin(angle)
    p[mask, 2] = mid + y * np.sin(angle) + z * np.cos(angle)
    p[:, 2] += 0.5
    backend.particles.setStateArrays(p @ TO_PBD.T, np.zeros_like(p))
    backend.pbd.TimeManager.getCurrent().setTimeStepSize(1 / 300)
    for _ in range(10):
        backend.stepper.step(backend.native)
    final = np.asarray(backend.particles.getVertices()) @ TO_PBD
    faces = binding.prop.cloth_data()["faces"]
    from mars_sim_driver.cloth_xpbd import panel_bending_pairs

    pairs = np.array(panel_bending_pairs(rest, faces))
    initial_lengths = np.linalg.norm(rest[pairs[:, 0]] - rest[pairs[:, 1]], axis=1)
    final_lengths = np.linalg.norm(final[pairs[:, 0]] - final[pairs[:, 1]], axis=1)
    # Shortened panel spans quantify the remaining sharp fold, not translation.
    return float(np.mean(np.maximum(0, 1 - final_lengths / initial_lengths)))


def test_wool_releases_a_sharp_crease_more_than_previous_preset():
    load_native()
    before = crease_recovery("wool_sock", panel=0)
    after = crease_recovery("wool_sock")
    print("crease compression before/after", before, after)
    assert after < before * 0.8


def test_panel_spans_never_join_opposite_layers_and_ignore_world_pose():
    from mars_sim_driver.cloth_xpbd import panel_bending_pairs

    square = np.array([[0, 0, 0], [0.03, 0, 0], [0.03, 0.03, 0], [0, 0.03, 0]])
    vertices = np.vstack([square, square + [0, 0, 0.001]])
    faces = np.array([[0, 1, 2], [0, 2, 3], [6, 5, 4], [7, 6, 4]])
    pairs = panel_bending_pairs(vertices, faces)
    assert pairs
    assert all((a < 4) == (b < 4) for a, b in pairs)
    rotated = vertices[:, [2, 0, 1]] + [2, 3, 4]
    assert panel_bending_pairs(rotated, faces) == pairs


@pytest.mark.parametrize(
    "field,value",
    [
        ("xpbd_bend_stiffness", 0),
        ("xpbd_bend_stiffness", np.nan),
        ("xpbd_bending_model", 2),
        ("xpbd_panel_bend_stiffness", -1),
        ("xpbd_panel_bend_stiffness", np.nan),
        ("xpbd_contact_thickness", -0.001),
        ("xpbd_contact_thickness", 0.003),
    ],
)
def test_invalid_material_settings_rejected(field, value):
    with pytest.raises(ValueError):
        SoftProp(name="invalid", data="missing", deformable_id=7, **{field: value})
