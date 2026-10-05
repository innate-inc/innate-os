# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Geometry and strict scene validation for one stationary rehearsal."""

import json
import math


def world_xy(x, y, pose):
    c, s = math.cos(pose.theta), math.sin(pose.theta)
    return pose.x + c * x - s * y, pose.y + s * x + c * y


def body_xy(x, y, pose):
    c, s = math.cos(pose.theta), math.sin(pose.theta)
    dx, dy = x - pose.x, y - pose.y
    return c * dx + s * dy, -s * dx + c * dy


def angle_error(target, current):
    return math.atan2(math.sin(target - current), math.cos(target - current))


def parse_scene(text, pixel_to_floor, tilt, width=640, height=480):
    """Accept exactly three distinct floor targets; never guess missing targets."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    data = json.loads(text)
    socks = data.get("socks")
    if not isinstance(socks, list) or len(socks) != 3:
        raise ValueError("Exactly three visible floor socks are required")
    boxes = data.get("boxes", [])
    if not isinstance(boxes, list):
        raise ValueError("Invalid box list")

    def rect(value):
        if not isinstance(value, list) or len(value) != 4:
            raise ValueError("Invalid detection rectangle")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
            raise ValueError("Non-finite rectangle")
        y0, x0, y1, x1 = value
        if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
            raise ValueError("Rectangle outside image")
        return x0, y0, x1, y1

    boxes = [rect(b) for b in boxes]
    result = []
    for sock in socks:
        desc = sock.get("description")
        if not isinstance(desc, str) or not desc.strip() or len(desc) > 160:
            raise ValueError("Each sock needs a short distinguishing description")
        x0, y0, x1, y1 = rect(sock.get("box_2d"))
        u, v = (x0 + x1) / 2, (y0 + y1) / 2
        # Reject any overlap, not just a centre inside a box.
        if any(min(x1, bx1) > max(x0, bx0) and min(y1, by1) > max(y0, by0) for bx0, by0, bx1, by1 in boxes):
            raise ValueError("A sock overlaps a box; move it onto clear floor")
        xy = pixel_to_floor(u * width / 1000, v * height / 1000, tilt)
        if xy is None or not all(math.isfinite(p) for p in xy) or xy[0] <= 0:
            raise ValueError("Sock cannot be projected onto the floor")
        if any(math.dist(xy, other["xy"]) < 0.07 for other in result):
            raise ValueError("Socks must be distinct and separated by at least 7 cm")
        result.append({"description": desc.strip(), "xy": tuple(xy)})
    return result
