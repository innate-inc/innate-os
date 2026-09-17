import numpy as np
from asset_tools.build_soft_sock_asset import _localized_affine_weights


def test_localized_weights_preserve_affine_motion_without_cross_cluster_influence() -> None:
    tetrahedron = np.asarray(
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        )
    )
    controls = np.vstack((tetrahedron, tetrahedron + (10.0, 0.0, 0.0)))
    renders = np.asarray(((0.2, 0.3, 0.1), (10.3, 0.1, 0.2)))

    weights, _, largest_neighborhood = _localized_affine_weights(renders, controls, neighbor_count=4)

    assert largest_neighborhood == 4
    np.testing.assert_allclose(weights.sum(axis=1), 1.0, atol=1.0e-12)
    np.testing.assert_allclose(weights @ controls, renders, atol=1.0e-12)
    np.testing.assert_array_equal(weights[0, 4:], 0.0)
    np.testing.assert_array_equal(weights[1, :4], 0.0)

    affine = np.asarray(((1.2, 0.1, 0.0), (0.0, 0.8, -0.2), (0.1, 0.0, 1.1)))
    translation = np.asarray((2.0, -1.0, 0.5))
    transformed_controls = controls @ affine.T + translation
    transformed_renders = renders @ affine.T + translation
    np.testing.assert_allclose(weights @ transformed_controls, transformed_renders, atol=1.0e-12)
