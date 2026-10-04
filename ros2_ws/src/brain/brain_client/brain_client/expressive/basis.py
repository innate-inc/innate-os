# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The semantic basis: plan-space channels -> what MARS's actuators hold.

``basis.json`` is data, tuned by rendering: the NEUTRAL actuator pose plus, for each body channel
(approach, expand, rise, attend, askew), the actuator deltas of its -1 and +1 extremes. A plan row
first has its arm channels (approach, expand, rise, askew) scaled toward NEUTRAL by the precomputed
``safe`` table, so combinations that would fold the arm into itself or the floor stop short (the
robot has no collision model; the table is built on the host with ``reach`` against mars.urdf).
It then becomes NEUTRAL + u * (READY - NEUTRAL) + sum |w| * endpoint(sign w): NEUTRAL is the folded
arm, and the channels whose positive end unfolds it (``unfold``: approach, expand, rise) store that
end relative to READY, a raised front pose, with u = 1 - prod(1 - w+) over them. One channel alone
is plain linear interpolation to its endpoint; several share one unfolding instead of each adding
its own (tall + reaching is the mast leaning in, not two unfoldings summed into a knot). The
gripper is the grip channel plus the endpoints' j6 deltas, orient/advance are base offsets from
the anchor, and everything is clamped to the joint limits and the shoulder-clearance rule.

The safe table is multilinear over a grid of the five body channels (attend included: the head can
meet the arm), sampled at ``safe.grid`` per channel, C order, channel order approach..askew.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from brain_client.expressive.channels import Ch, Frames

BASIS_PATH = Path(__file__).with_name("basis.json")
ACTUATOR_KEYS: tuple[str, ...] = ("j1", "j2", "j3", "j4", "j5", "j6", "head_deg", "base_yaw", "base_x")
BODY_CHANNELS: tuple[Ch, ...] = (Ch.APPROACH, Ch.EXPAND, Ch.RISE, Ch.ATTEND, Ch.ASKEW)
ARM_CHANNELS: tuple[Ch, ...] = (Ch.APPROACH, Ch.EXPAND, Ch.RISE, Ch.ASKEW)
Vector = NDArray[np.float64]


class Act(IntEnum):
    J1 = 0
    J2 = 1
    J3 = 2
    J4 = 3
    J5 = 4
    J6 = 5
    HEAD_DEG = 6
    BASE_YAW = 7
    BASE_X = 8


@dataclass(frozen=True)
class ActuatorPose:
    """One command for the whole body: ``vector`` is [j1..j6 rad, head_deg, base_yaw rad, base_x m].

    Base values are offsets from the anchor captured when expression started; treat ``vector`` as
    read-only.
    """

    vector: Vector

    @property
    def arm(self) -> list[float]:
        return [float(v) for v in self.vector[: Act.HEAD_DEG]]

    @property
    def head_deg(self) -> float:
        return float(self.vector[Act.HEAD_DEG])

    @property
    def base_yaw(self) -> float:
        return float(self.vector[Act.BASE_YAW])

    @property
    def base_x(self) -> float:
        return float(self.vector[Act.BASE_X])

    @property
    def grip(self) -> float:
        return float(self.vector[Act.J6]) / GRIP_OPEN_RAD

    def as_dict(self) -> dict[str, float]:
        return {**dict(zip(ACTUATOR_KEYS, self.vector.tolist(), strict=True)), "grip": self.grip}


GRIP_OPEN_RAD = 0.8727
Projector = Callable[[Vector], Vector]


def clearance_floor(j1: float, full_min: float, guard_min: float) -> float:
    """j2's floor for a given j1: across the front arc the arm must duck under the head.

    The same piecewise ramp as arm_control.cpp and the sim (mars_sim_driver.core.joint2_min_target).
    """
    if j1 < -1.35 or j1 >= 1.25:
        return full_min
    if j1 < -1.0:
        t = -(j1 + 1.0) / 0.35
    elif j1 < 1.0:
        t = 0.0
    else:
        t = (j1 - 1.0) / 0.25
    return guard_min + t * (full_min - guard_min)


