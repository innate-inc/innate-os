# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time

from pydantic import BaseModel

from innate import Mobility, People, Skill, SkillOutput, SkillReturn

# The band the person should end up in, and how straight-on "facing them" is.
TARGET_RANGE_M = 1.35
RANGE_TOL_M = 0.15
FACING_TOL_DEG = 8.0
# Past this bearing the robot turns before it drives: driving at someone well
# off to the side walks a curve through whatever is between them.
TURN_FIRST_DEG = 25.0

LOOP_PERIOD = 0.1
CMD_DURATION = 0.4  # cmd_vel deadman: the base stops if this loop dies
# Consecutive in-band snapshots before the approach is done — counted by their
# stamps: this loop runs at 10 Hz against a snapshot published at up to 5 Hz, so
# counting iterations would declare arrival on one look at the person.
ARRIVE_FRAMES = 3

MAX_LINEAR = 0.25
MAX_REVERSE = 0.10
MIN_LINEAR = 0.05
MAX_ANGULAR = 0.8
MIN_ANGULAR = 0.15
LINEAR_GAIN = 0.35  # m/s per metre of range error
TURN_GAIN = 1.2  # rad/s per radian of bearing error

LOST_GRACE_SEC = 3.0
TIMEOUT_SEC = 60.0


class ApproachResult(BaseModel):
    """Structured payload on .data for chaining callers."""

    tag: str
    name: str | None
    range_m: float


def servo(error: float, gain: float, v_min: float, v_max: float, deadband: float) -> float:
    """P-servo axis: zero inside the deadband, else gain*error clamped into
    [v_min, v_max] with the sign of the error."""
    if abs(error) <= deadband:
        return 0.0
    velocity = max(-v_max, min(v_max, gain * error))
    return math.copysign(v_min, velocity) if abs(velocity) < v_min else velocity


def lost_message(label: str, seen: bool) -> str:
    if seen:
        return f"I can see {label} but can't tell how far away they are."
    return f"Lost {label} — they left or moved out of sight."


class ApproachPerson(Skill):
    """Walk up to a person the robot can see and stop about 1.3 m in front of them,
    facing them. Give the tag from the People block ('P3') or their name ('Ana');
    the tag is exact, a name picks the nearest person of that name. Use it when
    being close is the point -- handing something over, hearing someone better, or
    getting a proper look at a face. It stops as soon as they walk off or drop out
    of sight, and it does not avoid obstacles, so keep the floor between you clear."""

    people: People
    mobility: Mobility

    def execute(self, who: str) -> SkillReturn:
        person = self.people.find(who)
        if person is None:
            self.fail(f"I can't see {who} right now.")
        tag = person.tag
        label = person.name or person.tag

        deadline = time.monotonic() + TIMEOUT_SEC
        last_fix = time.monotonic()
        arrived: set[float] = set()  # the distinct snapshots that saw them in the band
        try:
            while True:
                # Re-read every loop: the person is walking too, and a snapshot
                # the engine stopped confirming reads as nobody at all.
                person = self.people.find(tag)
                distance = person.range_m if person is not None else None
                if person is None or distance is None:
                    if time.monotonic() - last_fix > LOST_GRACE_SEC:
                        self.fail(lost_message(label, seen=person is not None))
                    arrived.clear()  # a look that measured nothing breaks the run
                    self.mobility.stop()
                    self.sleep(LOOP_PERIOD)
                    continue

                last_fix = time.monotonic()
                bearing = person.bearing_deg
                # No bearing is not "straight ahead": it is not knowing, and
                # standing still on it would report facing somebody sideways.
                facing = bearing is not None and abs(bearing) <= FACING_TOL_DEG
                if abs(distance - TARGET_RANGE_M) <= RANGE_TOL_M and facing:
                    arrived.add(person.stamp)
                    self.mobility.stop()
                    if len(arrived) >= ARRIVE_FRAMES:
                        return SkillOutput(
                            f"Standing {distance:.2f} m from {label}, facing them.",
                            ApproachResult(tag=tag, name=person.name, range_m=round(distance, 2)),
                        )
                else:
                    arrived.clear()
                    self._drive(distance - TARGET_RANGE_M, bearing if bearing is not None else 0.0)

                if time.monotonic() > deadline:
                    self.fail(f"Gave up approaching {label}, still {distance:.2f} m away.")
                self.sleep(LOOP_PERIOD)
        finally:
            self.mobility.stop()

    def _drive(self, range_error: float, bearing_deg: float) -> None:
        angular = servo(math.radians(bearing_deg), TURN_GAIN, MIN_ANGULAR, MAX_ANGULAR, math.radians(FACING_TOL_DEG))
        linear = 0.0
        if abs(bearing_deg) <= TURN_FIRST_DEG:
            linear = max(servo(range_error, LINEAR_GAIN, MIN_LINEAR, MAX_LINEAR, RANGE_TOL_M), -MAX_REVERSE)
        self.mobility.send_cmd_vel(linear_x=linear, angular_z=angular, duration=CMD_DURATION)
