"""Six presets side by side, each robot on its own stage and Animator: the gesture, idle, then again."""

from __future__ import annotations

import logging
from pathlib import Path

import _core  # noqa: F401
import numpy as np

from brain_client.expressive import presets
from brain_client.expressive.animator import Animator
from demo.film import Encoder, Player, caption

logger = logging.getLogger("mars-express.montage")
NAMES = ("surprised", "proud", "excited", "curious", "sad", "sleepy")  # the six that read best blind (eval, 10d0232cc)
GRID = (3, 2)
TILE = (426, 360)
FPS = 30
REST_S = 1.0


def render_montage(out: Path, names: tuple[str, ...] = NAMES, seconds: float = 15.0) -> Path:
    columns, rows = GRID
    players = [Player(TILE, distance=1.0, look_z=0.25, fovy=26.0, shift=-0.1) for _ in names]
    animators = [Animator(fps=FPS) for _ in names]
    clips = [presets.clip(name, seed=k) for k, name in enumerate(names)]
    starts = [0.5 + 0.15 * k for k in range(len(names))]
    for player, animator in zip(players, animators, strict=True):
        player.settle(animator.tick(0.0).vector)
    size = (columns * TILE[0], rows * TILE[1])
    with Encoder(out, size, FPS) as encoder:
        for k in range(round(seconds * FPS)):
            t = k / FPS
            tiles = []
            for i, (player, animator, clip) in enumerate(zip(players, animators, clips, strict=True)):
                if t >= starts[i]:
                    animator.play(clip)
                    starts[i] += clip.duration + REST_S
                if k:
                    player.step(animator.tick(t).vector, 1.0 / FPS)
                tiles.append(caption(player.shot(), corner=names[i], sizes=(24, 26)))
            grid = [np.concatenate(tiles[r * columns : (r + 1) * columns], 1) for r in range(rows)]
            encoder.write(np.concatenate(grid, 0))
    logger.info("montage -> %s", out)
    return out
