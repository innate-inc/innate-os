"""On the live path our own failures must not be scored against the robot.

A roster mismatch, a state stream that goes quiet, a start the server refused,
a stream with no challenge block, and any websocket exception used to set
`error` and leave `blocked` empty -- and the final score selects on
`not e.blocked`, so each one contributed a failed challenge and a zero-goal
denominator to the robot's headline and stayed an unblocked loss in the JSON.
This is the same defect that was fixed for the in-process path.

These drive the real `_episode()` against a stubbed socket, because the
classification is what is under test.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

import live_runner

CID = "gallery_ring_tour"
ROSTER = json.dumps({"challenges": [{"id": CID}]})


class _Socket:
    """A websocket that replays the given frames, then does `after`."""

    def __init__(self, frames, after="quiet"):
        self._frames = list(frames)
        self._after = after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, _msg):
        return None

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        if self._after == "quiet":
            await asyncio.sleep(3600)  # never answers; the caller times out
        raise ConnectionError("socket died")


def connect_with(sock):
    def _connect(*a, **k):
        return sock

    return _connect


def run(monkeypatch, sock, timeout_s=1.0):
    # `import websockets` happens inside _episode, so the module itself is what
    # has to be replaced.
    import types

    stub = types.ModuleType("websockets")
    stub.connect = connect_with(sock)
    monkeypatch.setitem(sys.modules, "websockets", stub)
    return asyncio.run(live_runner._episode("ws://stub", CID, timeout_s=timeout_s, poll_s=0.05))


def test_a_challenge_missing_from_the_roster_is_ours(monkeypatch):
    ep = run(monkeypatch, _Socket([json.dumps({"challenges": [{"id": "something_else"}]})]))
    assert ep.blocked.startswith("harness:"), f"scored against the robot: {ep}"
    assert "roster" in ep.blocked
    assert ep.started is False, "we never asked it to start"


def test_a_stream_that_goes_quiet_is_ours(monkeypatch):
    ep = run(monkeypatch, _Socket([ROSTER]))
    assert ep.blocked.startswith("harness:"), f"scored against the robot: {ep}"
    assert "quiet" in ep.blocked
    assert ep.started is None, "we asked, so whether it started is not knowable here"


def test_a_dead_socket_is_ours(monkeypatch):
    ep = run(monkeypatch, _Socket([ROSTER], after="die"))
    assert ep.blocked.startswith("harness:"), f"scored against the robot: {ep}"
    assert "live stream failed" in ep.blocked


def test_a_stream_with_no_challenge_block_is_ours(monkeypatch):
    frames = [ROSTER] + [json.dumps({"something": "else"}) for _ in range(3)]
    ep = run(monkeypatch, _Socket(frames))
    assert ep.blocked.startswith("harness:"), f"scored against the robot: {ep}"


def test_a_blocked_live_episode_leaves_the_score():
    """The property all of the above rely on."""
    from runner import Episode

    rows = [
        Episode("live", "a", "brain", False, 0, 2, 0.0, "", 1.0, 0, blocked="harness: roster"),
        Episode("live", "b", "brain", False, 0, 2, 0.0, "", 1.0, 0),
    ]
    scored = [e for e in rows if not e.blocked]
    assert len(scored) == 1, "a harness fault stayed in the robot's denominator"


def test_a_real_robot_failure_is_still_scored(monkeypatch):
    """The fix must not excuse the robot: a challenge that runs and fails is
    an ordinary loss."""
    frames = [ROSTER] + [
        json.dumps({"challenge": {"active": {"id": CID, "state": "running", "goals": [{"done": False}]}}}),
        json.dumps({"challenge": {"active": {"id": CID, "state": "failed", "goals": [{"done": False}]}}}),
    ]
    ep = run(monkeypatch, _Socket(frames), timeout_s=5.0)
    assert ep.blocked == "", f"an ordinary failure was excused as a harness fault: {ep.blocked!r}"
    assert ep.started is True
    assert not ep.passed
