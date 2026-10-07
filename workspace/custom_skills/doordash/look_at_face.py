# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

from innate import Head, Skill, SkillReturn

_STATE = Path.home() / ".thomas_head_angle"


class LookAtFace(Skill):
    """Tilt the camera UP to face level and keep it there, so you can see the
    face of a person standing or sitting in front of you. ALWAYS call this
    when you are about to talk to a person, BEFORE greeting them. Call with
    angle_degrees=0 to look back down when you leave."""

    head: Head

    def execute(self, angle_degrees: float = 18.0) -> SkillReturn:
        angle = int(max(-25, min(20, angle_degrees)))
        try:
            if _STATE.exists() and int(_STATE.read_text()) == angle:
                return f"Camera already at {angle} degrees — no need to call this again."
        except ValueError:
            pass
        self.head.set_position(angle)
        _STATE.write_text(str(angle))
        self.sleep(0.6)  # let the head settle so the next frame is sharp
        if angle > 0:
            return f"Camera raised to {angle} degrees — faces should now be visible."
        return f"Camera set to {angle} degrees."
