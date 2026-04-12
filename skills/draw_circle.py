#!/usr/bin/env python3
"""
DrawCircle — Draw a circle of specified diameter on the floor with the robot arm.

The circle is drawn by moving the arm end-effector along the circumference,
tracing the circle shape on the floor.
"""

import math
import time

from brain_client.skill_types import Interface, InterfaceType, Skill, SkillResult


class DrawCircle(Skill):
    """Trace a circle on the floor using the arm end-effector."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    # Default circle geometry (metres).
    # Center of circle positioned relative to robot base_link
    DEFAULT_DIAMETER = 0.20  # 20 cm diameter
    X_OFFSET = 0.25  # 25 cm in front of the robot
    Y_OFFSET = 0.0  # centered on robot's y=0
    Z_FLOOR = 0.05  # 5 cm above floor (so TCP doesn't touch ground)
    Z_LIFT = 0.15  # 15 cm for safe transit height

    # Arm orientation (pen pointing down)
    DRAW_ROLL = 0.0
    DRAW_PITCH = math.pi / 2  # 90° pitch → end-effector pointing down
    DRAW_YAW = 0.0

    # Drawing parameters
    DEFAULT_NUM_POINTS = 36  # Number of points to trace (1 per 10 degrees)
    SEG_DURATION = 0.5  # Seconds per segment
    LIFT_DURATION = 1.0  # Seconds for lift/lower moves

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "draw_circle"

    def guidelines(self):
        return (
            "Draw a circle on the floor using the robot arm. "
            "The circle has a configurable diameter (default 20 cm) and position "
            "(default 25 cm in front of the robot, centered). "
            "Parameters: diameter (meters), x_offset (meters, forward from robot), "
            "y_offset (meters, left/right from robot center), num_points (trace resolution). "
            "Use when the user asks the robot to draw a circle or trace a circular shape."
        )

    def execute(self, diameter=None, x_offset=None, y_offset=None, num_points=None, **kwargs):
        """
        Trace a circle on the floor with the arm.

        Args:
            diameter: Circle diameter in meters (default: 0.20 m / 20 cm)
            x_offset: X position of circle center relative to robot base (default: 0.25 m)
            y_offset: Y position of circle center relative to robot base (default: 0.0 m)
            num_points: Number of points to trace the circle (default: 36)

        Returns:
            tuple: (result_message, SkillResult)
        """
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        # Use defaults if parameters not provided
        circle_diameter = diameter if diameter is not None else self.DEFAULT_DIAMETER
        circle_x_offset = x_offset if x_offset is not None else self.X_OFFSET
        circle_y_offset = y_offset if y_offset is not None else self.Y_OFFSET
        points = num_points if num_points is not None else self.DEFAULT_NUM_POINTS

        radius = circle_diameter / 2

        self.logger.info(
            f"[DrawCircle] Drawing circle: diameter={circle_diameter}m, "
            f"center=({circle_x_offset}, {circle_y_offset}), points={points}"
        )
        self._send_feedback(f"Starting to draw {circle_diameter * 100:.0f} cm diameter circle")

        # Calculate circle points
        angle_step = 2 * math.pi / points

        # Start at the rightmost point of the circle (at angle = 0)
        start_x = circle_x_offset + radius
        start_y = circle_y_offset

        # ── 1. Move to transit height above starting point ──────────────────
        self.logger.info("[DrawCircle] Moving to starting position")
        ok = self._move(start_x, start_y, self.Z_LIFT, duration=self.LIFT_DURATION)
        if not ok:
            return "Failed to reach starting position", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled before drawing", SkillResult.CANCELLED

        # ── 2. Touch down at starting point ────────────────────────────────
        self._send_feedback("Touching down to start drawing")
        ok = self._move(start_x, start_y, self.Z_FLOOR, duration=self.LIFT_DURATION)
        if not ok:
            return "Failed to touch down at starting point", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled at start", SkillResult.CANCELLED

        # ── 3. Trace the circle ────────────────────────────────────────────
        for i in range(1, points + 1):
            angle = i * angle_step
            x = circle_x_offset + radius * math.cos(angle)
            y = circle_y_offset + radius * math.sin(angle)

            if i % 9 == 0:  # Send feedback every quarter of the circle
                progress = int((i / points) * 100)
                self._send_feedback(f"Drawing circle: {progress}% complete")

            self.logger.info(f"[DrawCircle] Point {i}/{points}: ({x:.3f}, {y:.3f})")
            ok = self._move(x, y, self.Z_FLOOR, duration=self.SEG_DURATION)
            if not ok:
                return f"Failed to draw point {i}/{points}", SkillResult.FAILURE
            if self._cancelled:
                return f"Cancelled at point {i}/{points}", SkillResult.CANCELLED

        # ── 4. Lift arm ─────────────────────────────────────────────────────
        self._send_feedback("Lifting arm")
        self._move(start_x, start_y, self.Z_LIFT, duration=self.LIFT_DURATION)

        self.logger.info("[DrawCircle] Circle complete")
        return (
            f"Circle drawn: {circle_diameter * 100:.0f} cm diameter at "
            f"({circle_x_offset * 100:.0f}, {circle_y_offset * 100:.0f}) cm from robot",
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
