# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The motion gate, the one rule the brain and the people engine share.

It was lifted out of ``perception/camera.py`` so both can import it, so these
tests pin the behaviour that lift had to preserve: the sample interval, the
two-hot-samples debounce, the ego-motion suppressions, and the cooldown. The
clock is driven by hand — the gate reads ``time.monotonic`` and a test that
slept its way through a ten-second cooldown would not be worth running.
"""

import cv2
import numpy as np
import pytest

from brain_client.perception import motion_gate
from brain_client.perception.motion_gate import (
    MOTION_COOLDOWN_SEC,
    MOTION_HOT_SAMPLES,
    MOTION_SAMPLE_SEC,
    MotionGate,
)

SIZE = (480, 640)


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test advances itself."""

    now = [1000.0]
    monkeypatch.setattr(motion_gate.time, "monotonic", lambda: now[0])
    return now


def still_frame(shade: int = 100) -> bytes:
    ok, encoded = cv2.imencode(".jpg", np.full((*SIZE, 3), shade, np.uint8))
    assert ok
    return bytes(encoded)


def moved_frame(step: int) -> bytes:
    """The same scene with a large block in a different place: well past the 2%
    of moved pixels the gate calls motion. The block walks across the frame and
    back, so no two consecutive steps look alike."""
    frame = np.full((*SIZE, 3), 100, np.uint8)
    left = 40 + 60 * (step % 8)
    cv2.rectangle(frame, (left, 60), (left + 160, 420), (230, 230, 230), -1)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return bytes(encoded)


def feed(gate: MotionGate, clock, frames, *, pitch: float = 0.0, suppressed: bool = False) -> list[bool]:
    fired = []
    for frame in frames:
        fired.append(gate.observe(frame, pitch, suppressed))
        clock[0] += MOTION_SAMPLE_SEC
    return fired


def test_a_moving_scene_fires_once_the_debounce_has_two_hot_samples(clock):
    gate = MotionGate()
    fired = feed(gate, clock, [moved_frame(step) for step in range(MOTION_HOT_SAMPLES + 1)])
    assert fired == [False, False, True]  # the first sample is only the baseline


def test_a_still_scene_never_fires(clock):
    gate = MotionGate()
    assert not any(feed(gate, clock, [still_frame() for _ in range(6)]))


def test_one_hot_sample_alone_is_an_exposure_step_not_motion(clock):
    """The light comes on: one sample moves, and then the scene is still again
    at its new brightness."""
    gate = MotionGate()
    frames = [still_frame(100), still_frame(100), still_frame(170), still_frame(170), still_frame(170)]
    assert not any(feed(gate, clock, frames))


def test_samples_closer_together_than_the_interval_are_ignored(clock):
    """Six moving frames inside one sample interval are one sample, not six."""
    gate = MotionGate()
    for step in range(4):
        assert gate.observe(moved_frame(step), 0.0, False) is False
        clock[0] += MOTION_SAMPLE_SEC / 4.0  # four times too fast
    assert gate.consume_peak() == 0.0  # only the baseline sample was ever taken


def test_the_robot_driving_suppresses_its_own_ego_motion(clock):
    gate = MotionGate()
    assert not any(feed(gate, clock, [moved_frame(step) for step in range(4)], suppressed=True))


def test_a_moving_head_is_not_a_moving_scene(clock):
    gate = MotionGate()
    for step, pitch in enumerate((0.0, 5.0, 10.0, 15.0)):
        assert gate.observe(moved_frame(step), pitch, False) is False
        clock[0] += MOTION_SAMPLE_SEC


def test_the_baseline_keeps_updating_while_suppressed(clock):
    """Coming out of suppression must not diff against an ancient frame."""
    gate = MotionGate()
    feed(gate, clock, [moved_frame(step) for step in range(3)], suppressed=True)
    assert gate.observe(moved_frame(2), 0.0, False) is False  # same scene as the last suppressed sample


def test_sustained_motion_re_fires_only_after_the_cooldown(clock):
    gate = MotionGate()
    feed(gate, clock, [moved_frame(step) for step in range(3)])
    assert not any(feed(gate, clock, [moved_frame(step) for step in range(3, 9)]))
    clock[0] += MOTION_COOLDOWN_SEC
    assert feed(gate, clock, [moved_frame(step) for step in range(9, 12)])[0] is True


def test_an_undecodable_frame_is_skipped_rather_than_fired_on(clock):
    gate = MotionGate()
    assert gate.observe(b"not a jpeg", 0.0, False) is False


def test_the_peak_fraction_is_read_and_reset(clock):
    gate = MotionGate()
    feed(gate, clock, [moved_frame(step) for step in range(3)])
    assert gate.consume_peak() > 0.02
    assert gate.consume_peak() == 0.0


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
