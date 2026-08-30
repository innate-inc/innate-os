# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import importlib
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
pull_control = importlib.import_module("workspace.innate_skills.arm.pull_control")
PullGuidance = pull_control.PullGuidance
load_delta = pull_control.load_delta
normalize = pull_control.normalize
pull_drift = pull_control.pull_drift
pull_progress = pull_control.pull_progress


def test_pull_guidance_slows_and_reverses_when_resistance_worsens():
    guidance = PullGuidance(normalize(-1.0, 0.0, 0.0), soft_delta=10.0)

    first_heading, first_scale = guidance.command(12.0)
    second_heading, second_scale = guidance.command(14.0)

    assert first_scale == second_scale == 0.45
    assert first_heading[1] < 0.0
    assert second_heading[1] > 0.0
    assert second_heading[0] == pytest.approx(first_heading[0])
    assert load_delta((7.0, 2.0, -3.0, 1.0, 0.0), (1.0, 2.0, 3.0, 1.0, 0.0)) == 6.0


def test_pull_progress_ignores_sideways_motion_and_reports_drift():
    direction = normalize(1.0, 0.0, 0.0)

    assert pull_progress((0.1, 0.0, 0.2), (0.12, 0.03, 0.19), direction) == pytest.approx(0.02)
    cross_track, vertical_drift = pull_drift((0.1, 0.0, 0.2), (0.12, 0.03, 0.19), direction)

    assert cross_track == pytest.approx(math.hypot(0.03, 0.01))
    assert vertical_drift == pytest.approx(0.01)
