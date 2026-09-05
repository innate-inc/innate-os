# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Authored Crossroads layout, signals and car model, shared by both renderers."""

import math

ROAD_HALF_WIDTH = 3.5
MAP_HALF_SIZE = 20.0
CROSSING_CENTER = ROAD_HALF_WIDTH + 1.8
CROSSING_WIDTH = 1.5
CURB_CUT_HALF_WIDTH = 0.8
STOP_LINE_CENTER = CROSSING_CENTER + CROSSING_WIDTH / 2 + 0.45
STOP_LINE_WIDTH = 0.22
SIGNAL_ALONG = STOP_LINE_CENTER + 0.3
SIGNAL_ACROSS = ROAD_HALF_WIDTH + 0.4
CAR_LENGTH_M = 3.6
CAR_WIDTH_M = 1.55
LANE_CENTER = ROAD_HALF_WIDTH / 2
STOP_CENTER = STOP_LINE_CENTER + STOP_LINE_WIDTH / 2 + 0.1 + CAR_LENGTH_M / 2
JUNCTION_HALF_SIZE = STOP_LINE_CENTER - STOP_LINE_WIDTH / 2 - 0.04
ROUTE_END = MAP_HALF_SIZE + CAR_LENGTH_M / 2 + 1.2

NS = "north_south"
EW = "east_west"
RED = "red"
YELLOW = "yellow"
GREEN = "green"

SIGNAL_MATERIALS = {
    NS: {RED: "Signal_NS_Red", YELLOW: "Signal_NS_Yellow", GREEN: "Signal_NS_Green"},
    EW: {RED: "Signal_EW_Red", YELLOW: "Signal_EW_Yellow", GREEN: "Signal_EW_Green"},
}

SIGNAL_COLORS = {RED: "#ff4b55", YELLOW: "#ffd45a", GREEN: "#5ee27a"}

# build_traffic_car exports these primitives to GLB; _part_xml converts the
# same descriptors to MJCF half-sizes and degrees.
CAR_MODEL = {
    "length": CAR_LENGTH_M,
    "width": CAR_WIDTH_M,
    "parts": [
        # Leave the lamps slightly proud of the body.  Coplanar outer faces
        # shimmer badly at distance in both MuJoCo and Three's depth buffers.
        {
            "shape": "prism",
            "width": 1.55,
            "position": [0, 0, 0],
            "material": "body",
            "profile": [
                [-1.77, 0.32],
                [-1.60, 0.20],
                [1.62, 0.20],
                [1.77, 0.32],
                [1.70, 0.62],
                [1.35, 0.77],
                [-1.35, 0.77],
                [-1.73, 0.61],
            ],
        },
        {
            "shape": "prism",
            "width": 1.28,
            "position": [0, 0, 0],
            "material": "body",
            "profile": [[-1.02, 0.76], [0.76, 0.76], [0.34, 1.27], [-0.62, 1.27]],
        },
        {"shape": "box", "size": [1.01, 1.30, 0.055], "position": [-0.14, 0, 1.285], "material": "body"},
        {
            "shape": "box",
            "size": [0.035, 1.14, 0.48],
            "position": [0.568, 0, 1.027],
            "rotation": [0, -math.atan2(0.42, 0.51), 0],
            "material": "glass",
        },
        {
            "shape": "box",
            "size": [0.035, 1.14, 0.47],
            "position": [-0.835, 0, 1.027],
            "rotation": [0, math.atan2(0.40, 0.51), 0],
            "material": "glass",
        },
        *(
            {
                "shape": "prism",
                "width": 0.035,
                "position": [0, y, 0],
                "material": "glass",
                "profile": [[-0.89, 0.83], [0.60, 0.83], [0.28, 1.20], [-0.56, 1.20]],
            }
            for y in (-0.657, 0.657)
        ),
        *(
            {
                "shape": "cylinder",
                "radius": 0.30,
                "length": 0.18,
                "position": [x, y, 0.30],
                "rotation": [math.pi / 2, 0.0, 0.0],
                "material": "rubber",
                "rolling_radius": 0.30,
            }
            for x in (-1.125, 1.125)
            # Keep the outer wheel caps slightly proud of the 1.55 m body.
            # Flush caps are coplanar with its sides and z-fight in Three.
            for y in (-0.710, 0.710)
        ),
        *(
            {
                "shape": "cylinder",
                "radius": radius,
                "length": 0.018,
                "position": [x, sign * y, 0.30],
                "rotation": [math.pi / 2, 0, 0],
                "material": "alloy",
                "rolling_radius": 0.30,
            }
            for x in (-1.125, 1.125)
            for sign in (-1, 1)
            for radius, y in ((0.215, 0.815), (0.055, 0.842))
        ),
        *(
            {"shape": "box", "size": size, "position": [x, y, 0.30], "material": "rubber", "rolling_radius": 0.30}
            for x in (-1.125, 1.125)
            for y in (-0.83, 0.83)
            for size in ([0.32, 0.012, 0.075], [0.075, 0.012, 0.32])
        ),
        *(
            {"shape": "box", "size": [0.065, 0.025, 0.40], "position": [-0.18, y, 1.01], "material": "body"}
            for y in (-0.682, 0.682)
        ),
        *(
            {"shape": "box", "size": [0.20, 0.035, 0.045], "position": [0.25, y, 0.67], "material": "alloy"}
            for y in (-0.79, 0.79)
        ),
        *(
            {"shape": "box", "size": [0.06, 1.40, 0.12], "position": [x, 0, 0.32], "material": "alloy"}
            for x in (-1.78, 1.78)
        ),
        {"shape": "box", "size": [0.05, 0.32, 0.18], "position": [1.775, 0.46, 0.52], "material": "headlight"},
        {"shape": "box", "size": [0.05, 0.32, 0.18], "position": [1.775, -0.46, 0.52], "material": "headlight"},
        {"shape": "box", "size": [0.05, 0.28, 0.17], "position": [-1.775, 0.48, 0.52], "material": "taillight"},
        {"shape": "box", "size": [0.05, 0.28, 0.17], "position": [-1.775, -0.48, 0.52], "material": "taillight"},
    ],
    "colliders": [
        {"shape": "box", "size": [3.60, 1.56, 0.73], "position": [0.0, 0.0, 0.365]},
        {"shape": "box", "size": [1.70, 1.32, 0.60], "position": [-0.10, 0.0, 1.00]},
    ],
    "materials": {
        "glass": "#263641",
        "rubber": "#202328",
        "headlight": "#fff0a8",
        "taillight": "#d7353f",
        "alloy": "#b6c1c8",
    },
}
