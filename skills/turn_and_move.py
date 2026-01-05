#!/usr/bin/env python3
"""
Turn and Move Skill - Basic wheel movement for voice commands.

Supports:
- Turning left/right by angle (degrees)
- Moving forward/backward by distance (meters)
- Moving head up/down to angle (degrees)
"""

import math
import time
from typing import Optional
from brain_client.skill_types import Skill, SkillResult


class TurnAndMove(Skill):
    """
    A simple skill for basic robot movement via voice commands.
    
    Handles turning, moving forward/backward, and head tilt.
    """
    
    # Movement speeds
    LINEAR_SPEED = 0.1  # m/s for forward/backward
    ANGULAR_SPEED = 0.5  # rad/s for turning
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "turn_and_move"
    
    @property
    def metadata(self):
        """Metadata for Gemini function calling."""
        return {
            "description": (
                "Control robot movement. Can turn left/right, move forward/backward, "
                "or tilt head up/down. Only specify ONE action at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["turn_left", "turn_right", "move_forward", "move_backward", "move_head"],
                        "description": "The movement action to perform"
                    },
                    "value": {
                        "type": "number",
                        "description": (
                            "For turn: angle in degrees (default 90). "
                            "For move: distance in meters (default 0.2). "
                            "For head: angle in degrees (-15 down, 0 center, +15 up)."
                        )
                    }
                },
                "required": ["action"]
            }
        }
    
    def guidelines(self):
        return (
            "Use for basic robot movement. Actions: turn_left, turn_right (degrees), "
            "move_forward, move_backward (meters), move_head (degrees: -15 to +15)."
        )
    
    def execute(self, action: str, value: Optional[float] = None):
        """Execute the movement action."""
        self._cancelled = False
        
        self.logger.info(f"🚗 TurnAndMove: action={action}, value={value}")
        
        if action == "turn_left":
            return self._turn(angle_degrees=value or 90, direction="left")
        elif action == "turn_right":
            return self._turn(angle_degrees=value or 90, direction="right")
        elif action == "move_forward":
            return self._move(distance=value or 0.2, direction="forward")
        elif action == "move_backward":
            return self._move(distance=value or 0.2, direction="backward")
        elif action == "move_head":
            return self._move_head(angle=value or 0)
        else:
            return f"Unknown action: {action}", SkillResult.FAILURE
    
    def _turn(self, angle_degrees: float, direction: str):
        """Turn the robot by specified angle."""
        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE
        
        # Convert degrees to radians
        angle_rad = math.radians(abs(angle_degrees))
        
        # Calculate duration based on angle and speed
        duration = angle_rad / self.ANGULAR_SPEED
        
        # Determine angular velocity direction
        angular_z = self.ANGULAR_SPEED if direction == "left" else -self.ANGULAR_SPEED
        
        self.logger.info(f"🔄 Turning {direction} {angle_degrees}° (duration: {duration:.1f}s)")
        
        # Send command with auto-stop
        self.mobility.send_cmd_vel(linear_x=0.0, angular_z=angular_z, duration=duration)
        
        # Wait for completion (with cancellation check)
        start_time = time.time()
        while time.time() - start_time < duration:
            if self._cancelled:
                self.mobility.send_cmd_vel(0, 0)  # Stop immediately
                return "Turn cancelled", SkillResult.CANCELLED
            time.sleep(0.1)
        
        return f"Turned {direction} {angle_degrees}°", SkillResult.SUCCESS
    
    def _move(self, distance: float, direction: str):
        """Move the robot forward or backward."""
        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE
        
        # Calculate duration based on distance and speed
        duration = abs(distance) / self.LINEAR_SPEED
        
        # Determine linear velocity direction
        linear_x = self.LINEAR_SPEED if direction == "forward" else -self.LINEAR_SPEED
        
        self.logger.info(f"➡️ Moving {direction} {distance}m (duration: {duration:.1f}s)")
        
        # Send command with auto-stop
        self.mobility.send_cmd_vel(linear_x=linear_x, angular_z=0.0, duration=duration)
        
        # Wait for completion (with cancellation check)
        start_time = time.time()
        while time.time() - start_time < duration:
            if self._cancelled:
                self.mobility.send_cmd_vel(0, 0)  # Stop immediately
                return "Movement cancelled", SkillResult.CANCELLED
            time.sleep(0.1)
        
        return f"Moved {direction} {distance}m", SkillResult.SUCCESS
    
    def _move_head(self, angle: float):
        """Move the robot's head to specified angle."""
        if self.head is None:
            return "Head interface not available", SkillResult.FAILURE
        
        # Clamp angle to safe range
        angle = max(-25, min(15, angle))
        
        self.logger.info(f"👤 Moving head to {angle}°")
        
        self.head.set_position(int(angle))
        
        # Small delay for head to move
        time.sleep(0.5)
        
        return f"Head moved to {angle}°", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the current movement."""
        self._cancelled = True
        if self.mobility:
            self.mobility.send_cmd_vel(0, 0)  # Stop immediately
        return "Movement cancelled"

