# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Publish-until-stopped policy for top-priority teleop velocity sources."""


class TeleopPublishGate:
    """Stream motion, emit one settling zero, then release the velocity mux."""

    def __init__(self, epsilon: float = 1e-3):
        if epsilon < 0.0:
            raise ValueError("epsilon must be non-negative")
        self._epsilon = epsilon
        self._active = False

    def next_command(self, linear: float, angular: float, *, enabled: bool = True) -> tuple[float, float] | None:
        moving = enabled and (abs(linear) > self._epsilon or abs(angular) > self._epsilon)
        if moving:
            self._active = True
            return linear, angular
        if self._active:
            self._active = False
            return 0.0, 0.0
        return None
