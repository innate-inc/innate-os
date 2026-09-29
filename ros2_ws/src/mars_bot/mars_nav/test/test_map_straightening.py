"""A straightened map and the poses carried into it must agree on where the walls are."""

import math

import cv2
import numpy as np
import pytest
import yaml

from mars_nav.map_straightening import OCCUPIED, UNKNOWN, straighten_saved_map, turn_pose

RESOLUTION = 0.05
ORIGIN = (-3.0, -3.0)
SIZE = 200


def _pixel(x: float, y: float, origin: tuple[float, float], height: int) -> tuple[int, int]:
    return int((x - origin[0]) / RESOLUTION), height - 1 - int((y - origin[1]) / RESOLUTION)


@pytest.mark.parametrize("tilt_deg", [20.0, -35.0])
def test_turned_poses_land_on_the_straightened_walls(tmp_path, tilt_deg):
    c, s = math.cos(math.radians(tilt_deg)), math.sin(math.radians(tilt_deg))
    centre = (1.0, 0.5)
    corners = [
        (centre[0] + dx * c - dy * s, centre[1] + dx * s + dy * c) for dx, dy in ((-2, -2), (2, -2), (2, 2), (-2, 2))
    ]
    image = np.full((SIZE, SIZE), UNKNOWN, np.uint8)
    outline = np.array([_pixel(x, y, ORIGIN, SIZE) for x, y in corners], np.int32)
    cv2.fillPoly(image, [outline], (254,))
    cv2.polylines(image, [outline], True, (OCCUPIED,))
    cv2.imwrite(str(tmp_path / "room.pgm"), image)
    meta = {"image": "room.pgm", "resolution": RESOLUTION, "origin": [*ORIGIN, 0.0], "negate": 0}
    (tmp_path / "room.yaml").write_text(yaml.safe_dump(meta))

    rotation = straighten_saved_map(tmp_path / "room.yaml")

    assert math.degrees(rotation) == pytest.approx(-tilt_deg, abs=1.0)  # the drawn corners snap to cells
    straight = cv2.imread(str(tmp_path / "room.pgm"), cv2.IMREAD_UNCHANGED)
    rim = np.pad(straight, 1, constant_values=UNKNOWN)  # the crop puts walls on the image edge
    origin = tuple(yaml.safe_load((tmp_path / "room.yaml").read_text())["origin"][:2])
    for (ax, ay), (bx, by) in zip(corners, corners[1:] + corners[:1], strict=True):
        x, y, _ = turn_pose((ax + bx) / 2, (ay + by) / 2, 0.0, rotation)
        col, row = _pixel(x, y, origin, straight.shape[0])
        assert (rim[row : row + 3, col : col + 3] == OCCUPIED).any()
    x, y, _ = turn_pose(*centre, 0.0, rotation)
    col, row = _pixel(x, y, origin, straight.shape[0])
    assert straight[row, col] == 254
