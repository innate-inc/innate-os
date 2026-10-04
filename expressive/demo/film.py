"""Filming helpers for the demo videos: a stage driven physically by actuator poses, a framed camera,
captions, and an ffmpeg encoder that can mux an audio track."""

from __future__ import annotations

import math
import subprocess
import textwrap
from pathlib import Path
from types import TracebackType

import _core  # noqa: F401
import mujoco
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw, ImageFont
from render.clip import FFMPEG
from render.stage import Stage

from brain_client.expressive.basis import Act
from brain_client.expressive.drive import BaseTracker

INK = (20, 20, 20)
MUTED = (95, 95, 92)


class Player:
    """One robot on a stage, commanded like the real one: arm and head through the servos, the base by
    the odometry tracker toward the pose's orient/advance offsets."""

    def __init__(
        self,
        size: tuple[int, int],
        distance: float = 0.95,
        look_z: float = 0.23,
        fovy: float = 22.0,
        shift: float = -0.12,
    ) -> None:
        self.stage = Stage(size=size)
        self.tracker = BaseTracker()
        self.distance, self.look_z, self.fovy, self.shift = distance, look_z, fovy, shift
        self._last: NDArray[np.float64] | None = None

    def settle(self, q: NDArray[np.float64], seconds: float = 1.0) -> None:
        self.stage.pose(q)
        self.stage.command(q)
        self.stage.sim.step(seconds)
        self.stage.anchor = self.stage.sim.pose()
        self._last = q.copy()

    def step(self, q: NDArray[np.float64], dt: float) -> None:
        previous = self._last if self._last is not None else q
        self.stage.command(q)
        velocity = (q - previous) / dt
        vx, wz = self.tracker.twist(
            self.stage.anchor,
            self.stage.sim.pose(),
            q[Act.BASE_X],
            q[Act.BASE_YAW],
            velocity[Act.BASE_X],
            velocity[Act.BASE_YAW],
        )
        self.stage.sim.set_cmd_vel(vx, wz)
        self.stage.sim.step(dt)
        self._last = q.copy()

    def camera(self) -> mujoco.MjvCamera:
        """The three-quarter view, closer and re-centred on the robot (shifted along the image's x axis)."""
        camera = self.stage.camera("three-quarter")
        camera.distance *= self.distance
        camera.lookat[2] = self.look_z
        yaw = math.radians(camera.azimuth)
        camera.lookat[0] += self.shift * math.sin(yaw)
        camera.lookat[1] -= self.shift * math.cos(yaw)
        self.stage.sim.model.vis.global_.fovy = self.fovy
        return camera

    def shot(self) -> NDArray[np.uint8]:
        return self.stage.sim.render_rgb(self.camera())


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def caption(
    frame: NDArray[np.uint8],
    subtitle: str = "",
    corner: str = "",
    title: str = "",
    sizes: tuple[int, int] = (34, 22),
) -> NDArray[np.uint8]:
    """Subtitle centred at the bottom (wrapped, on a soft band), a small note top-left, a title top-right."""
    image = Image.fromarray(frame)
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    big, small = font(sizes[0]), font(sizes[1])
    if subtitle:
        lines = textwrap.wrap(subtitle, width=58)
        line_h = sizes[0] + 10
        top = height - 28 - line_h * len(lines)
        draw.rectangle((0, top - 14, width, height), fill=(252, 252, 251, 215))
        for k, line in enumerate(lines):
            w = draw.textlength(line, font=big)
            draw.text(((width - w) / 2, top + k * line_h), line, fill=INK, font=big)
    if corner:
        draw.text((24, 20), corner, fill=MUTED, font=small)
    if title:
        w = draw.textlength(title, font=small)
        draw.text((width - w - 24, 20), title, fill=MUTED, font=small)
    return np.asarray(image.convert("RGB"))


class Encoder:
    """Raw RGB frames in, H.264 mp4 out, with an optional audio track muxed in."""

    def __init__(self, out: Path, size: tuple[int, int], fps: float, audio: Path | None = None) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        command = [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24"]
        command += ["-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-"]
        if audio is not None:
            command += ["-i", str(audio), "-c:a", "aac", "-b:a", "160k", "-shortest"]
        command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19", "-movflags", "+faststart", str(out)]
        self.out = out
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE)

    def write(self, frame: NDArray[np.uint8]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(np.ascontiguousarray(frame).tobytes())

    def __enter__(self) -> Encoder:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        assert self._process.stdin is not None
        self._process.stdin.close()
        if self._process.wait() != 0 and kind is None:
            raise RuntimeError(f"ffmpeg failed writing {self.out}")
