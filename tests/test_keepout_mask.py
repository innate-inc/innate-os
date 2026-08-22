import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "ros2_ws" / "src" / "mars_bot" / "mars_nav" / "mars_nav" / "keepout_mask.py"
spec = importlib.util.spec_from_file_location("keepout_mask", MODULE)
keepout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keepout)


def grid(**changes):
    values = {
        "width": 3,
        "height": 2,
        "resolution": 0.05,
        "origin_x": -1.0,
        "origin_y": 2.0,
        "origin_yaw": 0.0,
        "frame_id": "map",
    }
    values.update(changes)
    return keepout.GridSpec(**values)


def test_map_fingerprint_ignores_nothing_that_changes_the_map():
    base = grid()
    cells = [-1, 0, 0, 100, 0, 0]
    fingerprint = keepout.map_fingerprint(base, cells)

    assert keepout.map_fingerprint(base, cells.copy()) == fingerprint
    assert keepout.map_fingerprint(base, [0, 0, 0, 100, 0, 0]) != fingerprint
    assert keepout.map_fingerprint(grid(origin_x=-0.5), cells) != fingerprint


def test_edit_frame_round_trip_binds_exact_map_fingerprint():
    fingerprint = "a" * 64
    encoded = keepout.encode_edit_frame("map", fingerprint)

    assert keepout.decode_edit_frame(encoded) == ("map", fingerprint)


@pytest.mark.parametrize("value", ["map", "map#keepout-map=short", f"map#keepout-map={'g' * 64}"])
def test_edit_frame_rejects_missing_or_invalid_fingerprint(value):
    with pytest.raises(ValueError):
        keepout.decode_edit_frame(value)


def test_binary_mask_validates_size_and_thresholds_values():
    assert keepout.binary_mask([-1, 0, 49, 50, 99, 100], 6) == [0, 0, 0, 100, 100, 100]

    try:
        keepout.binary_mask([0], 2)
    except ValueError as exc:
        assert "expected 2" in str(exc)
    else:
        raise AssertionError("short mask was accepted")


def test_mask_round_trip_is_scoped_to_the_exact_map(tmp_path):
    spec = grid()
    map_hash = keepout.map_fingerprint(spec, [0, 0, 0, 100, 0, 0])
    path = tmp_path / "mask.json.gz"
    keepout.save_mask(path, map_hash, spec, [0, 100, 0, 0, 100, 0])

    assert keepout.load_mask(path, map_hash, spec) == [0, 100, 0, 0, 100, 0]
    assert keepout.load_mask(path, "different", spec) is None
    assert keepout.load_mask(path, map_hash, grid(width=6, height=1)) is None


def test_corrupt_persistence_falls_back_cleanly(tmp_path):
    path = tmp_path / "mask.json.gz"
    path.write_bytes(b"not gzip")

    assert keepout.load_mask(path, "hash", grid()) is None
