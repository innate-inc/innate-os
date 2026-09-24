#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

"""Backend-agnostic occupancy helpers for grid localization."""

import numpy as np


def occupancy_masks(data):
    """Return distinct known-free and occupied masks for an occupancy grid.

    Unknown cells (-1) belong to neither mask. Positive occupancy values retain
    the grid localizer's existing obstacle semantics for trinary and scale maps.
    """
    return (data == 0).astype(np.float32), (data > 0).astype(np.float32)


def endpoint_mismatch_scores(occupied_map, pix_x, pix_y, *, xp=np):
    """Score scan endpoints; only in-bounds occupied cells are matches.

    Coordinates outside the map are sampled safely after clipping, but remain
    mismatches. This prevents map-edge cells from becoming artificial obstacle
    matches for rays that leave the map.
    """
    map_h, map_w = occupied_map.shape
    in_bounds = (pix_x >= 0) & (pix_x < map_w) & (pix_y >= 0) & (pix_y < map_h)
    pix_x_int = xp.clip(pix_x, 0, map_w - 1).astype(xp.int32)
    pix_y_int = xp.clip(pix_y, 0, map_h - 1).astype(xp.int32)
    hit_occupied = xp.where(in_bounds, occupied_map[pix_y_int, pix_x_int], 0.0)
    return 1.0 - xp.mean(hit_occupied, axis=1)
