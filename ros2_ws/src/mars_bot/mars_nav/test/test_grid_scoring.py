# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import numpy as np

from mars_nav.grid_scoring import endpoint_mismatch_scores, occupancy_masks


def test_only_occupied_in_bounds_endpoints_are_matches():
    cells = np.array([[0, -1, 100]], dtype=np.int8)
    known_free, occupied = occupancy_masks(cells)

    np.testing.assert_array_equal(known_free, [[1.0, 0.0, 0.0]])
    np.testing.assert_array_equal(occupied, [[0.0, 0.0, 1.0]])

    pix_x = np.array([[2.0, 1.0, 0.0, 3.0, -1.0], [2.0, 2.0, 2.0, 2.0, 2.0]], dtype=np.float32)
    pix_y = np.zeros_like(pix_x)
    scores = endpoint_mismatch_scores(occupied, pix_x, pix_y)

    # First candidate matches only the occupied cell. Unknown, free, and both
    # out-of-bounds endpoints are mismatches; the second is a perfect match.
    np.testing.assert_allclose(scores, [0.8, 0.0])
