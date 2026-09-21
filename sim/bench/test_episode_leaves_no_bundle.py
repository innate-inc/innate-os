"""An episode must not leave the process pointed at its own map.

`_prepare` selects a map's bundle two ways at once: it sets
VIRTUAL_MARS_ASSETS, and it rebinds core.ASSETS_DIR to whatever that resolved
to. Neither was ever put back. A sweep never noticed, because every episode
sets them again and each worker is its own process, but anything that builds a
world after an episode in the SAME process inherits the last map's furniture.

That is not hypothetical. It is what made test_environment_refresh fail only
in a full run: after an episode on `counter`, a `rounds` world reported seven
populated props instead of one, six of them counter's cups and jars. The test
passed alone, which is the shape of a bug that survives review and fails CI.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import os

from backends import EchoBackend
from brain_agent import BrainAgent
from mars_sim_driver import core as _core
from runner import run_episode


def test_an_episode_puts_the_bundle_binding_back():
    before_env = os.environ.get("VIRTUAL_MARS_ASSETS")
    before_dir = _core.ASSETS_DIR

    ep = run_episode(
        "counter",
        "counter_read_the_pass",
        lambda ch: BrainAgent(EchoBackend([{"action": "finish", "args": "{}"}]), max_turns=1),
        max_sim_s=5.0,
        agent_name="brain:echo",
        render_wh=(64, 48),
    )
    assert ep.started, ep.blocked

    assert os.environ.get("VIRTUAL_MARS_ASSETS") == before_env, (
        "the episode left VIRTUAL_MARS_ASSETS pointing at its own bundle"
    )
    assert _core.ASSETS_DIR == before_dir, "the episode left core.ASSETS_DIR pointing at its own bundle"


def test_a_world_built_after_an_episode_is_not_furnished_from_it():
    """The symptom, not just the mechanism: the next world is its own."""
    from pathlib import Path

    from mars_sim_driver.core import VirtualMars
    from mars_sim_driver.environments import Environment

    assets = Path(__file__).resolve().parents[1] / "assets"

    def populated(pack):
        mars = VirtualMars(render_wh=(64, 48), depth_render_wh=(64, 48), environment=Environment.load(pack, assets))
        try:
            return {p.name for p in mars.props.props.values() if p.initial_pose}
        finally:
            mars.close()

    clean = populated("rounds")
    run_episode(
        "counter",
        "counter_read_the_pass",
        lambda ch: BrainAgent(EchoBackend([{"action": "finish", "args": "{}"}]), max_turns=1),
        max_sim_s=5.0,
        agent_name="brain:echo",
        render_wh=(64, 48),
    )
    after = populated("rounds")
    assert after == clean, f"the counter episode furnished rounds with {sorted(after - clean)}"
