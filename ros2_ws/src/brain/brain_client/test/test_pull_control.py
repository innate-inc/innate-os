# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import pytest

from workspace.innate_skills.arm.pull_control import PullGuidance, load_delta, normalize


def test_pull_guidance_slows_and_reverses_when_resistance_worsens():
    guidance = PullGuidance(normalize(-1.0, 0.0, 0.0), soft_delta=10.0)

    first_heading, first_scale = guidance.command(12.0)
    second_heading, second_scale = guidance.command(14.0)

    assert first_scale == second_scale == 0.45
    assert first_heading[1] < 0.0
    assert second_heading[1] > 0.0
    assert second_heading[0] == pytest.approx(first_heading[0])
    assert load_delta((7.0, 2.0, -3.0, 1.0, 0.0), (1.0, 2.0, 3.0, 1.0, 0.0)) == 6.0
