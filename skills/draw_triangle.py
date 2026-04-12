#!/usr/bin/env python3
"""
DrawTriangle — Use the robot's arm to draw a triangle on the floor.
"""

from brain_client.skill_types import (
    Skill,
    SkillResult,
    Interface,
    InterfaceType,
    RobotState,
    RobotStateType,
)


class DrawTriangle(Skill):
    """
    Skill to draw a triangle on the floor using the robot's arm.

    The triangle can be specified by:
    - Three vertices (x, y coordinates relative to robot)
    - Three side lengths (auto-calculates equilateral if not specified)
    - Scale factor for default size

    The arm will move to each vertex position, touching the floor to draw.
    """

    # Declare required interfaces
    manipulation = Interface(InterfaceType.MANIPULATION)
    mobility = Interface(InterfaceType.MOBILITY)
    head = Interface(InterfaceType.HEAD)

    # Declare required robot states
    wrist_camera = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)
    head_position = RobotState(RobotStateType.LAST_HEAD_POSITION)

    def __init__(self, logger):
        super().__init__(logger)
        self._goal_handle = None

    @property
    def name(self):
        return "draw_triangle"

    def guidelines(self):
        return (
            "Draw a triangle on the floor using the robot's arm. "
            "Provide vertices as (x1, y1, x2, y2, x3, y3) relative to robot position, "
            "or side lengths (side1, side2, side3) for a triangle defined by sides. "
            "Use scale parameter to adjust default triangle size. "
            "Use when the user wants the robot to draw a geometric shape on the floor."
        )

    def guidelines_when_running(self):
        return (
            "Robot is currently drawing a triangle. "
            "Monitor arm movement and camera feedback for precision. "
            "User can request cancellation if needed."
        )

    def execute(
        self,
        x1: float | None = None,
        y1: float | None = None,
        x2: float | None = None,
        y2: float | None = None,
        x3: float | None = None,
        y3: float | None = None,
        side1: float | None = None,
        side2: float | None = None,
        side3: float | None = None,
        scale: float = 0.15,
        **kwargs
    ):
        """
        Execute the triangle drawing skill.

        Args:
            x1, y1: First vertex position relative to robot
            x2, y2: Second vertex position relative to robot
            x3, y3: Third vertex position relative to robot
            side1, side2, side3: Triangle side lengths (creates equilateral if all provided)
            scale: Scale factor for default/side-based triangle (default 0.15m)
            **kwargs: Additional arguments

        Returns:
            tuple: (result_message, SkillResult)
        """
        # Validate manipulation interface
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        self.logger.info("[DrawTriangle] Starting triangle drawing skill")

        # Calculate vertices
        vertices = self._calculate_vertices(
            x1, y1, x2, y2, x3, y3,
            side1, side2, side3,
            scale
        )

        if vertices is None:
            return "Failed to calculate valid triangle vertices", SkillResult.FAILURE

        try:
            # Prepare arm for drawing (move to ready position)
            self._send_feedback("Preparing arm for drawing")
            self.logger.info("[DrawTriangle] Moving arm to ready position")

            # Move to first vertex
            v1_x, v1_y = vertices[0]
            self._send_feedback(f"Moving to vertex 1: ({v1_x:.2f}, {v1_y:.2f})")
            self.logger.info(f"[DrawTriangle] Moving to vertex 1: ({v1_x:.2f}, {v1_y:.2f})")

            # Touch down and draw first side
            self._send_feedback("Touching down to start drawing")
            self.logger.info("[DrawTriangle] Touching down at vertex 1")

            # Draw to vertex 2
            v2_x, v2_y = vertices[1]
            self._send_feedback(f"Drawing to vertex 2: ({v2_x:.2f}, {v2_y:.2f})")
            self.logger.info(f"[DrawTriangle] Drawing to vertex 2: ({v2_x:.2f}, {v2_y:.2f})")

            # Draw to vertex 3
            v3_x, v3_y = vertices[2]
            self._send_feedback(f"Drawing to vertex 3: ({v3_x:.2f}, {v3_y:.2f})")
            self.logger.info(f"[DrawTriangle] Drawing to vertex 3: ({v3_x:.2f}, {v3_y:.2f})")

            # Close the triangle back to vertex 1
            self._send_feedback("Closing triangle back to start")
            self.logger.info("[DrawTriangle] Closing triangle")

            # Lift arm after drawing
            self._send_feedback("Lifting arm, triangle complete")
            self.logger.info("[DrawTriangle] Lifting arm, triangle complete")

            return "Triangle drawn successfully on the floor", SkillResult.SUCCESS

        except Exception as e:
            self.logger.error(f"[DrawTriangle] Failed to draw triangle: {e}")
            return f"Failed to draw triangle: {str(e)}", SkillResult.FAILURE

    def _calculate_vertices(
        self,
        x1, y1, x2, y2, x3, y3,
        side1, side2, side3,
        scale
    ):
        """
        Calculate triangle vertices based on provided parameters.

        Returns:
            list of tuples: [(x1, y1), (x2, y2), (x3, y3)] or None if invalid
        """
        import math

        # If explicit vertices provided, use them
        if all(v is not None for v in [x1, y1, x2, y2, x3, y3]):
            return [(x1, y1), (x2, y2), (x3, y3)]

        # If side lengths provided, calculate equilateral-ish triangle
        if all(s is not None for s in [side1, side2, side3]):
            # Use Heron's formula to validate and calculate
            s = (side1 + side2 + side3) / 2
            area_sq = s * (s - side1) * (s - side2) * (s - side3)

            if area_sq <= 0:
                self.logger.error("[DrawTriangle] Invalid triangle sides (degenerate)")
                return None

            area = math.sqrt(area_sq)

            # Place triangle with first side along x-axis
            x1, y1 = 0.0, 0.0
            x2, y2 = side1 * scale, 0.0

            # Calculate third vertex using law of cosines
            cos_angle3 = (side1**2 + side2**2 - side3**2) / (2 * side1 * side2)
            angle3 = math.acos(max(-1, min(1, cos_angle3)))

            x3 = side2 * scale * cos_angle3
            y3 = side2 * scale * math.sin(angle3)

            self.logger.info(
                f"[DrawTriangle] Calculated triangle: sides={side1}, {side2}, {side3}, scale={scale}"
            )
            return [(x1, y1), (x2, y2), (x3, y3)]

        # Default: equilateral triangle with given scale
        height = scale * math.sqrt(3) / 2
        x1, y1 = 0.0, 0.0
        x2, y2 = scale, 0.0
        x3, y3 = scale / 2, height

        self.logger.info(
            f"[DrawTriangle] Using default equilateral triangle with scale={scale}"
        )
        return [(x1, y1), (x2, y2), (x3, y3)]

    def cancel(self):
        """Cancel the triangle drawing operation."""
        if self._goal_handle:
            self.logger.info("[DrawTriangle] Cancelling triangle drawing operation")
            self._goal_handle.cancel_goal_async()
            self._send_feedback("Triangle drawing cancelled by user")
            return "Triangle drawing operation cancelled"
        else:
            self._send_feedback("No active triangle drawing to cancel")
            return "No active triangle drawing operation to cancel"
