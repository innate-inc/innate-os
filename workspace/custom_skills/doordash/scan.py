# SPDX-License-Identifier: Apache-2.0
import json
import math
from pathlib import Path

from innate import Mobility, Odometry, Skill, SkillReturn

STATE = Path.home() / ".thomas_scan_state.json"
MIN_TRAVEL_M = 1.5  # must move this far before scanning again


class Scan(Skill):
    """Look around from where you stand: one controlled full rotation in
    three steps, pausing to observe between steps. This is the ONLY way to
    look around — never rotate with navigate_to_position. It refuses to run
    twice in the same spot: you must travel at least 1.5 meters between
    scans, so if it refuses, MOVE somewhere new instead."""

    mobility: Mobility
    odom: Odometry

    def execute(self) -> SkillReturn:
        o = self.odom
        if o is not None and STATE.exists():
            try:
                last = json.loads(STATE.read_text())
                dist = math.hypot(o.x - last["x"], o.y - last["y"])
                if dist < MIN_TRAVEL_M:
                    self.fail(
                        f"You already scanned here ({dist:.1f}m from your last scan). "
                        "Do NOT rotate again — travel at least 2 meters toward a new "
                        "area (memory hint, doorway, or open space), then scan there."
                    )
            except (KeyError, ValueError):
                pass

        def get_xyt():
            cur = self.odom
            return None if cur is None else (cur.x, cur.y, cur.theta)

        for i in range(3):
            self.feedback(f"Scanning... view {i + 1} of 3")
            self.sleep(1.2)  # hold still so the brain gets a sharp frame
            if i < 2 and not self.mobility.rotate_by(get_xyt, math.radians(120.0), kp=2.0, wz_max=0.8, wz_min=0.4, timeout=15.0, logger=self.logger):
                self.fail("Scan turn timed out — travel to a new spot and try there.")
        self.sleep(1.2)

        if o is not None:
            STATE.write_text(json.dumps({"x": o.x, "y": o.y}))
        return ("Scan complete — you observed three views of this area. If any person or "
                "person-part appeared in ANY of them, target-lock them now. Otherwise "
                "travel to a new area; scanning here again is not allowed.")
