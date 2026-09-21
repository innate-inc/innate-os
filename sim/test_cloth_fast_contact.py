from itertools import product
from types import SimpleNamespace

import numpy as np
import pytest
import sandbox._driver_pkg  # noqa: F401
from mars_sim_driver.cloth_fast_contact import FastContacts


def box_fixture(dofs=0):
    box = np.array(list(product((-0.05, 0.05), repeat=3)))
    cloth = np.array([[-0.01, -0.01, 0.0505], [0.01, -0.01, 0.0505], [0, 0.01, 0.0505]])
    data = SimpleNamespace(geom_xpos=np.zeros((1, 3)), geom_xmat=np.eye(3).reshape(1, 9))
    solver = FastContacts(None, data, [(0, box)], np.array([[0, 1, 2]]), np.ones(3), iterations=24)
    previous = np.vstack([cloth, box])
    jac = np.zeros((8, 3, dofs))
    if dofs:
        jac[:] = np.eye(3)
    return solver, previous, jac, np.eye(dofs) / 3


@pytest.mark.parametrize("dofs", [0, 3])
def test_convex_contact_pushes_out_and_transfers_momentum(dofs):
    solver, previous, jac, inverse = box_fixture(dofs)
    predicted = previous.copy()
    predicted[:3] += [0.0001, 0, -0.001]
    cloth, delta, stats = solver.project(previous, predicted, jac, inverse)
    assert stats["active"] > 0
    assert cloth[:, 2].min() > predicted[:3, 2].min()
    if dofs:
        np.testing.assert_allclose((cloth - predicted[:3]).sum(0) + 3 * delta, 0, atol=1e-12)
    else:
        assert cloth[:, 2].min() >= 0.05024


def test_opening_contact_does_not_attach():
    solver, previous, jac, inverse = box_fixture()
    predicted = previous.copy()
    predicted[:3, 2] += 0.01
    cloth, delta, stats = solver.project(previous, predicted, jac, inverse)
    np.testing.assert_array_equal(cloth, predicted[:3])
    assert stats["active"] == 0 and delta.size == 0


def test_penetration_keeps_entry_side_instead_of_exiting_opposite_face():
    solver, previous, jac, inverse = box_fixture()
    predicted = previous.copy()
    predicted[:3, 2] = -0.04  # Inside, but closer to the opposite face.
    cloth, _, _ = solver.project(previous, predicted, jac, inverse)
    assert cloth[:, 2].min() >= 0.05024


def test_nearby_non_neighbour_particles_repel_without_changing_mass_center():
    lower = np.array([[-0.01, -0.01, 0], [0.01, -0.01, 0], [0, 0.01, 0]])
    previous = np.vstack([lower, lower + [0, 0, 0.0005]])
    predicted = previous.copy()
    predicted[3:, 2] = 0.0001
    solver = FastContacts(None, None, [], np.array([[0, 1, 2], [3, 4, 5]]), np.ones(6))
    cloth, _, stats = solver.project(previous, predicted, np.zeros((0, 3, 0)), np.zeros((0, 0)))
    assert stats["active"] > 0
    np.testing.assert_allclose(cloth.mean(0), predicted.mean(0), atol=1e-12)
    assert np.min(cloth[3:, 2] - cloth[:3, 2]) >= 0.000249


def test_invalid_state_rejected():
    solver, previous, jac, inverse = box_fixture()
    invalid = previous.copy()
    invalid[0, 0] = np.nan
    with pytest.raises(ValueError):
        solver.project(previous, invalid, jac, inverse)


def test_strain_projection_preserves_mass_center_without_pinning():
    rest = np.array([[0.0, 0, 0], [0.01, 0, 0], [0, 0.01, 0]])
    masses = np.array([1.0, 2, 3])
    fast = FastContacts(None, None, [], np.array([[0, 1, 2]]), masses, rest_vertices=rest)
    predicted = rest * 2
    actual, _, _ = fast.project(rest, predicted, np.zeros((0, 3, 0)), np.zeros((0, 0)))
    np.testing.assert_allclose((actual * masses[:, None]).sum(0), (predicted * masses[:, None]).sum(0), atol=1e-12)
    lengths = np.linalg.norm(actual[fast.edges[:, 0]] - actual[fast.edges[:, 1]], axis=1)
    assert np.max(lengths / fast.rest_lengths) < 1.16


def test_unused_coordinate_keeps_off_diagonal_inertia_response():
    reference, previous, jac, _ = box_fixture(3)
    jac[:, :, 1:] = 0
    inverse = np.array([[1.0, 0.2, 0.1], [0.2, 1.0, 0.0], [0.1, 0.0, 1.0]])
    # Let x coordinate move the box normally (z), with inertia coupling to
    # two coordinates absent from the contact Jacobian.
    jac[:] = 0
    jac[:, 2, 0] = 1
    fast = FastContacts(None, reference.data, [(0, previous[3:])], np.array([[0, 1, 2]]), np.ones(3), iterations=24)
    predicted = previous.copy()
    predicted[:3, 2] -= 0.001
    actual, actual_delta, _ = fast.project(previous, predicted, jac, inverse)
    assert actual[:, 2].min() > predicted[:3, 2].min()
    np.testing.assert_allclose(actual_delta[1:], actual_delta[0] * np.array([0.2, 0.1]), atol=1e-12)
    assert abs(actual_delta[1]) > 0


@pytest.mark.parametrize(
    "faces", [np.array([[0, 1, 3]]), np.array([[0, -1, 2]]), np.array([[0.0, 1.0, 2.0]]), np.array([0, 1, 2])]
)
def test_invalid_topology_rejected_before_native_kernel(faces):
    with pytest.raises(ValueError, match="vertex-index"):
        FastContacts(None, None, [], faces, np.ones(3))


def test_batch_native_state_validation_is_atomic():
    from mars_sim_driver.cloth_xpbd import load_native

    pbd = load_native()
    sim = pbd.Simulation.getCurrent()
    sim.initDefault()
    model = sim.getModel()
    points = np.array([[0.0, 0, 0], [0.01, 0, 0], [0, 0, 0.01]])
    model.addTriangleModel(points.tolist(), [0, 1, 2], testMesh=False)
    particles = model.getParticles()
    if not hasattr(particles, "setStateArrays"):
        pytest.skip("Requires optional batch-state native patch")
    velocities = np.ones_like(points) * 0.2
    particles.setStateArrays(points, velocities)
    np.testing.assert_allclose(particles.getVertices(), points)
    np.testing.assert_allclose([particles.getVelocity(i) for i in range(3)], velocities)
    for bad in (np.zeros((2, 3)), np.full((3, 3), np.nan)):
        with pytest.raises(ValueError):
            particles.setStateArrays(points + 1, bad)
        np.testing.assert_allclose(particles.getVertices(), points)
