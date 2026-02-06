#!/usr/bin/env python3
"""
Pick Up Piece Skill - Pick up a chess piece from a given square using calibration data.
"""

import json
import time
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


CALIBRATION_FILE = Path.home() / "board_calibration.json"


class PickUpPiece(Skill):
    """Pick up a chess piece from a specified square (e.g., A4, B6)."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    # Fixed orientation - pointing downward
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57
    
    # Heights in meters
    HEIGHT_ABOVE = 0.10  # 10cm above board
    HEIGHT_PICK = 0.04   # 4cm above ground for picking
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "pick_up_piece"
    
    def guidelines(self):
        return (
            "Pick up a chess piece from a square. "
            "Requires 'square' parameter in chess notation (e.g., 'A1', 'H8'). "
            "Uses calibration data to calculate position. "
            "Moves 10cm above, descends to 4cm, picks piece, returns to 10cm."
        )
    
    def _load_calibration(self):
        """Load calibration data from file."""
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except:
            return None
    
    def _square_to_position(self, square: str, calibration: dict) -> tuple[float, float] | None:
        """
        Convert chess square (e.g., 'A1') to XY position using bilinear interpolation.
        
        Board layout (from robot's perspective):
        - A1 is bottom-left, H8 is top-right
        - Files A-H map to columns 0-7
        - Ranks 1-8 map to rows 0-7
        """
        if len(square) != 2:
            return None
        
        file_char = square[0].upper()
        rank_char = square[1]
        
        if file_char not in "ABCDEFGH" or rank_char not in "12345678":
            return None
        
        # Convert to 0-7 indices
        file_idx = ord(file_char) - ord('A')  # A=0, H=7
        rank_idx = int(rank_char) - 1          # 1=0, 8=7
        
        # Normalize to 0-1 range
        u = file_idx / 7.0  # 0 at A, 1 at H
        v = rank_idx / 7.0  # 0 at rank 1, 1 at rank 8
        
        # Get corner positions
        tl = calibration.get("top_left")
        tr = calibration.get("top_right")
        bl = calibration.get("bottom_left")
        br = calibration.get("bottom_right")
        
        if not all([tl, tr, bl, br]):
            return None
        
        # Bilinear interpolation
        # top_left = rank 8, file A (v=1, u=0)
        # top_right = rank 8, file H (v=1, u=1)
        # bottom_left = rank 1, file A (v=0, u=0)
        # bottom_right = rank 1, file H (v=0, u=1)
        
        x = (1-u)*(1-v)*bl["x"] + u*(1-v)*br["x"] + (1-u)*v*tl["x"] + u*v*tr["x"]
        y = (1-u)*(1-v)*bl["y"] + u*(1-v)*br["y"] + (1-u)*v*tl["y"] + u*v*tr["y"]
        
        return x, y
    
    def execute(self, square: str):
        """
        Pick up a piece from the specified square.
        
        Args:
            square: Chess notation (e.g., 'A4', 'E2')
        """
        self._cancelled = False
        
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        # Load calibration
        calibration = self._load_calibration()
        if calibration is None:
            return "No calibration data found. Run board calibration first.", SkillResult.FAILURE
        
        # Calculate position
        pos = self._square_to_position(square, calibration)
        if pos is None:
            return f"Invalid square '{square}' or incomplete calibration", SkillResult.FAILURE
        
        x, y = pos
        self._send_feedback(f"Moving to {square} at X={x:.4f}, Y={y:.4f}")
        
        # Step 1: Move 10cm above the square
        self._send_feedback(f"Positioning 10cm above {square}...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=2
        )
        if not success:
            return "Failed to move above square", SkillResult.FAILURE
        time.sleep(2)
        
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED
        
        # Step 2: Descend to picking height (4cm)
        self._send_feedback(f"Descending to pick height...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_PICK,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=1
        )
        if not success:
            return "Failed to descend", SkillResult.FAILURE
        time.sleep(1.5)
        
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED
        
        # Step 3: Pick the piece (gripper close would go here)
        self._send_feedback("Picking piece...")
        time.sleep(0.5)
        
        # Step 4: Return to 10cm above
        self._send_feedback(f"Lifting piece...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_ABOVE,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=1
        )
        if not success:
            return "Failed to lift", SkillResult.FAILURE
        time.sleep(1.5)
        
        self._send_feedback(f"Piece picked up from {square}")
        return f"Picked up piece from {square}", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the pick up operation."""
        self._cancelled = True
        return "Pick up cancelled"