class Basis:
    def __init__(self, data: Mapping[str, Any]) -> None:
        self.version = int(data["version"])
        self.data = data
        self.neutral = np.array([float(data["neutral"].get(k, 0.0)) for k in ACTUATOR_KEYS])
        ready = data.get("ready", data["neutral"])
        self.unfolding = (np.array([float(ready.get(k, 0.0)) for k in ACTUATOR_KEYS]) - self.neutral) * np.isin(
            ACTUATOR_KEYS, list(ready)
        )
        unfold = set(data.get("unfold", ()))
        self.unfolds = np.array([channel.name.lower() in unfold for channel in BODY_CHANNELS])
        self.low = np.array([float(data["limits"][k][0]) for k in ACTUATOR_KEYS])
        self.high = np.array([float(data["limits"][k][1]) for k in ACTUATOR_KEYS])
        self.guard_min = float(data["clearance"]["j2_min"])
        speeds = data.get("max_speed", {})
        self.max_speed = np.array([float(speeds.get(k, math.inf)) for k in ACTUATOR_KEYS])
        self.grip_rad = float(data["grip_rad"])
        self.endpoints = np.zeros((len(BODY_CHANNELS), 2, len(ACTUATOR_KEYS)))
        for i, channel in enumerate(BODY_CHANNELS):
            spec = data["channels"][channel.name.lower()]
            for side, sign in enumerate(("neg", "pos")):
                for key, value in spec.get(sign, {}).items():
                    self.endpoints[i, side, ACTUATOR_KEYS.index(key)] = float(value)
        safe = data.get("safe")
        self.safe_grid = np.array(safe["grid"], dtype=np.float64) if safe else None
        self.safe_scale = (
            np.array(safe["scale"], dtype=np.float64).reshape((len(safe["grid"]),) * len(BODY_CHANNELS))
            if safe
            else None
        )

    @classmethod
    def load(cls, path: Path = BASIS_PATH) -> Basis:
        return cls(json.loads(path.read_text()))

    def safe_factor(self, row: Frames) -> float:
        """How far toward ``row``'s arm channels the body can go before folding into itself (0..1)."""
        if self.safe_grid is None or self.safe_scale is None:
            return 1.0
        grid = self.safe_grid
        weights = np.clip(np.asarray(row, dtype=np.float64)[: len(BODY_CHANNELS)], grid[0], grid[-1])
        cells = np.minimum(np.searchsorted(grid, weights, side="right") - 1, len(grid) - 2)
        fractions = (weights - grid[cells]) / (grid[cells + 1] - grid[cells])
        factor = 0.0
        for corner in range(1 << len(BODY_CHANNELS)):
            bits = [(corner >> d) & 1 for d in range(len(BODY_CHANNELS))]
            weight = math.prod(f if b else 1.0 - f for f, b in zip(fractions.tolist(), bits, strict=True))
            if weight > 0.0:
                factor += weight * float(self.safe_scale[tuple(int(c) + b for c, b in zip(cells, bits, strict=True))])
        return factor

    def safe_row(self, row: Frames) -> Frames:
        """``row`` with its arm channels scaled by ``safe_factor``."""
        row = np.array(row, dtype=np.float64)
        factor = self.safe_factor(row)
        if factor < 1.0:
            row[list(ARM_CHANNELS)] *= factor
        return row

    def offset(self, row: Frames) -> Vector:
        """Actuator delta of the body channels in ``row`` from NEUTRAL (no grip/base, no clamping)."""
        weights = np.clip(np.asarray(row, dtype=np.float64)[: len(BODY_CHANNELS)], -1.0, 1.0)
        sides = (weights > 0).astype(int)
        unfold = 1.0 - float(np.prod(1.0 - np.maximum(weights, 0.0)[self.unfolds]))
        return unfold * self.unfolding + np.abs(weights) @ self.endpoints[np.arange(len(BODY_CHANNELS)), sides]

    def raw(self, row: Frames) -> Vector:
        """Plan row (>= 8 channels) -> unclamped actuator vector."""
        row = np.asarray(row, dtype=np.float64)
        delta = self.offset(row)
        q = self.neutral + delta
        q[Act.J6] = self.grip_rad * row[Ch.GRIP] + delta[Act.J6]
        q[Act.BASE_YAW] = math.radians(row[Ch.ORIENT])
        q[Act.BASE_X] = row[Ch.ADVANCE]
        return q

    def clamp(self, q: Vector) -> Vector:
        """Joint limits, then the shoulder-clearance rule."""
        q = np.clip(q, self.low, self.high)
        q[Act.J2] = max(q[Act.J2], clearance_floor(q[Act.J1], self.low[Act.J2], self.guard_min))
        return q

    def _floor(self, j1: float) -> float:
        return clearance_floor(j1, self.low[Act.J2], self.guard_min)

    def limit(self, previous: Vector, target: Vector, dt: float) -> Vector:
        """Step from ``previous`` toward ``target`` within ``max_speed``, keeping the shoulder clearance.

        j1 may only sweep into the front arc as fast as j2 can rise over the clearance floor, and the
        rate limit is re-applied after the clamp, so no step ever exceeds ``max_speed * dt`` (a
        ``previous`` that already violates the floor climbs out at j2's top speed).
        """
        step = self.max_speed * dt
        q = np.clip(previous + np.clip(target - previous, -step, step), self.low, self.high)
        bound = max(previous[Act.J2] + step[Act.J2], self._floor(float(previous[Act.J1])))
        if self._floor(float(q[Act.J1])) > bound:
            # Within one step j1 cannot cross the front arc's plateau, so the floor is monotonic here.
            lo, hi = float(previous[Act.J1]), float(q[Act.J1])
            for _ in range(20):
                mid = 0.5 * (lo + hi)
                lo, hi = (mid, hi) if self._floor(mid) <= bound else (lo, mid)
            q[Act.J1] = lo
        return np.clip(self.clamp(q), previous - step, previous + step)

    def limit_frames(self, frames: Frames, dt: float) -> Frames:
        """(T, 9) actuator frames as the robot can follow them: ``limit`` applied frame to frame."""
        out = np.array(frames, dtype=np.float64)
        for i in range(1, len(out)):
            out[i] = self.limit(out[i - 1], out[i], dt)
        return out

    def synthesize(self, row: Frames, project: Projector | None = None) -> ActuatorPose:
        q = self.clamp(self.raw(self.safe_row(row)))
        return ActuatorPose(project(q) if project is not None else q)

    def synthesize_frames(self, frames: Frames, project: Projector | None = None) -> Frames:
        """(T, >=8) plan-space frames -> (T, 9) actuator vectors."""
        return np.stack([self.synthesize(row, project).vector for row in frames]) if len(frames) else np.zeros((0, 9))
