"""PlannerAgent: the oracle that plans its own route.

Takes a list of high-level STEPS derived from a challenge's goals (see
autoplan.py) and executes each by A*-ing over the sim's occupancy grid. This is
what makes the validity gate usable on the apartment: with a straight-line
follower, "the oracle failed" conflated "this challenge is broken" with "my
follower cannot get out of a room", and there is no reading of a benchmark
where those mean the same thing.

Steps:
    ("goto", x, y)            drive to a world point
    ("near", prop)            drive to a standoff beside a prop
    ("grab", prop)            acknowledge it (no arm; see below)
    ("put", prop, x, y)       teleport it to a point, modelling a place
    ("put_near", prop, other) teleport it beside another prop
    ("wait", seconds)         hold station (for Hold() dwells)

Carrying is abstract on purpose. These plans certify that a challenge's goals
are reachable and its geometry is navigable -- nothing about manipulation. A
challenge whose goal genuinely needs the arm is classified requires_arm and
gated differently (autoplan.classify).
"""

from __future__ import annotations

import math

from navplan import NavMap

V_MAX = 0.30
W_MAX = 1.2
ARRIVE_M = 0.10  # waypoint tolerance along a path
# Tolerance on the last waypoint of a leg, plus an escape hatch.
#
# Tight (0.07) and the base ORBITS: this gain overshoots, re-aims, overshoots
# back, and the leg never completes even while standing in the goal region.
# Loose (0.15) and legs that genuinely need a close approach stop short --
# "agent finished its plan" with the goal radius unmet. Neither number works
# for both, so precision stays tight and a leg is also accepted once the robot
# has simply been NEAR the target for a while: settled, not converged.
FINAL_M = 0.08
SETTLE_M = 0.22
SETTLE_S = 2.0
FACE_RAD = 0.30
NEAR_STANDOFF = 0.34  # inside every goal radius, outside the base footprint
REPLAN_AFTER_S = 12.0  # a leg that stops making progress gets one fresh plan


