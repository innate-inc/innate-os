# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Live guidance for an operator moving a calibration board through the frame.

Pure functions — no ROS, no I/O, no OpenCV state. Answers the two questions the
capture screen asks on every frame: is the board at a useful size, and what is
still missing before this run will calibrate well.

Every decision here is made on frame-relative quantities, because this runs
*during* calibration and there are no intrinsics yet to convert with. Distances
in metres appear only as display text, derived from the lens's nominal field of
view — approximate on purpose, and never an input to a threshold.
"""

from dataclasses import dataclass

import numpy as np

# Near limit, as board diagonal over frame diagonal. Board-independent: this is
# about fitting inside the frame, and past it corners start leaving the image
# rather than degrading.
EXTENT_MAX = 0.85

# Far limit, as pixels per square needed to still detect. Plain corners survive
# on far less than ArUco markers, which have to resolve 6x6 cells of payload
# well enough to decode — which is why the same paper reaches roughly three
# times further as a checkerboard than as a ChArUco board.
CHECKERBOARD_MIN_PIXELS_PER_SQUARE = 10.0
CHARUCO_MIN_PIXELS_PER_SQUARE = 20.0

# Used only when no board geometry was supplied, so the hint degrades to a
# generic "very small in frame" rather than being silently disabled.
DEFAULT_EXTENT_MIN = 0.12

TOO_FAR = "TOO_FAR"
TOO_CLOSE = "TOO_CLOSE"
IN_RANGE = "IN_RANGE"


@dataclass(frozen=True)
class BoardGeometry:
    """The printed target, reduced to what sets the usable range.

    ``squares`` counts the span of the DETECTED corner grid in squares, not the
    printed square count: a 9x6 inner-corner checkerboard spans 8x5 squares, and
    a 17x9 ChArUco board yields corners spanning 16x8.
    """

    squares: tuple[int, int]
    square_size_m: float
    min_pixels_per_square: float

    @staticmethod
    def checkerboard(pattern: tuple[int, int], square_size_m: float) -> "BoardGeometry":
        return BoardGeometry(
            squares=(pattern[0] - 1, pattern[1] - 1),
            square_size_m=square_size_m,
            min_pixels_per_square=CHECKERBOARD_MIN_PIXELS_PER_SQUARE,
        )

    @staticmethod
    def charuco(squares_x: int, squares_y: int, square_size_m: float) -> "BoardGeometry":
        return BoardGeometry(
            squares=(squares_x - 1, squares_y - 1),
            square_size_m=square_size_m,
            min_pixels_per_square=CHARUCO_MIN_PIXELS_PER_SQUARE,
        )

    @property
    def diagonal_squares(self) -> float:
        return float(np.hypot(*self.squares))

    @property
    def diagonal_m(self) -> float:
        return self.diagonal_squares * self.square_size_m

    def extent_min(self, frame_diagonal_px: float) -> float:
        """Smallest usable board diagonal, as a fraction of the frame diagonal."""
        if frame_diagonal_px <= 0.0:
            return 0.0
        return self.diagonal_squares * self.min_pixels_per_square / frame_diagonal_px

    def range_m(self, focal_px: float, frame_diagonal_px: float) -> tuple[float, float]:
        """(nearest, furthest) the board can be held, in metres.

        Approximate by construction — ``focal_px`` comes from the lens's nominal
        field of view, not from a calibration, because the calibration is what
        this is trying to produce. For telling an operator "hold it around 30cm"
        that is accurate enough; nothing downstream consumes it.
        """
        if focal_px <= 0.0 or frame_diagonal_px <= 0.0:
            return 0.0, 0.0
        span = focal_px * self.diagonal_m / frame_diagonal_px
        return span / EXTENT_MAX, span / self.extent_min(frame_diagonal_px)


# A board this foreshortened counts as tilted. Tilt is normalised by extent (see
# measure), which makes it ~0.0143 per degree off square regardless of range, so
# 0.28 is about 20 degrees — enough to separate focal length from distance.
TILT_MIN = 0.28

ZONE_GRID = 3
CAPTURES_PER_ZONE = 1
CAPTURES_PER_SCALE_BAND = 2
CAPTURES_TILTED = 6

ZONE_LABELS = (
    ("top-left", "top-centre", "top-right"),
    ("mid-left", "centre", "mid-right"),
    ("bottom-left", "bottom-centre", "bottom-right"),
)
SCALE_LABELS = ("close", "mid-range", "far")


@dataclass(frozen=True)
class BoardView:
    """One detected board, reduced to what coverage actually depends on.

    ``extent_min`` rides along rather than being a module constant because it
    depends on the target: the same sheet of letter paper reaches much further
    as a checkerboard than as a ChArUco board.
    """

    centroid_norm: tuple[float, float]
    extent: float
    tilt: float
    pixels_per_square: float = 0.0
    extent_min: float = DEFAULT_EXTENT_MIN
    approx_distance_m: float = 0.0

    @property
    def hint(self) -> str:
        if self.extent < self.extent_min:
            return TOO_FAR
        if self.extent > EXTENT_MAX:
            return TOO_CLOSE
        return IN_RANGE

    @property
    def usable(self) -> bool:
        return self.hint == IN_RANGE

    @property
    def zone(self) -> tuple[int, int]:
        """Which cell of the 3x3 grid the board centre falls in."""
        col = min(ZONE_GRID - 1, max(0, int(self.centroid_norm[0] * ZONE_GRID)))
        row = min(ZONE_GRID - 1, max(0, int(self.centroid_norm[1] * ZONE_GRID)))
        return row, col

    @property
    def scale_band(self) -> int:
        """0 = filling the frame, 2 = small in frame, across the usable range."""
        span = max(1e-6, (EXTENT_MAX - self.extent_min) / 3.0)
        return min(2, max(0, int((EXTENT_MAX - self.extent) / span)))


def measure(
    image_points: np.ndarray,
    board_points: np.ndarray,
    frame_size: tuple[int, int],
    geometry: BoardGeometry | None = None,
    focal_px: float = 0.0,
) -> BoardView | None:
    """Reduce one detection to a BoardView, or None if the geometry is degenerate.

    ``board_points`` are the same corners in board coordinates, which is what
    lets a partial ChArUco detection be measured the same way as a complete
    checkerboard: the homography is fitted from whatever correspondences exist
    and then asked about the board's own outline.
    """
    image_xy = np.asarray(image_points, dtype=np.float64).reshape(-1, 2)
    board_xy = np.asarray(board_points, dtype=np.float64).reshape(-1, 3)[:, :2]
    if len(image_xy) < 4 or len(image_xy) != len(board_xy):
        return None

    width, height = frame_size
    if width <= 0 or height <= 0:
        return None

    homography = _fit_homography(board_xy, image_xy)
    if homography is None:
        return None

    outline = _project(homography, _board_outline(board_xy))
    if outline is None:
        return None

    centroid = image_xy.mean(axis=0)
    lo, hi = image_xy.min(axis=0), image_xy.max(axis=0)
    frame_diagonal = float(np.hypot(width, height))
    extent = float(np.hypot(*(hi - lo)) / frame_diagonal)
    if extent < 1e-6:
        return None

    diagonal_px = extent * frame_diagonal
    pixels_per_square = diagonal_px / geometry.diagonal_squares if geometry else 0.0
    extent_min = geometry.extent_min(frame_diagonal) if geometry else DEFAULT_EXTENT_MIN
    distance = focal_px * geometry.diagonal_m / diagonal_px if geometry and focal_px > 0.0 else 0.0

    # Raw foreshortening shrinks with range for the same physical tilt, because
    # it depends on the board's depth spread relative to its distance. Dividing
    # by extent cancels that: the result is ~0.0143 per degree off square at any
    # distance, so one threshold works across the whole usable range.
    return BoardView(
        centroid_norm=(float(centroid[0] / width), float(centroid[1] / height)),
        extent=extent,
        tilt=_foreshortening(outline) / extent,
        pixels_per_square=float(pixels_per_square),
        extent_min=float(extent_min),
        approx_distance_m=float(distance),
    )


def _board_outline(board_xy: np.ndarray) -> np.ndarray:
    """The board's own bounding rectangle, corners ordered TL, TR, BR, BL."""
    lo, hi = board_xy.min(axis=0), board_xy.max(axis=0)
    return np.array([[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]], dtype=np.float64)


