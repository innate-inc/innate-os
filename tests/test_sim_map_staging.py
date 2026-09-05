"""Exercise the launcher's map-copy commands without starting ROS or tmux."""

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


def test_startup_stages_crossroads_without_importing_retired_local_maps(tmp_path):
    shell = shutil.which("zsh")
    if shell is None:
        pytest.skip("the simulator launch script requires zsh")
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/launch_sim_in_tmux.zsh").read_text()
    start = script.index("mkdir -p ~/innate-os/data/maps")
    end = script.index('tmux new-window -t "$SESSION_NAME" -n nav-brain', start)
    commands = script[start:end].replace("~/innate-os", shlex.quote(str(tmp_path)))
    published = tmp_path / "sim/assets/map"
    maps = tmp_path / "data/maps"
    published.mkdir(parents=True)
    maps.mkdir(parents=True)
    for suffix in ("yaml", "pgm"):
        (published / f"intersection.{suffix}").write_text(f"published {suffix}")
        (maps / f"town.{suffix}").write_text(f"user {suffix}")
        for pack in ("old-town", "other-pack"):
            local = tmp_path / "sim/assets/local-environments" / pack / "map"
            local.mkdir(parents=True, exist_ok=True)
            (local / f"town.{suffix}").write_text(f"conflicting {pack}")
            (local / f"local-only.{suffix}").write_text("not a published map")

    subprocess.run([shell, "-c", commands], check=True, capture_output=True)

    for suffix in ("yaml", "pgm"):
        assert (maps / f"town.{suffix}").read_text() == f"user {suffix}"
        assert (maps / f"intersection.{suffix}").read_text() == f"published {suffix}"
        assert not (maps / f"local-only.{suffix}").exists()
