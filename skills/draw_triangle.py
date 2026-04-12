#!/usr/bin/env python3
"""
DrawTriangle — Draw a small equilateral triangle on the floor with the robot arm.

Triangle is fixed: 10 cm sides, first vertex 10 cm in front of the robot.
The arm traces all three sides sequentially then lifts back to the ready pose.
"""

import math
import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class DrawTriangle(Skill):
    """Trace a 10 cm equilateral triangle on the floor using the arm end-effector."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    # Equilateral triangle geometry (metres).
    # Placed so the left base vertex is 20 cm forward, centred on the robot's y=0.
    SIDE = 0.10
    # x offset: how far in front of the robot the near edge starts
    X_OFFSET = 0.20
    # z for the drawing plane — 10 cm above floor so the TCP doesn't touch the ground
    Z_FLOOR = 0.05
    # z for the safe transit height above the drawing plane
    Z_LIFT = 0.20

    # Default arm orientation while drawing (pen pointing straight down)
    DRAW_ROLL = 0.0
    DRAW_PITCH = math.pi / 2   # 90° pitch → end-effector pointing down
    DRAW_YAW = 0.0

    # Seconds per segment; increase for slower, more accurate moves
    SEG_DURATION = 2.0
    LIFT_DURATION = 1.0

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "draw_triangle"

    def guidelines(self):
        return (
            "Draw a fixed equilateral triangle (10 cm sides) on the floor, "
            "starting 10 cm directly in front of the robot. "
            "No parameters are required. "
            "Use when the user asks the robot to draw a triangle."
        )

    def execute(self, **kwargs):
        """
        Trace the triangle on the floor, then lift the arm.

        Vertices (in robot base_link frame, metres):
          V1 — (X_OFFSET,           -SIDE/2,    Z_FLOOR)   left base corner  (0.20, -0.05, 0.05)
          V2 — (X_OFFSET,           +SIDE/2,    Z_FLOOR)   right base corner (0.20, +0.05, 0.05)
          V3 — (X_OFFSET + height,   0,          Z_FLOOR)   apex (forward)    (~0.29,  0.00, 0.05)
        """
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        height = self.SIDE * math.sqrt(3) / 2

        v1 = (self.X_OFFSET,          -self.SIDE / 2, self.Z_FLOOR)
        v2 = (self.X_OFFSET,          +self.SIDE / 2, self.Z_FLOOR)
        v3 = (self.X_OFFSET + height,  0.0,            self.Z_FLOOR)

        # ── 1. Move to safe height above V1 ────────────────────────────────
        self.logger.info("[DrawTriangle] Moving to transit height above V1")
        ok = self._move(v1[0], v1[1], self.Z_LIFT, duration=self.LIFT_DURATION)
        if not ok:
            return "Failed to reach starting position", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled before drawing", SkillResult.CANCELLED

        # ── 2. Touch down at V1 ─────────────────────────────────────────────
        self._send_feedback("Touching down at vertex 1")
        ok = self._move(*v1, duration=self.LIFT_DURATION)
        if not ok:
            return "Failed to touch down at V1", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled at V1", SkillResult.CANCELLED

        # ── 3. Draw V1 → V2 ─────────────────────────────────────────────────
        self._send_feedback("Drawing side 1 (V1 → V2)")
        self.logger.info(f"[DrawTriangle] V1→V2: {v1} → {v2}")
        ok = self._move(*v2, duration=self.SEG_DURATION)
        if not ok:
            return "Failed to draw side 1", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled during side 1", SkillResult.CANCELLED

        # ── 4. Draw V2 → V3 ─────────────────────────────────────────────────
        self._send_feedback("Drawing side 2 (V2 → V3)")
        self.logger.info(f"[DrawTriangle] V2→V3: {v2} → {v3}")
        ok = self._move(*v3, duration=self.SEG_DURATION)
        if not ok:
            return "Failed to draw side 2", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled during side 2", SkillResult.CANCELLED

        # ── 5. Draw V3 → V1 (close the triangle) ───────────────────────────
        self._send_feedback("Closing triangle (V3 → V1)")
        self.logger.info(f"[DrawTriangle] V3→V1: {v3} → {v1}")
        ok = self._move(*v1, duration=self.SEG_DURATION)
        if not ok:
            return "Failed to close triangle", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled while closing", SkillResult.CANCELLED

        # ── 6. Lift arm ─────────────────────────────────────────────────────
        self._send_feedback("Lifting arm")
        self._move(v1[0], v1[1], self.Z_LIFT, duration=self.LIFT_DURATION)

        self.logger.info("[DrawTriangle] Triangle complete")
        return (
            f"Triangle drawn: {self.SIDE * 100:.0f} cm equilateral, "
            f"starting {self.X_OFFSET * 100:.0f} cm in front of robot",
            SkillResult.SUCCESS,
        )

    def _move(self, x: float, y: float, z: float, duration: float) -> bool:
        """Move end-effector to (x, y, z) and wait for the motion to finish."""
        ok = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=z,
            roll=self.DRAW_ROLL,
            pitch=self.DRAW_PITCH,
            yaw=self.DRAW_YAW,
            duration=duration,
        )
        if ok:
            time.sleep(duration)
        return bool(ok)

    def cancel(self):
        self._cancelled = True
        return "Triangle drawing cancelled"
