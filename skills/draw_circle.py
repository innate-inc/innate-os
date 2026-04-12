#!/usr/bin/env python3
"""
DrawCircle — Draw a circle on the floor with the robot arm.

The circle is defined by its radius and center position (x, y).
The arm traces the circumference then lifts back to the ready pose.
"""

import math
import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class DrawCircle(Skill):
    """Trace a circle on the floor using the arm end-effector."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    # Default circle geometry (metres).
    # z for the drawing plane — 10 cm above floor so the TCP doesn't touch the ground
    Z_FLOOR = 0.05
    # z for the safe transit height above the drawing plane
    Z_LIFT = 0.20
    # Number of waypoints for smooth circle approximation (10 degrees per waypoint)
    NUM_WAYPOINTS = 36

    # Default arm orientation while drawing (end-effector pointing down)
    DRAW_ROLL = 0.0
    DRAW_PITCH = math.pi / 2   # 90° pitch → end-effector pointing down
    DRAW_YAW = 0.0

    # Seconds per segment
    SEG_DURATION = 0.5
    LIFT_DURATION = 1.0

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "draw_circle"

    def guidelines(self):
        return (
            "Draw a circle on the floor with the robot arm. "
            "Parameters: radius (metres), center_x (metres), center_y (metres). "
            "If radius is not provided, defaults to 0.10 m (10 cm). "
            "If center_x is not provided, defaults to 0.20 m (20 cm in front of robot). "
            "If center_y is not provided, defaults to 0.00 m (centered on robot). "
            "Use when the user asks the robot to draw a circle."
        )

    def _compute_waypoints(self, center_x: float, center_y: float) -> list[tuple]:
        """
        Compute waypoints around the circle circumference.

        Args:
            center_x: x coordinate of circle center (metres)
            center_y: y coordinate of circle center (metres)

        Returns:
            List of (x, y, z, roll, pitch, yaw) tuples for each waypoint
        """
        waypoints = []
        for i in range(self.NUM_WAYPOINTS + 1):
            angle = 2 * math.pi * i / self.NUM_WAYPOINTS
            x = center_x + self.radius * math.cos(angle)
            y = center_y + self.radius * math.sin(angle)
            waypoints.append((x, y, self.Z_FLOOR, self.DRAW_ROLL, self.DRAW_PITCH, self.DRAW_YAW))
        return waypoints

    def execute(self, **kwargs):
        """
        Trace the circle on the floor, then lift the arm.

        Parameters (from kwargs):
            radius: Circle radius in metres (default: 0.10)
            center_x: Center x position in robot base frame (default: 0.20)
            center_y: Center y position in robot base frame (default: 0.00)
        """
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        # Get parameters with defaults
        self.radius = kwargs.get("radius", 0.10)
        center_x = kwargs.get("center_x", 0.20)
        center_y = kwargs.get("center_y", 0.00)

        # Validate radius
        if self.radius <= 0:
            return "Radius must be positive", SkillResult.FAILURE

        self.logger.info(f"[DrawCircle] Drawing circle: radius={self.radius}m, center=({center_x}, {center_y})")

        # Compute waypoints around the circle
        waypoints = self._compute_waypoints(center_x, center_y)
        start_point = waypoints[0]

        # ── 1. Move to safe height above starting point ───────────────────
        self.logger.info("[DrawCircle] Moving to transit height above starting point")
        ok = self._move(start_point[0], start_point[1], self.Z_LIFT, self.LIFT_DURATION)
        if not ok:
            return "Failed to reach starting position", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled before drawing", SkillResult.CANCELLED

        # ── 2. Touch down at starting point on circle ──────────────────────
        self._send_feedback("Touching down to start circle")
        ok = self._move(*start_point[:3], duration=self.LIFT_DURATION)
        if not ok:
            return "Failed to touch down at starting point", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled at start point", SkillResult.CANCELLED

        # ── 3. Trace the circle through all waypoints ─────────────────────
        for i in range(1, len(waypoints)):
            wp = waypoints[i]
            self.logger.info(f"[DrawCircle] Moving to waypoint {i}/{len(waypoints) - 1}")
            ok = self._move(wp[0], wp[1], wp[2], duration=self.SEG_DURATION)
            if not ok:
                return f"Failed to trace circle at waypoint {i}", SkillResult.FAILURE
            if self._cancelled:
                return f"Cancelled during circle tracing at waypoint {i}", SkillResult.CANCELLED

        # ── 4. Lift arm ─────────────────────────────────────────────────────
        self._send_feedback("Lifting arm")
        self._move(start_point[0], start_point[1], self.Z_LIFT, duration=self.LIFT_DURATION)

        self.logger.info("[DrawCircle] Circle complete")
        return (
            f"Circle drawn: {self.radius * 100:.0f} cm radius, "
            f"center at ({center_x * 100:.0f}, {center_y * 100:.0f}) cm",
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
        return "Circle drawing cancelled"
