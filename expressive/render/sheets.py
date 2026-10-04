"""Still sheets: each basis channel at -1 / 0 / +1, and a clip's frames over time."""

from __future__ import annotations

from pathlib import Path

import _core  # noqa: F401
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw

from brain_client.expressive.basis import Basis
from brain_client.expressive.channels import CHANNELS, NEUTRAL, Ch
from brain_client.expressive.motion import Clip
from render.stage import Stage

AXIS_CHANNELS = (Ch.APPROACH, Ch.EXPAND, Ch.RISE, Ch.ATTEND, Ch.ASKEW, Ch.ORIENT, Ch.ADVANCE, Ch.GRIP)
EXTREMES = {Ch.ORIENT: 45.0, Ch.ADVANCE: 0.2, Ch.GRIP: 1.0}


def _label(image: NDArray[np.uint8], text: str) -> NDArray[np.uint8]:
    tile = Image.fromarray(image)
    ImageDraw.Draw(tile).text((8, 6), text, fill=(20, 20, 20))
    return np.asarray(tile)


def _grid(rows: list[list[NDArray[np.uint8]]]) -> Image.Image:
    return Image.fromarray(np.concatenate([np.concatenate(r, 1) for r in rows], 0))


def axis_sheet(basis: Basis, png: Path, view: str = "front", stage: Stage | None = None) -> Path:
    """One row per channel: its -1, NEUTRAL and +1 poses, from ``view``."""
    stage = stage or Stage(size=(400, 400))
    rows: list[list[NDArray[np.uint8]]] = []
    for channel in AXIS_CHANNELS:
        extreme = EXTREMES.get(channel, 1.0)
        tiles = []
        for sign in (-1.0, 0.0, 1.0):
            row = NEUTRAL.copy()
            if channel == Ch.GRIP:
                row[Ch.GRIP] = (0.0, NEUTRAL[Ch.GRIP], extreme)[int(sign) + 1]
            else:
                row[channel] = sign * extreme
            stage.pose(basis.synthesize(row).vector)
            label = f"{CHANNELS[channel].key} {row[channel]:+g}  [{view}]"
            if view == "main":
                label += f"  arm covers {stage.arm_in_view():.1%} of the image"
            tiles.append(_label(stage.shot(view), label))
        rows.append(tiles)
    png.parent.mkdir(parents=True, exist_ok=True)
    _grid(rows).save(png)
    return png


def contact_sheet(
    clip: Clip,
    png: Path,
    basis: Basis | None = None,
    columns: int = 6,
    rows: int = 2,
    view: str = "three-quarter",
    stage: Stage | None = None,
) -> Path:
    """``columns x rows`` evenly spaced kinematic stills of a clip, timestamped."""
    basis = basis or Basis.load()
    stage = stage or Stage(size=(320, 320))
    actuators = basis.limit_frames(clip.actuator_frames(basis), 1.0 / clip.fps)
    count = columns * rows
    picks = np.linspace(0, len(actuators) - 1, count).round().astype(int)
    tiles = []
    for i in picks:
        stage.pose(actuators[i])
        tiles.append(_label(stage.shot(view), f"{clip.name} t={i / clip.fps:.2f}s"))
    png.parent.mkdir(parents=True, exist_ok=True)
    _grid([tiles[r * columns : (r + 1) * columns] for r in range(rows)]).save(png)
    return png
