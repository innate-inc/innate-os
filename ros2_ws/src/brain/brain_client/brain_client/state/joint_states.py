# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing arm joint state. ROS-free on purpose."""

from dataclasses import dataclass
from functools import cached_property

from brain_client.state.dictcompat import LegacyMapping


@dataclass(frozen=True)
class JointStates(LegacyMapping):
    """An arm joint-state snapshot, read via ``self.joint_states`` in skills.

    The four tuples are parallel: index i of each describes the same joint
    (the gripper claw is index 5, aka j6 — ``self.arm.gripper`` reads it
    directly). ``of(name)`` looks a joint up by its published name.
    """

    name: tuple
    """Joint names, e.g. ("j1", ..., "j6")."""
    position: tuple
    """Joint positions in radians."""
    velocity: tuple
    """Joint velocities in rad/s."""
    effort: tuple
    """Signed actuator effort in percent of configured capacity."""
    received_at: float = 0.0
    """Local monotonic timestamp when this state was received."""

    def of(self, name: str) -> "tuple[float | None, float | None, float | None] | None":
        """(position, velocity, effort) for the named joint, or None if the
        name is unknown. Missing per-joint entries (a driver may publish
        fewer velocities/efforts than names) read as None."""
        try:
            i = self.name.index(name)
        except ValueError:
            return None

        def at(values):
            return values[i] if i < len(values) else None

        return (at(self.position), at(self.velocity), at(self.effort))

    # --- legacy dict compatibility ---------------------------------------
    # LAST_JOINT_STATES injected {"name", "position", ...} of lists through
    # 0.6.x; the LegacyMapping mixin keeps that access working (see
    # dictcompat.py). Do not delete.

    _legacy_hint = "the JointStates attributes (joint_states.position, joint_states.of(name), ...)"

    @cached_property
    def _legacy_dict(self) -> dict:
        """Exactly the 0.3.0-0.6.x injected shape: the four parallel tuples
        as lists."""
        return {
            "name": list(self.name),
            "position": list(self.position),
            "velocity": list(self.velocity),
            "effort": list(self.effort),
        }
