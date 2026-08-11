# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import math

import cv2
import numpy as np

from brain_client.perception.map_image import occupancy_grid_raster, render_navigation_map


def test_occupancy_grid_raster_flips_y_and_maps_occupancy_to_grayscale():
    raster = occupancy_grid_raster(
        [-1, 0, 100, 25, 50, 75],
        width=3,
        height=2,
    )

    assert raster is not None
    assert raster.tolist() == [[190, 127, 64], [205, 254, 0]]


def test_occupancy_grid_raster_rejects_invalid_and_null_maps():
    assert occupancy_grid_raster([], width=0, height=0) is None
    assert occupancy_grid_raster([0], width=1, height=1) is None
    assert occupancy_grid_raster([0, 100], width=2, height=2) is None


def test_render_navigation_map_caps_size_and_describes_world_bounds():
    width, height = 600, 300
    image = render_navigation_map(
        [0] * (width * height),
        width=width,
        height=height,
        resolution=0.05,
        origin_x=-10.0,
        origin_y=-4.0,
        origin_yaw=0.0,
        frame_id="map",
    )

    assert image is not None
    decoded = cv2.imdecode(np.frombuffer(image.jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert decoded.shape == (240, 480)
    assert image.jpeg.startswith(b"\xff\xd8")
    note = image.observation_note(3)
    assert "image 3 is the navigation occupancy map" in note
    assert "0.05 m/cell" in note
    assert "white is free, darker means more occupied" in note
    assert "lower-left=(-10.00,-4.00), lower-right=(20.00,-4.00)" in note


def test_render_navigation_map_describes_a_rotated_origin():
    image = render_navigation_map(
        [0, 100, 0, 100],
        width=2,
        height=2,
        resolution=1.0,
        origin_x=1.0,
        origin_y=2.0,
        origin_yaw=math.pi / 2,
        frame_id="map",
    )

    assert image is not None
    note = image.observation_note(2)
    assert "lower-right=(1.00,4.00)" in note
    assert "upper-left=(-1.00,2.00)" in note
    assert "map-local yaw=90.0deg" in note


def test_render_navigation_map_preserves_thin_walls_when_downsampling():
    data = np.zeros((100, 960), dtype=np.int16)
    data[:, 1] = 100
    image = render_navigation_map(
        data.ravel(),
        width=960,
        height=100,
        resolution=0.05,
        origin_x=0.0,
        origin_y=0.0,
        origin_yaw=0.0,
        frame_id="map",
    )

    assert image is not None
    decoded = cv2.imdecode(np.frombuffer(image.jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert decoded.shape == (50, 480)
    assert decoded[:, 0].max() < 10
