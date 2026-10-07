# SPDX-License-Identifier: Apache-2.0
from innate import Mobility, Skill, SkillReturn

SPEED = 0.1  # m/s, deliberately slow — we are driving blind backwards
MAX_M = 0.6


class StepBack(Skill):
    """Back up a short distance to get a better view of a person — use when
    you are TOO CLOSE (all you see is legs, a chair, or a torso filling the
    frame) and need to frame their face. This is the ONLY allowed backward
    movement: slow and capped at 0.6 meters. You have no rear camera, so
    never call it twice in a row — after one step_back, look and reassess."""

    mobility: Mobility

    def execute(self, distance_m: float = 0.4) -> SkillReturn:
        d = max(0.1, min(MAX_M, float(distance_m)))
        duration = d / SPEED
        self.mobility.send_cmd_vel(linear_x=-SPEED, duration=duration)
        self.sleep(duration + 0.2)
        return (f"Backed up {d:.1f}m slowly. Look now — if you can see their face, "
                "greet them; if you're still too close, approach the framing from an angle "
                "instead of backing up again.")
