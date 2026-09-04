#!/usr/bin/env python3
"""Compile the MuJoCo world at build time so a session loads a .mjb in ~50ms
instead of compiling 1300 hulls for minutes. The cache key hashes asset paths,
mtimes and the render scale, so this must import from the sourced install tree,
build at the scale the world server will use, and run after every COPY."""

import os
import sys
import time
from pathlib import Path

from mars_sim_driver import core, world
from mars_sim_driver.constants import CAMERA_HEIGHT, CAMERA_WIDTH
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.world_server import DEPTH_WH

INSTALL_ROOT = Path("/root/innate-os/ros2_ws/install")
SCALE_MARKER = "render_scale"


def main() -> int:
    for module in (core, world):
        path = Path(module.__file__ or "")
        if not path.is_relative_to(INSTALL_ROOT):
            print(
                f"ERROR: {module.__name__} imported from {path}, not the install tree.\n"
                "The baked cache would key differently from the running world server.",
                file=sys.stderr,
            )
            return 1

    scale = int(os.environ.get("INNATE_SIM_RENDER_SCALE", "1"))
    assets = Path(os.environ["VIRTUAL_MARS_ASSETS"])

    def build() -> float:
        # Exactly what world_server.main() builds, so the cache key matches.
        start = time.monotonic()
        VirtualMars(
            render_wh=(CAMERA_WIDTH // scale, CAMERA_HEIGHT // scale),
            depth_render_wh=DEPTH_WH,
        )
        return time.monotonic() - start

    compiled = build()
    print(f"world at render scale {scale}: first build {compiled:.1f}s")

    cache_dir = assets / ".model_cache"
    cached = sorted(cache_dir.glob("world-*.mjb"))
    if len(cached) != 1:
        names = [p.name for p in cached] or ["<none>"]
        print(
            f"ERROR: expected exactly one .mjb, found {len(cached)}: {names}.\n"
            "Cannot tell which one the runtime would resolve to.",
            file=sys.stderr,
        )
        return 1
    print(f"model cache: {cached[0]} ({cached[0].stat().st_size / 1e6:.0f} MB)")

    # A second build is the only proof: the binary is rewritten only on the
    # compile path, so an untouched file is a hit. Elapsed time cannot tell.
    baked = cached[0].stat().st_mtime_ns
    reload_s = build()
    print(f"cache reload: {reload_s:.2f}s")
    after = sorted(cache_dir.glob("world-*.mjb"))
    if [p.name for p in after] != [cached[0].name] or after[0].stat().st_mtime_ns != baked:
        print(
            "ERROR: the second build rewrote the cache, so the runtime misses it and every session would recompile.",
            file=sys.stderr,
        )
        return 1

    # entrypoint.sh refuses a scale the cache was not baked for.
    (cache_dir / SCALE_MARKER).write_text(f"{scale}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
