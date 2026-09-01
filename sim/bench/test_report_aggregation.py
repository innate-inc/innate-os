"""A challenge must be scored from one sweep, not from a mixture of several.

Per-map results files accumulate. Re-running one map leaves older files behind
that still describe the same challenges, and appending them all let an old
passing oracle and a newer failing one both reach the gate, which then read
whichever row it met first -- declaring a challenge VALID from a measurement
that no longer holds, and scoring the agent against the mixture.

The rule is "the newest file that contains a challenge owns it", and what that
displaces has to be said rather than silently outvoted.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

import report


def ep(challenge, agent, passed, **kw):
    return {
        "map": "gallery",
        "challenge": challenge,
        "agent": agent,
        "passed": passed,
        "goals_done": 2 if passed else 0,
        "goals_total": 2,
        "elapsed_s": 10.0,
        "reason": "",
        "wall_s": 1.0,
        "steps": 10,
        "needs": "nav",
        "error": "",
        "blocked": "",
        "turns": 5,
        "path_len_m": 3.0,
        **kw,
    }


def write(path, rows, mtime):
    path.write_text(json.dumps(rows))
    os.utime(path, (mtime, mtime))


@pytest.fixture
def results(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RESULTS", tmp_path)
    return tmp_path


def run(capsys):
    report.main()
    return capsys.readouterr().out


def test_the_newest_file_owns_a_challenge(results, capsys):
    """The old sweep passed; the new one failed. The new one is the answer."""
    old = [ep("gallery_ring_tour", "oracle", True), ep("gallery_ring_tour", "random", False)]
    new = [ep("gallery_ring_tour", "oracle", False), ep("gallery_ring_tour", "random", False)]
    write(results / "bench_a.json", old, 1_000_000)
    write(results / "bench_b.json", new, 2_000_000)
    out = run(capsys)
    # The tally line, matched whole: "VALID 1" is a substring of "INVALID 1".
    assert "VALID 0" in out and "INVALID 1" in out, f"an old passing oracle outvoted the newer failure:{chr(10)}{out}"
    assert "superseded" in out, "the displaced rows were dropped silently"
    assert "2 episodes" in out, out


def test_a_byte_identical_copy_is_not_counted_twice(results, capsys):
    rows = [ep("gallery_ring_tour", "oracle", True), ep("gallery_ring_tour", "random", False)]
    write(results / "bench_a.json", rows, 1_000_000)
    write(results / "bench_copy.json", rows, 2_000_000)
    out = run(capsys)
    assert "byte-identical" in out, out
    assert "2 episodes from 1 file(s)" in out, out


def test_files_covering_different_challenges_are_all_used(results, capsys):
    write(
        results / "bench_a.json",
        [ep("gallery_ring_tour", "oracle", True), ep("gallery_ring_tour", "random", False)],
        1_000_000,
    )
    write(
        results / "bench_b.json",
        [ep("gallery_count_ring", "oracle", True), ep("gallery_count_ring", "random", False)],
        2_000_000,
    )
    out = run(capsys)
    assert "4 episodes from 2 file(s)" in out, out
    assert "superseded" not in out, "unrelated files were treated as a conflict"


def test_an_exact_timestamp_tie_is_not_broken_by_filename(results, capsys):
    """Neither file is newer. Picking the lexicographically later name would
    let RENAMING a file change a challenge from VALID to INVALID."""
    same = 1_500_000
    write(
        results / "bench_a.json",
        [ep("gallery_ring_tour", "oracle", True), ep("gallery_ring_tour", "random", False)],
        same,
    )
    write(
        results / "bench_z.json",
        [ep("gallery_ring_tour", "oracle", False), ep("gallery_ring_tour", "random", False)],
        same,
    )
    out = run(capsys)
    assert "AMBIGUOUS gallery_ring_tour" in out, out
    assert "bench_a.json and bench_z.json" in out, out
    # Not scored either way: no rows survive, so no verdict is invented.
    assert "VALID 0" in out and "INVALID 0" in out, out


def test_renaming_a_tied_file_cannot_change_the_verdict(results, capsys):
    """The same two files under swapped names must reach the same conclusion."""
    same = 1_500_000
    passing = [ep("gallery_ring_tour", "oracle", True), ep("gallery_ring_tour", "random", False)]
    failing = [ep("gallery_ring_tour", "oracle", False), ep("gallery_ring_tour", "random", False)]
    write(results / "bench_a.json", passing, same)
    write(results / "bench_z.json", failing, same)
    first = run(capsys)
    for f in results.glob("*.json"):
        f.unlink()
    write(results / "bench_a.json", failing, same)
    write(results / "bench_z.json", passing, same)
    second = run(capsys)
    assert ("AMBIGUOUS" in first) and ("AMBIGUOUS" in second)
    assert ("VALID 1" in first) == ("VALID 1" in second), (first, second)


def test_only_the_superseded_challenge_is_displaced(results, capsys):
    """A file that owns one challenge and loses another keeps the one it owns."""
    write(
        results / "bench_a.json",
        [
            ep("gallery_ring_tour", "oracle", True),
            ep("gallery_ring_tour", "random", False),
            ep("gallery_count_ring", "oracle", True),
            ep("gallery_count_ring", "random", False),
        ],
        1_000_000,
    )
    write(
        results / "bench_b.json",
        [ep("gallery_ring_tour", "oracle", False), ep("gallery_ring_tour", "random", False)],
        2_000_000,
    )
    out = run(capsys)
    assert "4 episodes" in out, out
    assert "gallery_count_ring" not in out.split("superseded")[-1].split(chr(10))[0]
    assert "VALID 1" in out and "INVALID 1" in out, out


def test_separate_sweeps_are_called_a_composite(results, capsys):
    write(
        results / "bench_a.json",
        [ep("gallery_ring_tour", "oracle", True), ep("gallery_ring_tour", "random", False)],
        1_000_000,
    )
    write(
        results / "bench_b.json",
        [ep("gallery_count_ring", "oracle", True), ep("gallery_count_ring", "random", False)],
        1_000_000 + 90_000,
    )
    assert "composite of separate sweeps" in run(capsys)
