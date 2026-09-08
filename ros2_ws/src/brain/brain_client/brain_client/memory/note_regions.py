# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Small, bounded map polygons; semantic areas are not navigation goals."""

import math


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return (
        abs(_cross(a, b, p)) < 1e-9
        and min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9
        and min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9
    )


def _intersects(a, b, c, d):
    return (_cross(a, b, c) * _cross(a, b, d) < 0 and _cross(c, d, a) * _cross(c, d, b) < 0) or any(
        (_on_segment(a, b, c), _on_segment(a, b, d), _on_segment(c, d, a), _on_segment(c, d, b))
    )


def region_metrics(points):
    """Reject degenerate/self-crossing polygons; return area and an interior label point."""
    if not isinstance(points, list) or not 3 <= len(points) <= 12:
        raise ValueError("Use 3 to 12 region vertices")
    if any(
        not isinstance(p, (list, tuple))
        or len(p) != 2
        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p)
        for p in points
    ) or len({tuple(p) for p in points}) != len(points):
        raise ValueError("Invalid or duplicate region vertices")
    edges = list(zip(points, points[1:] + points[:1], strict=True))
    for i, (a, b) in enumerate(edges):
        # Adjacent edges may share a vertex, but must not fold back over each other.
        c = points[(i + 2) % len(points)]
        if _on_segment(a, b, c) or _on_segment(b, c, a):
            raise ValueError("Overlapping region edges")
        for j in range(i + 2, len(edges)):
            if i == 0 and j == len(edges) - 1:
                continue
            if _intersects(a, b, *edges[j]):
                raise ValueError("Self-crossing region")
    twice_area = sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)
    if abs(twice_area) < 0.02:
        raise ValueError("Region must cover at least 0.01 square metres")
    center = [
        sum((a[axis] + b[axis]) * (a[0] * b[1] - b[0] * a[1]) for a, b in edges) / (3 * twice_area) for axis in (0, 1)
    ]
    if region_distance(points, *center) > 1e-8:
        # A concave polygon's centroid can lie outside it. Use its boundary instead.
        center = [(points[0][i] + points[1][i]) / 2 for i in (0, 1)]
    return abs(twice_area) / 2, center


def region_distance(points, x, y):
    """Distance to the area, zero inside/on its boundary (not distance to its label)."""
    inside, distance = False, math.inf
    for a, b in zip(points, points[1:] + points[:1], strict=True):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = max(0, min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / length2)) if length2 else 0
        distance = min(distance, math.hypot(x - a[0] - t * dx, y - a[1] - t * dy))
        if (a[1] > y) != (b[1] > y) and x < a[0] + (y - a[1]) * dx / dy:
            inside = not inside
    return 0.0 if inside else distance


def note_distance(note, x, y):
    return region_distance(note["region"], x, y) if note.get("region") else math.hypot(note["x"] - x, note["y"] - y)