# A leg that moves less than STALL_M in STALL_S of sim time, after its one
# replan has been used, is deadlocked rather than slow.
#
# CALIBRATED, not guessed, and the first calibration was wrong. 0.10 m in 25 s
# looked like an obvious deadlock -- 0.4% of V_MAX -- and it reclassified
# household_take_orders, which had been passing, as stuck. That challenge
# really does grind: the robot squeezes a diagonal through two doorway openings
# that meet at a corner and takes 317 s to finish three goals it could do in
# 60. It was making progress, just barely.
#
# So the watch is now generous enough that anything which would EVENTUALLY
# succeed survives it, and only true deadlock trips it. Its value is turning a
# 900 s uninformative timeout into a 90 s named failure, not policing slowness
# -- slowness is already reported, as elapsed time and path length.
STALL_M = 0.15
STALL_S = 90.0


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class PlannerAgent:
    name = "oracle"

    def __init__(self, steps: list[tuple]):
        self.steps = steps
        self.i = 0
        self._post = None
        self.nav: NavMap | None = None
        self._path: list[tuple[float, float]] = []
        self._wp = 0
        self._t0: float | None = None
        self._leg_started: float | None = None
        self._replanned = False
        self._stall_from: tuple[float, float] | None = None
        self._stall_since: float | None = None
        self.failed_reason = ""

    def reset(self, mars, challenge, nav: NavMap | None = None) -> None:
        self.i = 0
        self._path, self._wp = [], 0
        self._t0 = self._leg_started = self._near_since = None
        self._replanned = False
        self._stall_from = None
        self._stall_since = None
        self.failed_reason = ""
        # The caller passes a map built BEFORE the challenge dropped its props.
        # occupancy_grid rasterises every static collision triangle, and a
        # dropped prop counts: building the map afterwards turned each target
        # into an obstacle and made "go to the sock" unreachable -- 30-odd
        # apartment challenges failed with "no path" to a cell that was free.
        # A real robot's nav map is the static world too; props are runtime
        # obstacles, not map features.
        self.nav = nav or NavMap.from_sim(mars)

    def bind_events(self, post) -> None:
        """Channel for answers (see challenges.Answered). Goal-position
        predicates are judged from the world; a reported answer has to be
        SENT, so the agent needs a way to speak."""
        self._post = post

    @property
    def done(self) -> bool:
        return self.i >= len(self.steps) or bool(self.failed_reason)

    @property
    def turns(self) -> int:
        """Plan steps consumed -- the REFERENCE action count for the episode.

        An LLM agent that takes 40 turns where the derived plan takes 6 is a
        finding; the same 40 with no denominator beside it is just a number.
        """
        return self.i

    def hear(self, line: dict) -> None:
        """The narrator spoke, and this agent deliberately ignores it.

        A scripted plan is derived from the challenge's goals, which already
        encode the FINAL state. The oracle's job is to witness that that state
        is reachable; a correction part-way through changes what an agent has
        to work out, not whether the destination exists. Staying deaf is what
        keeps the validity gate meaningful for scripted challenges instead of
        turning the oracle into a second, worse agent.
        """
        return None

    # -- execution --

    def act(self, mars, t: float) -> None:
        if self.done:
            mars.set_cmd_vel(0.0, 0.0)
            return
        step = self.steps[self.i]
        op = step[0]

        if op == "goto":
            self._leg(mars, t, step[1], step[2])
        elif op == "near":
            p = mars.object_centers().get(step[1])
            if p is None:
                self._advance()
                return
            x, y, _ = mars.pose()
            if math.hypot(p[0] - x, p[1] - y) <= NEAR_STANDOFF:
                mars.set_cmd_vel(0.0, 0.0)
                self._advance()
            else:
                self._leg(mars, t, p[0], p[1], stop_short=NEAR_STANDOFF)
        elif op == "grab":
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
        elif op == "put":
            mars.drop_prop_at(step[1], step[2], step[3])
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
        elif op == "put_near":
            other = mars.object_centers().get(step[2])
            if other is not None:
                mars.drop_prop_at(step[1], other[0], other[1] - 0.35)
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
        elif op == "answer":
            # The oracle simply reports the right value. That is all a
            # solvability witness needs to show -- that the goal CAN be
            # satisfied. It says nothing about whether an agent could work the
            # answer out, which is exactly what the challenge measures.
            if self._post is not None:
                self._post({"type": "answer", "value": step[1]})
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
        elif op == "say":
            # Same channel as "answer", a different envelope: Said() reads both,
            # because from the robot's side speaking IS speaking, and which
            # envelope a given stack puts it in is that stack's business.
            if self._post is not None:
                self._post({"type": "say", "text": step[1]})
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
        elif op == "wait":
            mars.set_cmd_vel(0.0, 0.0)
            if self._t0 is None:
                self._t0 = t
            if t - self._t0 >= step[1]:
                self._t0 = None
                self._advance()
        else:
            raise ValueError(f"unknown step {op!r}")

    def _advance(self) -> None:
        # A new leg gets a fresh stall watch, or a slow arrival poisons the next.
        self._stall_from, self._stall_since = None, None
        self.i += 1
        self._path, self._wp = [], 0
        self._leg_started = None
        self._near_since = None
        self._replanned = False

    def _leg(self, mars, t: float, tx: float, ty: float, stop_short: float = 0.0) -> None:
        x, y, yaw = mars.pose()
        if not self._path:
            path = self.nav.plan((x, y), (tx, ty))
            if path is None:
                # Unreachable is a RESULT, not a stall: report it so the gate
                # can say "goal not reachable" instead of "timed out".
                self.failed_reason = f"no path to ({tx:.2f}, {ty:.2f})"
                mars.set_cmd_vel(0.0, 0.0)
                return
            self._path, self._wp = path, 0
            self._leg_started = t

        if stop_short and math.hypot(tx - x, ty - y) <= stop_short:
            mars.set_cmd_vel(0.0, 0.0)
            self._advance()
            return

        # One replan if a leg stops making progress -- props get shoved, and a
        # path computed against the old layout can end inside one.
        if self._leg_started is not None and t - self._leg_started > REPLAN_AFTER_S and not self._replanned:
            self._replanned = True
            self._path, self._wp = [], 0
            self._leg_started = t
            self._stall_from, self._stall_since = None, None
            return

        # Stall watch: without this a wedged robot spends
        # the entire time limit and the episode is reported as "time limit",
        # which says it was slow when it was stuck.
        if self._stall_since is None or self._stall_from is None:
            self._stall_from, self._stall_since = (x, y), t
        elif math.hypot(x - self._stall_from[0], y - self._stall_from[1]) > STALL_M:
            self._stall_from, self._stall_since = (x, y), t
        elif t - self._stall_since > STALL_S:
            self.failed_reason = f"stuck at ({x:.2f}, {y:.2f}) heading for ({tx:.2f}, {ty:.2f})"
            mars.set_cmd_vel(0.0, 0.0)
            return

        wx, wy = self._path[self._wp]
        last = self._wp == len(self._path) - 1
        d = math.hypot(wx - x, wy - y)
        if last and d <= SETTLE_M:
            # Orbit escape: close enough for long enough counts as arrived.
            if self._near_since is None:
                self._near_since = t
            elif t - self._near_since >= SETTLE_S:
                mars.set_cmd_vel(0.0, 0.0)
                self._advance()
                return
        elif last:
            self._near_since = None
        if d <= (FINAL_M if last else ARRIVE_M):
            if last:
                mars.set_cmd_vel(0.0, 0.0)
                self._advance()
                return
            self._wp += 1
            wx, wy = self._path[self._wp]

        err = _wrap(math.atan2(wy - y, wx - x) - yaw)  # pose() yaw is RADIANS
        if abs(err) > FACE_RAD:
            mars.set_cmd_vel(0.0, max(-W_MAX, min(W_MAX, 2.0 * err)))
        else:
            dist = math.hypot(wx - x, wy - y)
            mars.set_cmd_vel(min(V_MAX, 0.8 * dist + 0.10), max(-W_MAX, min(W_MAX, 1.5 * err)))