def _foreshortening(quad: np.ndarray) -> float:
    """How much opposite edges disagree in length — 0 when square to the camera.

    Perspective is the only thing that can shorten one edge relative to the one
    opposite it, so this isolates tilt from board size, board position and
    in-plane rotation, none of which change the ratio.
    """
    top = np.linalg.norm(quad[1] - quad[0])
    bottom = np.linalg.norm(quad[2] - quad[3])
    left = np.linalg.norm(quad[3] - quad[0])
    right = np.linalg.norm(quad[2] - quad[1])
    if min(top, bottom, left, right) <= 1e-9:
        return 0.0
    return float(max(abs(np.log(top / bottom)), abs(np.log(left / right))))


def _fit_homography(source: np.ndarray, target: np.ndarray) -> np.ndarray | None:
    """Direct linear transform, normalised — no cv2 so this stays unit-testable."""
    src, t_src = _normalise(source)
    dst, t_dst = _normalise(target)
    if src is None or dst is None:
        return None

    rows = []
    for (x, y), (u, v) in zip(src, dst):  # noqa: B905 — equal length checked by the caller
        rows.append([-x, -y, -1.0, 0.0, 0.0, 0.0, u * x, u * y, u])
        rows.append([0.0, 0.0, 0.0, -x, -y, -1.0, v * x, v * y, v])

    _, _, vt = np.linalg.svd(np.asarray(rows, dtype=np.float64))
    normalised = vt[-1].reshape(3, 3)
    homography = np.linalg.inv(t_dst) @ normalised @ t_src
    if abs(homography[2, 2]) < 1e-12:
        return None
    return homography / homography[2, 2]


