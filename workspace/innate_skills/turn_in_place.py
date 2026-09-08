# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time

from pydantic import BaseModel

from innate import Mobility, Odometry, Skill, SkillOutput, SkillReturn

# Allowed angular speeds (rad/s). Slow on purpose: no obstacle awareness here.
MIN_SPEED = 0.2
MAX_SPEED = 1.0
DEFAULT_SPEED = 0.5


class TurnResult(BaseModel):
    """Structured payload on .data for chaining callers."""

    turned_degrees: float


class TurnInPlace(Skill):
    """Turn the robot in place by angle_degrees: positive turns left
    (counter-clockwise), negative turns right. Uses odometry only -- no map or
    path planning. E.g. turn right 90 degrees -> angle_degrees=-90."""

    mobility: Mobility
    odom: Odometry

    def execute(self, angle_degrees: float, speed: float = DEFAULT_SPEED) -> SkillReturn:
        if angle_degrees == 0.0:
            return SkillOutput("Turned 0 degrees", TurnResult(turned_degrees=0.0))
        target = abs(angle_degrees)
        sign = 1.0 if angle_degrees > 0 else -1.0
        velocity = math.copysign(min(max(abs(speed), MIN_SPEED), MAX_SPEED), angle_degrees)
        deadline = time.time() + math.radians(target) / abs(velocity) * 3.0 + 2.0

        # accumulate wrapped yaw deltas so multi-turn and the ±180° seam work
        turned = 0.0
        last_yaw = self.odom.theta_degrees
        while turned < target:
            if time.time() > deadline:
                self.fail(f"Stuck: turned only {turned:.0f} of {target:.0f} degrees")
            self.mobility.send_cmd_vel(angular_z=velocity, duration=0.5)
            self.sleep(0.05)
            yaw = self.odom.theta_degrees
            turned += ((yaw - last_yaw + 180.0) % 360.0 - 180.0) * sign
            last_yaw = yaw

        self.mobility.stop()
        direction = "left" if angle_degrees > 0 else "right"
        return SkillOutput(
            f"Turned {turned:.0f} degrees {direction}",
            TurnResult(turned_degrees=round(turned, 1)),
        )
