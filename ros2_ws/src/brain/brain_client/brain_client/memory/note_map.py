# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Bounded annotated map image; cached until notes or robot pose change."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from threading import RLock

import cv2
import numpy as np
import yaml

from brain_client.memory.note_regions import region_metrics


class NoteMapRenderer:
    def __init__(self, data_dir: Path):
        self._maps = (data_dir / "maps").resolve()
        self._lock = RLock()
        self._identity = None
        self._grid = None
        self._cache_key = None
        self._cache = None
        self._version_key = None
        self._version = None

    def frame_version(self, map_name):
        """Include YAML geometry changes even when the occupancy image is identical."""
        with self._lock:
            try:
                path = (self._maps / map_name).resolve()
                if not path.is_relative_to(self._maps):
                    return None
                stat = path.stat()
                key = (str(path), stat.st_mtime_ns, stat.st_size)
                if key != self._version_key:
                    self._version = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                    self._version_key = key
                return self._version
            except (OSError, ValueError):
                return None

    def _load(self, ref):
        if ref == self._identity:
            return self._grid
        self._grid, self._cache_key, self._cache = None, None, None
        self._identity = None
        try:
            path = (self._maps / ref["map"]).resolve()
            if not path.is_relative_to(self._maps):
                return None
            info = yaml.safe_load(path.read_text())
            image_path = (path.parent / info["image"]).resolve()
            if not image_path.is_relative_to(self._maps):
                return None
            image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            resolution = float(info["resolution"])
            origin = tuple(float(x) for x in info["origin"])
            if (
                image is None
                or image.size > 25_000_000
                or not math.isfinite(resolution)
                or resolution <= 0
                or len(origin) != 3
                or not all(math.isfinite(x) for x in origin)
            ):
                return None
            self._grid = (image, resolution, origin)
            self._identity = dict(ref)
            return self._grid
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
            return None

    def _pixel(self, point, grid):
        image, resolution, (ox, oy, yaw) = grid
        x, y = point[0] - ox, point[1] - oy
        return (
            (math.cos(yaw) * x + math.sin(yaw) * y) / resolution,
            image.shape[0] - (math.cos(yaw) * y - math.sin(yaw) * x) / resolution,
        )

    def contains(self, ref, point):
        with self._lock:
            grid = self._load(ref)
            if grid is None:
                return False
            x, y = self._pixel(point, grid)
            return 0 <= x < grid[0].shape[1] and 0 <= y < grid[0].shape[0]

    def image_geometry(self, ref):
        """The full annotated image, including margins, frozen with each observation."""
        with self._lock:
            grid = self._load(ref) if ref else None
            if grid is None:
                return None
            image, resolution, origin = grid
            scale = min(576 / image.shape[1], 448 / image.shape[0])
            w, h = max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))
            return {
                "width": max(w + 48, 400),
                "height": h + 72,
                "map_rect": [24, 36, w, h],
                "grid_size": [image.shape[1], image.shape[0]],
                "resolution": resolution,
                "origin": list(origin),
            }

    def region_from_image(self, ref, vertices, geometry):
        """Convert Astra's 0..1000 image polygon to the saved map's metric frame."""
        if geometry is None or geometry != self.image_geometry(ref):
            raise ValueError("Map image is unavailable or changed")
        if not isinstance(vertices, list) or not 3 <= len(vertices) <= 12:
            raise ValueError("Use 3 to 12 vertices on the map image")
        left, top, w, h = geometry["map_rect"]
        cols, rows = geometry["grid_size"]
        ox, oy, yaw = geometry["origin"]
        resolution = geometry["resolution"]
        region = []
        for vertex in vertices:
            if (
                not isinstance(vertex, dict)
                or set(vertex) != {"x", "y"}
                or any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 1000 for v in vertex.values())
            ):
                raise ValueError("Coordinates must be integers from 0 to 1000")
            u = (vertex["x"] * geometry["width"] / 1000 - left) / w
            v = (vertex["y"] * geometry["height"] / 1000 - top) / h
            if not (0 <= u <= 1 and 0 <= v <= 1):
                raise ValueError("Region vertices must be inside the map, not its caption or margins")
            x, y = u * cols * resolution, (1 - v) * rows * resolution
            region.append(
                [
                    round(ox + math.cos(yaw) * x - math.sin(yaw) * y, 4),
                    round(oy + math.sin(yaw) * x + math.cos(yaw) * y, 4),
                ]
            )
        area, center = region_metrics(region)
        return region, round(area, 4), [round(v, 4) for v in center]

    def render(self, snapshot, pose, selected):
        with self._lock:
            if pose is not None and (len(pose) != 3 or not all(math.isfinite(v) for v in pose)):
                pose = None
            grid = self._load(snapshot["map_ref"])
            if grid is None:
                return None
            pose_key = (
                tuple(round(v / step) for v, step in zip(pose, (0.25, 0.25, 0.26), strict=True)) if pose else None
            )
            key = (snapshot["revision"], pose_key, tuple(n["id"] for n in selected))
            if key == self._cache_key:
                return self._cache
            image, resolution, origin = grid
            geometry = self.image_geometry(snapshot["map_ref"])
            _, _, w, h = geometry["map_rect"]
            canvas = np.full((geometry["height"], geometry["width"], 3), 245, np.uint8)
            canvas[36 : 36 + h, 24 : 24 + w] = cv2.cvtColor(
                cv2.resize(image, (w, h), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR
            )

            def pixel(point):
                x, y = self._pixel(point, grid)
                return round(24 + x * w / image.shape[1]), round(36 + y * h / image.shape[0])

            # One blended layer keeps walls visible, even with many overlapping notes.
            regions = [
                (n, np.array([pixel(p) for p in n["region"]], np.int32)) for n in snapshot["notes"] if n.get("region")
            ]
            tint = canvas.copy()
            for _, polygon in regions:
                cv2.fillPoly(tint, [polygon], (251, 31, 64))
            cv2.addWeighted(tint, 0.12, canvas, 0.88, 0, dst=canvas)
            selected_ids = {n["id"] for n in selected}
            for note, polygon in regions:
                cv2.polylines(canvas, [polygon], True, (200, 80, 60), 2 if note["id"] in selected_ids else 1)

            cv2.putText(
                canvas, "MAP SCRATCHPAD | metres", (16, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (30, 30, 30), 1, cv2.LINE_AA
            )
            for note in snapshot["notes"]:
                x, y = pixel((note["x"], note["y"]))
                if not (24 <= x < 24 + w and 36 <= y < 36 + h):
                    continue
                cv2.circle(canvas, (x, y), 2, (130, 100, 90), -1)
            used = []
            for note in selected:
                x, y = pixel((note["x"], note["y"]))
                if not (24 <= x < 24 + w and 36 <= y < 36 + h):
                    continue
                if note["anchor"] == "observation":
                    cv2.rectangle(canvas, (x - 4, y - 4), (x + 4, y + 4), (235, 60, 60), -1)
                else:
                    cv2.circle(canvas, (x, y), 5, (235, 60, 60), -1)
                label = note["id"] + ": " + note["title"][:24].encode("ascii", "replace").decode()
                size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.36, 1)[0]
                lx = max(2, min(x + 9, canvas.shape[1] - size[0] - 5))
                ly = max(48, min(y - 8, h + 28))
                for _ in range(8):
                    if not any(abs(ly - py) < 16 and abs(lx - px) < max(size[0], pw) for px, py, pw in used):
                        break
                    ly = max(48, ly - 17) if ly > h / 2 else min(h + 28, ly + 17)
                used.append((lx, ly, size[0]))
                cv2.rectangle(canvas, (lx - 2, ly - 12), (lx + size[0] + 2, ly + 3), (255, 255, 255), -1)
                cv2.line(canvas, (x, y), (lx, ly), (190, 150, 140), 1)
                cv2.putText(canvas, label, (lx, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (70, 35, 30), 1, cv2.LINE_AA)
            if pose:
                x, y = pixel(pose)
                heading = pose[2] - origin[2]
                if 0 <= x < canvas.shape[1] and 0 <= y < canvas.shape[0]:
                    cv2.circle(canvas, (x, y), 5, (150, 20, 200), -1)
                    cv2.arrowedLine(
                        canvas,
                        (x, y),
                        (round(x + 18 * math.cos(heading)), round(y - 18 * math.sin(heading))),
                        (150, 20, 200),
                        2,
                    )
            caption = f"origin ({origin[0]:.1f},{origin[1]:.1f}), grid yaw {math.degrees(origin[2]):.0f} deg | width {image.shape[1] * resolution:.1f} m"
            cv2.putText(canvas, caption, (12, h + 57), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (45, 45, 45), 1, cv2.LINE_AA)
            ok, encoded = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 78])
            if not ok:
                return None
            self._cache_key, self._cache = key, encoded.tobytes()
            return self._cache
