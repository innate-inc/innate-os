# SPDX-License-Identifier: Apache-2.0
import math

from innate import Mobility, Odometry, Skill, SkillReturn

TURNS = {
    "left": 90.0,
    "slight_left": 45.0,
    "right": -90.0,
    "slight_right": -45.0,
    "straight": 0.0,
    "behind": 180.0,      # behind YOU (the robot): turn around
    "behind_me": 0.0,     # behind THE PERSON: go around them and continue past
}


class GoDirection(Skill):
    """Turn precisely toward a direction someone pointed you in. Use this
    (NOT navigate_to_position) whenever a person tells you where to go:
    direction is one of 'left', 'slight_left', 'right', 'slight_right',
    'straight', 'behind' (behind YOU the robot — turns around), or
    'behind_me' (the person said "behind me": the target is PAST them).
    Directions are relative to where YOU are facing. The turn is exact and
    closed-loop. After it completes, LOOK at your camera, then
    approach what you see with short forward navigate_to_position hops."""

    mobility: Mobility
    odom: Odometry

    def execute(self, direction: str) -> SkillReturn:
        d = direction.strip().lower().replace(" ", "_").replace("-", "_")
        if d not in TURNS:
            self.fail(f"Unknown direction '{direction}'. Use one of: {', '.join(TURNS)}.")
        angle_deg = TURNS[d]
        if d == "behind_me":
            return ("The target is BEHIND the person you were talking to. Drive around them — "
                    "sidestep with a short hop (x=1.0, y=1.0 or y=-1.0, whichever side is open), "
                    "keep 1 meter clearance from them, then continue straight past and look.")
        if angle_deg == 0.0:
            return "Facing straight ahead already — look at your camera and approach in short forward hops."

        def get_xyt():
            o = self.odom
            return None if o is None else (o.x, o.y, o.theta)

        remaining = math.radians(angle_deg)
        # Split big turns so odometry wrap never confuses the controller.
        while abs(remaining) > math.radians(95.0):
            step = math.copysign(math.radians(90.0), remaining)
            if not self.mobility.rotate_by(get_xyt, step, kp=2.0, wz_max=0.8, wz_min=0.4, timeout=15.0, logger=self.logger):
                self.fail(f"Turn toward '{d}' timed out mid-way — look and re-orient visually.")
            remaining -= step
            self.sleep(0.2)
        if abs(remaining) > math.radians(1.0):
            if not self.mobility.rotate_by(get_xyt, remaining, kp=2.0, wz_max=0.8, wz_min=0.4, timeout=15.0, logger=self.logger):
                self.fail(f"Turn toward '{d}' timed out — look and re-orient visually.")
        return (f"Turned {angle_deg:+.0f} degrees ({d}). Now LOOK at your camera: describe what "
                "you see, then approach your target in short forward hops.")
