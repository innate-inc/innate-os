"""What the judges see: a 2x4 key-frame strip of a clip (posed kinematically) and a caption-free mp4 of it
played physically, so neither carries the prompt or the recipe, from one of ``CAMERAS``."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import _core  # noqa: F401
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont
from render.clip import render_clip
from render.stage import VIEWS, Stage, View

from brain_client.expressive.basis import Basis
from brain_client.expressive.channels import Frames
from brain_client.expressive.motion import Clip

TILE = (440, 360)
COLUMNS, ROWS = 4, 2
FOVY = 26.0  # the stock three-quarter view widened so a full advance or reach stays in frame
LOOK_Z = 0.25
VIDEO_SIZE = (640, 480)
DEFAULT_CAMERA = "three-quarter"


def _orbit(
    target: tuple[float, float, float], distance: float, down_deg: float, off_heading_deg: float, fovy: float
) -> View:
    """A view ``distance`` m from ``target``, looking down ``down_deg``, ``off_heading_deg`` to the robot's right."""
    down, side = math.radians(down_deg), math.radians(off_heading_deg)
    x, y, z = target
    eye = (
        x + distance * math.cos(down) * math.cos(side),
        y - distance * math.cos(down) * math.sin(side),
        z + distance * math.sin(down),
    )
    return View(eye, target, fovy)


# A person crouched to MARS's head height (eye 0.41 m): from there a head tilting down hides its face instead
# of showing its top. The robot spans 46 % (folded) to 75 % (mast) of the frame height, nothing clipped.
VIEWS.setdefault("human", _orbit((0.12, -0.02, 0.2), 1.5, 8.0, 30.0, 22.0))  # render_clip films VIEWS by name
CAMERAS = (DEFAULT_CAMERA, "human")
# One unit per "big move" of each actuator (j1..j6 rad, head deg, base yaw rad, base x m), plus time.
POSE_SCALE = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 0.5, 20.0, 1.0, 0.25])
TIME_WEIGHT = 1.5


def key_frames(actuators: Frames, count: int = COLUMNS * ROWS) -> list[int]:
    """``count`` frame indices in time order: farthest-point sampling over the normalised pose and time from
    the first frame, so a 0.2 s peak (a snap, a recoil) makes the strip where even spacing would skip it."""
    span = np.linspace(0.0, TIME_WEIGHT, len(actuators))[:, None]
    features = np.hstack([actuators / POSE_SCALE, span])
    chosen = [0]
    distance = np.linalg.norm(features - features[0], axis=1)
    while len(chosen) < min(count, len(actuators)):
        pick = int(np.argmax(distance))
        chosen.append(pick)
        distance = np.minimum(distance, np.linalg.norm(features - features[pick], axis=1))
    return sorted(chosen)


class Filmstrip:
    def __init__(self, camera: str = DEFAULT_CAMERA, basis: Basis | None = None) -> None:
        self.camera = camera
        self.basis = basis or Basis.load()
        self.stage = Stage(size=TILE)
        self.font = ImageFont.load_default(size=20)

    def tile(self, q: NDArray[np.float64]) -> NDArray[np.uint8]:
        self.stage.pose(q)
        if self.camera != DEFAULT_CAMERA:
            return self.stage.shot(self.camera)
        camera = self.stage.camera("three-quarter")
        camera.lookat[2] = LOOK_Z
        self.stage.sim.model.vis.global_.fovy = FOVY
        return self.stage.sim.render_rgb(camera)

    def render(self, clip: Clip, png: Path) -> list[float]:
        """Write the strip, returning the times (s) of its frames."""
        actuators = self.basis.limit_frames(clip.actuator_frames(self.basis), 1.0 / clip.fps)
        picks = key_frames(actuators)
        tiles = []
        for n, i in enumerate(picks, 1):
            image = Image.fromarray(self.tile(actuators[i]))
            ImageDraw.Draw(image).text((10, 8), f"{n}   t = {i / clip.fps:.1f} s", fill=(30, 30, 30), font=self.font)
            tiles.append(np.asarray(image))
        tiles += [np.full_like(tiles[0], 255)] * (COLUMNS * ROWS - len(tiles))
        rows = [np.concatenate(tiles[r * COLUMNS : (r + 1) * COLUMNS], 1) for r in range(ROWS)]
        png.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(np.concatenate(rows, 0)).save(png)
        return [i / clip.fps for i in picks]


def render_blind_video(clip: Clip, mp4: Path, camera: str = DEFAULT_CAMERA) -> Path:
    """The clip played physically from ``camera``, with no name or recipe in the caption."""
    blind = dataclasses.replace(clip, name="", recipe="", prompt="", idea="")
    return render_clip(blind, mp4, camera=camera, size=VIDEO_SIZE)