def _normalise(points: np.ndarray) -> tuple[np.ndarray | None, np.ndarray]:
    """Hartley normalisation: centre on the mean, scale to mean distance sqrt(2)."""
    centre = points.mean(axis=0)
    centred = points - centre
    spread = float(np.mean(np.hypot(centred[:, 0], centred[:, 1])))
    if spread < 1e-9:
        return None, np.eye(3)
    scale = np.sqrt(2.0) / spread
    transform = np.array([[scale, 0.0, -scale * centre[0]], [0.0, scale, -scale * centre[1]], [0.0, 0.0, 1.0]])
    return centred * scale, transform


def _project(homography: np.ndarray, points: np.ndarray) -> np.ndarray | None:
    homogeneous = np.column_stack([points, np.ones(len(points))]) @ homography.T
    w = homogeneous[:, 2]
    if np.any(np.abs(w) < 1e-9):
        return None
    return homogeneous[:, :2] / w[:, None]


@dataclass(frozen=True)
class Progress:
    """What the capture screen shows: one percentage and what is short."""

    percent: float
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


class CoverageTracker:
    """Accumulates accepted captures and reports what the run still needs.

    Counting captures alone is the failure this exists to prevent: forty images
    of a board held square in the middle of the frame satisfy any target count
    and calibrate badly, because nothing in them separates focal length from
    distance or constrains distortion away from the centre.
    """

    def __init__(self, target_captures: int) -> None:
        self._target = max(1, target_captures)
        self._count = 0
        self._zones = np.zeros((ZONE_GRID, ZONE_GRID), dtype=int)
        self._scales = np.zeros(3, dtype=int)
        self._tilted = 0

    def add(self, view: BoardView) -> None:
        self._count += 1
        row, col = view.zone
        self._zones[row, col] += 1
        self._scales[view.scale_band] += 1
        if view.tilt >= TILT_MIN:
            self._tilted += 1

    @property
    def captures(self) -> int:
        return self._count

    def progress(self) -> Progress:
        met, total, missing = 0, 0, []

        for row in range(ZONE_GRID):
            for col in range(ZONE_GRID):
                total += 1
                if self._zones[row, col] >= CAPTURES_PER_ZONE:
                    met += 1
                else:
                    missing.append(ZONE_LABELS[row][col])

        for band in range(3):
            total += 1
            if self._scales[band] >= CAPTURES_PER_SCALE_BAND:
                met += 1
            else:
                missing.append(SCALE_LABELS[band])

        total += 1
        if self._tilted >= CAPTURES_TILTED:
            met += 1
        else:
            missing.append("tilted views")

        total += 1
        if self._count >= self._target:
            met += 1
        else:
            missing.append(f"{self._target - self._count} more captures")

        return Progress(percent=100.0 * met / total, missing=tuple(missing))

    def advice(self) -> str:
        """One sentence naming the most useful next move, or "" when done."""
        progress = self.progress()
        if progress.complete:
            return ""
        return f"Still needed: {', '.join(progress.missing[:3])}"
