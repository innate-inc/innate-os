# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Joint-only grasp evidence for the rehearsed MARS 47 fabric demo."""
import math
import time
from innate.exceptions import SkillFailed

# Same observed empty range/cutoff as PickLegos on MARS 47. This is a demo
# calibration, not a universal gripper constant. Recheck after hardware changes.
EMPTY_J6 = -0.03
MAX_HELD_J6 = 0.8


def closing_command(manipulation):
    target = getattr(manipulation, '_grip_target', None)
    return isinstance(target, (int, float)) and math.isfinite(target) and target <= -0.3


def fresh_sock_held(host):
    """Two fresh, stable readings after a close. Unknown feedback is an error.

    A stalled claw can mimic an object; this intentionally accepts that demo
    assumption. An open claw, stale stream or NaN never counts as a sock.
    """
    if not closing_command(host.manipulation):
        raise SkillFailed('No closing command; cannot infer a held sock from finger spacing')
    previous = host.joint_states
    values = []
    deadline = time.monotonic() + .5
    while time.monotonic() < deadline:
        host.sleep(.02)
        sample = host.joint_states
        if sample is None or sample is previous:
            continue
        previous = sample
        if len(sample.position) < 6:
            values.clear()
            continue
        j6 = sample.position[5]
        if not math.isfinite(j6) or not -.2 <= j6 < MAX_HELD_J6:
            values.clear()
            continue
        values.append(j6)
        if len(values) >= 2 and abs(values[-1]-values[-2]) <= .01:
            states = [v > EMPTY_J6 for v in values[-2:]]
            if states[0] == states[1]:
                host.logger.info(f'[SockGrip] j6={j6:.4f}, held={states[1]}')
                return states[1]
    raise SkillFailed('No stable fresh gripper feedback; stopping')
