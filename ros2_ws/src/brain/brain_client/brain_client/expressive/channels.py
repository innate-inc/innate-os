# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The expressive channels (plan space): body-language axes the planner reasons in, in a fixed order.

A motion is a (T, 8) array of channels ``approach .. grip`` at ``FPS``; a plan adds ``energy`` as a
ninth column. The order and the DSL letters are frozen: the distilled planner is trained on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np
from numpy.typing import NDArray

FPS = 25
Frames = NDArray[np.float64]


class Ch(IntEnum):
    APPROACH = 0
    EXPAND = 1
    RISE = 2
    ATTEND = 3
    ASKEW = 4
    ORIENT = 5
    ADVANCE = 6
    GRIP = 7
    ENERGY = 8


@dataclass(frozen=True)
class Channel:
    key: str
    dsl: str
    lo: float
    hi: float
    neutral: float
    unit: str


CHANNELS: tuple[Channel, ...] = (
    Channel("approach", "a", -1.0, 1.0, 0.0, "unit"),
    Channel("expand", "x", -1.0, 1.0, 0.0, "unit"),
    Channel("rise", "z", -1.0, 1.0, 0.0, "unit"),
    Channel("attend", "p", -1.0, 1.0, 0.0, "unit"),
    Channel("askew", "k", -1.0, 1.0, 0.0, "unit"),
    Channel("orient", "b", -60.0, 60.0, 0.0, "deg"),
    Channel("advance", "d", -0.25, 0.25, 0.0, "m"),
    Channel("grip", "g", 0.0, 1.0, 0.15, "unit"),
    Channel("energy", "E", 0.0, 12.0, 0.5, "unit"),
)
MOTION_CHANNELS = 8
PLAN_KEYS: tuple[str, ...] = tuple(c.key for c in CHANNELS)
MOTION_KEYS: tuple[str, ...] = PLAN_KEYS[:MOTION_CHANNELS]
DSL_INDEX: dict[str, int] = {c.dsl: i for i, c in enumerate(CHANNELS)}

NEUTRAL: Frames = np.array([c.neutral for c in CHANNELS])
LOW: Frames = np.array([c.lo for c in CHANNELS])
HIGH: Frames = np.array([c.hi for c in CHANNELS])


def clip_to_limits(frames: Frames) -> Frames:
    """Clamp (T, 8) motion or (T, 9) plan frames to the channel ranges."""
    width = frames.shape[-1]
    return np.clip(frames, LOW[:width], HIGH[:width])
