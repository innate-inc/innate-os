"""Pure keepout-mask validation and persistence helpers."""

import gzip
import hashlib
import json
import os
import struct
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class GridSpec:
    width: int
    height: int
    resolution: float
    origin_x: float
    origin_y: float
    origin_yaw: float
    frame_id: str

    @property
    def cells(self) -> int:
        return self.width * self.height


def map_fingerprint(spec: GridSpec, cells: list[int]) -> str:
    """Stable identity for map geometry + occupancy, excluding ROS stamps."""
    if len(cells) != spec.cells:
        raise ValueError(f"map has {len(cells)} cells, expected {spec.cells}")
    digest = hashlib.sha256()
    digest.update(
        struct.pack(
            "<IIdddd",
            spec.width,
            spec.height,
            spec.resolution,
            spec.origin_x,
            spec.origin_y,
            spec.origin_yaw,
        )
    )
    digest.update(spec.frame_id.encode("utf-8"))
    digest.update(bytes((int(value) + 1) & 0xFF for value in cells))
    return digest.hexdigest()


def compatible(actual: GridSpec, expected: GridSpec, tolerance: float = 1e-6) -> bool:
    return (
        actual.width == expected.width
        and actual.height == expected.height
        and actual.frame_id == expected.frame_id
        and abs(actual.resolution - expected.resolution) <= tolerance
        and abs(actual.origin_x - expected.origin_x) <= tolerance
        and abs(actual.origin_y - expected.origin_y) <= tolerance
        and abs(actual.origin_yaw - expected.origin_yaw) <= tolerance
    )


def binary_mask(cells: list[int], expected_cells: int) -> list[int]:
    """Validate a full-grid edit and reduce it to Nav2's 0/100 mask."""
    if len(cells) != expected_cells:
        raise ValueError(f"mask has {len(cells)} cells, expected {expected_cells}")
    return [100 if int(value) >= 50 else 0 for value in cells]


def save_mask(path: Path, map_hash: str, spec: GridSpec, cells: list[int]) -> None:
    data = binary_mask(cells, spec.cells)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "map_hash": map_hash, "grid": asdict(spec), "data": data}
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    try:
        with gzip.open(tmp_name, "wt", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_mask(path: Path, map_hash: str, spec: GridSpec) -> list[int] | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        stored_spec = GridSpec(**payload["grid"])
        if payload.get("version") != 1 or payload.get("map_hash") != map_hash or not compatible(stored_spec, spec):
            return None
        return binary_mask(payload["data"], spec.cells)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
