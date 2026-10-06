"""Play a clip through the sim's servos and base drive, film it, write an mp4."""

from __future__ import annotations

import subprocess
from pathlib import Path

import _core  # noqa: F401
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw

from brain_client.expressive.basis import Act, Basis
from brain_client.expressive.drive import BaseTracker
from brain_client.expressive.motion import Clip
from render.stage import Stage

FFMPEG = "/opt/homebrew/bin/ffmpeg"
VIDEO_FPS = 25
SPLIT = ("front", "three-quarter")


def _caption(frame: NDArray[np.uint8], lines: list[str]) -> NDArray[np.uint8]:
    image = Image.fromarray(frame)
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((10, 8 + 14 * i), line, fill=(25, 25, 25))
    return np.asarray(image)


def _settle(stage: Stage, q: NDArray[np.float64], seconds: float = 1.0) -> None:
    stage.pose(q)
    stage.command(q)
    stage.sim.step(seconds)
    stage.anchor = stage.sim.pose()


def render_clip(
    clip: Clip,
    out: str | Path,
    env: str = "void",
    camera: str = "three-quarter",
    basis: Basis | None = None,
    size: tuple[int, int] = (640, 480),
    lead_s: float = 0.5,
    tail_s: float = 1.0,
) -> Path:
    """Film ``clip`` played physically at the robot's speed caps (servos + odometry-tracked base);
    ``camera="split"`` films front and three-quarter side by side."""
    basis = basis or Basis.load()
    actuators = basis.limit_frames(clip.resample(VIDEO_FPS).actuator_frames(basis), 1.0 / VIDEO_FPS)
    stage = Stage(env, size)
    _settle(stage, actuators[0])
    tracker = BaseTracker()
    views = SPLIT if camera == "split" else (camera,)
    width = size[0] * len(views)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    encoder = subprocess.Popen(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{size[1]}"]
        + ["-r", str(VIDEO_FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(out)],
        stdin=subprocess.PIPE,
    )
    assert encoder.stdin is not None
    lead, total = round(lead_s * VIDEO_FPS), len(actuators) + round((lead_s + tail_s) * VIDEO_FPS)
    for k in range(total):
        i = min(max(k - lead, 0), len(actuators) - 1)
        q, previous = actuators[i], actuators[max(i - 1, 0)]
        stage.command(q)
        velocity = (q - previous) * VIDEO_FPS
        vx, wz = tracker.twist(
            stage.anchor, stage.sim.pose(), q[Act.BASE_X], q[Act.BASE_YAW], velocity[Act.BASE_X], velocity[Act.BASE_YAW]
        )
        stage.sim.set_cmd_vel(vx, wz)
        stage.sim.step(1.0 / VIDEO_FPS)
        caption = [clip.name, clip.recipe[:110], f"t={max(k - lead, 0) / VIDEO_FPS:4.2f}s / {clip.duration:.2f}s"]
        frame = np.concatenate([stage.shot(view) for view in views], 1)
        encoder.stdin.write(_caption(frame, caption).tobytes())
    encoder.stdin.close()
    if encoder.wait() != 0:
        raise RuntimeError(f"ffmpeg failed writing {out}")
    return out
