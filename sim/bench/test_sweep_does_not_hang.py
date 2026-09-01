"""The sweep must not hang when a worker dies.

multiprocessing.Pool does not notice a worker that dies abnormally, so
imap_unordered blocks forever on a result that is never coming -- observed
twice, at 86/87 and 176/177, with every worker at zero CPU. For a benchmark
whose headline claim is "one command", hanging with no output and no exit code
is the worst available failure, so this drives the real entry point and checks
it comes back.

--result-timeout 0.001 stands in for the dead worker: no result can arrive that
fast, which is the same situation as a result that never arrives at all.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent
# The sim venv, not the repo root one: the system python has no mujoco.
VENV = BENCH.parent / ".venv/bin/python"
pytestmark = pytest.mark.skipif(not VENV.exists(), reason="needs sim/.venv (mujoco)")


def run(*extra, timeout=240):
    env = {**os.environ, "MUJOCO_GL": "osmesa"}
    return subprocess.run(
        [str(VENV), str(BENCH / "main.py"), "--agents", "oracle", "--challenges", "gallery_ring_tour", *extra],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(BENCH.parents[1]),
    )


def test_a_dead_worker_ends_the_sweep_instead_of_hanging(tmp_path):
    out = tmp_path / "r.json"
    r = run("--result-timeout", "0.001", "--out", str(out))
    assert "a worker died" in r.stdout, r.stdout[-2000:]
    assert "1 episode(s) never ran" in r.stdout, r.stdout[-2000:]
    # The lost episode is recorded, and recorded as OUR fault.
    eps = json.loads(out.read_text())
    assert len(eps) == 1 and eps[0]["blocked"].startswith("harness:"), eps
    assert not eps[0]["passed"]


def test_the_gate_calls_it_incomplete_not_a_failure(tmp_path):
    r = run("--result-timeout", "0.001", "--out", str(tmp_path / "r.json"))
    assert "INCOMPLETE=1" in r.stdout, r.stdout[-800:]
    assert "INVALID=1" not in r.stdout, "a harness fault was scored against the challenge"


def test_the_wait_is_generous_before_anything_has_finished():
    """With no result yet there is nothing to scale from, and a model-backed
    first episode can outlast any floor worth having. Erring long costs a slow
    recovery; erring short aborts a working sweep."""
    from main import result_budget

    assert result_budget({}, {"oracle"}) >= 1800.0


def test_a_fast_agent_does_not_shorten_the_wait_for_a_slow_one():
    """The case this got wrong: an oracle returns in five seconds while a
    model-backed episode of the same challenge is still running, and scaling
    off the oracle would declare the working sweep dead."""
    from main import result_budget

    assert result_budget({"oracle": [5.1]}, {"oracle", "brain:codex"}) >= 1800.0, (
        "a fast agent's timing was used to bound an agent nobody has measured"
    )


def test_the_wait_shrinks_once_every_agent_has_been_measured():
    from main import result_budget

    assert result_budget({"oracle": [10.0, 45.0]}, {"oracle"}) == 300.0
    assert result_budget({"brain:x": [600.0]}, {"brain:x"}) == 3600.0
    # The slowest agent sets it, not the average.
    assert result_budget({"oracle": [10.0], "brain:x": [600.0]}, {"oracle", "brain:x"}) == 3600.0


def test_an_explicit_timeout_wins():
    from main import result_budget

    assert result_budget({"brain:x": [600.0]}, {"brain:x"}, override=30.0) == 30.0
    assert result_budget({}, {"oracle"}, override=0.001) == 0.001
