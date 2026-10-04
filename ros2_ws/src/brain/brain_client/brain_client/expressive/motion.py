# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Clips: a motion on its own time axis, as JSON that plays in the browser, the sim and the robot.

``space="plan"`` clips carry the 8 expressive channels and go through the basis at play time;
``space="actuator"`` clips carry ``[j1..j6, head_deg, base_yaw, base_x]`` and play verbatim.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from brain_client.expressive.basis import ACTUATOR_KEYS, Basis, Projector, Vector
from brain_client.expressive.channels import FPS, MOTION_CHANNELS, MOTION_KEYS, Frames
from brain_client.expressive.dsl import expand
from brain_client.expressive.liveliness import animate
from brain_client.expressive.plan import frames as plan_frames
from brain_client.expressive.plan import to_plan

if TYPE_CHECKING:
    from brain_client.expressive.plan import Plan

Space = Literal["plan", "actuator"]


@dataclass(frozen=True)
class Clip:
    frames: Frames
    fps: float = FPS
    space: Space = "plan"
    name: str = "clip"
    prompt: str = ""
    idea: str = ""
    recipe: str = ""

    @property
    def channels(self) -> tuple[str, ...]:
        return MOTION_KEYS if self.space == "plan" else ACTUATOR_KEYS

    @property
    def duration(self) -> float:
        return (len(self.frames) - 1) / self.fps if len(self.frames) > 1 else 0.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Clip:
        space: Space = "actuator" if data.get("space") == "actuator" else "plan"
        expected = MOTION_KEYS if space == "plan" else ACTUATOR_KEYS
        channels = tuple(data.get("channels", expected))
        if channels != expected:
            raise ValueError(f"{space} clip channels must be {list(expected)}, got {list(channels)}")
        frames = np.asarray(data["frames"], dtype=np.float64).reshape(-1, len(expected))
        if len(frames) == 0 or not np.all(np.isfinite(frames)):
            raise ValueError("clip frames must be a non-empty grid of finite numbers")
        return cls(
            frames=frames,
            fps=float(data.get("fps", FPS)),
            space=space,
            name=str(data.get("name", "clip")),
            prompt=str(data.get("prompt", "")),
            idea=str(data.get("idea", "")),
            recipe=str(data.get("recipe", "")),
        )

    @classmethod
    def load(cls, source: str | Path | Mapping[str, Any]) -> Clip:
        """From a dict, a JSON string, or a path to a JSON file."""
        if isinstance(source, Mapping):
            return cls.from_dict(source)
        if isinstance(source, str) and source.lstrip().startswith("{"):
            return cls.from_dict(json.loads(source))
        return cls.from_dict(json.loads(Path(source).read_text()))

    def to_dict(self, decimals: int = 4) -> dict[str, Any]:
        return {
            "name": self.name,
            "prompt": self.prompt,
            "idea": self.idea,
            "recipe": self.recipe,
            "fps": self.fps,
            "space": self.space,
            "channels": list(self.channels),
            "frames": np.round(self.frames, decimals).tolist(),
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()))
        return path

    @classmethod
    def from_plan_frames(
        cls, frames: Frames, name: str = "clip", prompt: str = "", idea: str = "", recipe: str = ""
    ) -> Clip:
        """(T, 8 or 9) plan-space frames at FPS (energy, if present, is dropped)."""
        motion = np.asarray(frames, dtype=np.float64)[:, :MOTION_CHANNELS].copy()
        return cls(frames=motion, name=name, prompt=prompt, idea=idea, recipe=recipe)

    @classmethod
    def from_recipe(cls, recipe: str, name: str = "clip", seed: int = 0, prompt: str = "", idea: str = "") -> Clip:
        """Recipe -> plan -> procedural liveliness -> plan-space clip (raises ``RecipeError``)."""
        plan: Plan = to_plan(expand(recipe))
        motion = animate(plan_frames(plan), seed)
        return cls.from_plan_frames(motion, name=name, prompt=prompt, idea=idea, recipe=recipe)

    def resample(self, fps: float) -> Clip:
        """This clip on an ``fps`` grid; box-filtered first when downsampling so detail can't alias."""
        if len(self.frames) < 2 or math.isclose(self.fps, fps, rel_tol=1e-6):
            return self
        source = self.frames
        ratio = self.fps / fps
        if ratio > 1.25:
            half = max(1, round(ratio / 2))
            kernel = np.ones(2 * half + 1) / (2 * half + 1)
            padded = np.pad(source, ((half, half), (0, 0)), mode="edge")
            source = np.stack([np.convolve(padded[:, j], kernel, mode="valid") for j in range(source.shape[1])], 1)
        times = np.arange(len(self.frames)) / self.fps
        grid = np.arange(math.floor(self.duration * fps + 0.5) + 1) / fps
        resampled = np.stack([np.interp(grid, times, source[:, j]) for j in range(source.shape[1])], 1)
        return replace(self, frames=resampled, fps=fps)

    def sample(self, t: float) -> Vector:
        """Row at ``t`` s, linearly interpolated, held before 0 and after the end."""
        position = min(max(t * self.fps, 0.0), len(self.frames) - 1.0)
        i = min(int(position), len(self.frames) - 2) if len(self.frames) > 1 else 0
        a = position - i
        if len(self.frames) == 1 or a <= 0.0:
            return self.frames[i].copy()
        return (1.0 - a) * self.frames[i] + a * self.frames[i + 1]

    def actuator_frames(self, basis: Basis, project: Projector | None = None) -> Frames:
        """(T, 9) actuator vectors: plan rows through the basis, actuator rows as they are."""
        if self.space == "actuator":
            return self.frames
        return basis.synthesize_frames(self.frames, project)

    def to_actuators(self, basis: Basis, project: Projector | None = None) -> Clip:
        if self.space == "actuator":
            return self
        return replace(self, frames=self.actuator_frames(basis, project), space="actuator")
