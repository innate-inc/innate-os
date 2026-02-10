#!/usr/bin/env python3
"""
Position Above Square - Move arm above a board square with gripper open.
"""

import json
import time
from pathlib import Path
from brain_client.manipulation_interface import ManipulationInterface
from brain_client.skill_types import Skill, SkillResult, Interface


CALIBRATION_FILE = Path.home() / "board_calibration.json"


class PositionAboveSquare(Skill):
    """Position the arm 5cm above a board square with gripper open."""
    
    manipulation = Interface(ManipulationInterface)
    
    # Fixed orientation - pointing downward
    FIXED_ROLL = 0.0
    FIXED_YAW = 0.0
    FIXED_PITCH = 1.57
    
    HEIGHT = 0.1  # 5cm above board
    
    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
    
    @property
    def name(self):
        return "position_above_square"
    
    def guidelines(self):
        return "Move the arm to 5cm above a board square with gripper open. Requires 'square' parameter (e.g., 'A1', 'E4')."
    
    def _load_calibration(self):
        """Load calibration data from file."""
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except:
            return None
    
    def _square_to_position(self, square: str, calibration: dict) -> tuple[float, float] | None:
        """Convert chess square to XY position using bilinear interpolation."""
        if len(square) != 2:
            return None
        
        file_char = square[0].upper()
        rank_char = square[1]
        
        if file_char not in "ABCDEFGH" or rank_char not in "12345678":
            return None
        
        file_idx = ord(file_char) - ord('A')  # A=0, H=7
        rank_idx = int(rank_char) - 1          # 1=0, 8=7
        
        u = file_idx / 7.0
        v = rank_idx / 7.0
        
        bl = calibration.get("bottom_left", {})
        br = calibration.get("bottom_right", {})
        tl = calibration.get("top_left", {})
        tr = calibration.get("top_right", {})
        
        x = (1-u)*(1-v)*bl.get("x",0) + u*(1-v)*br.get("x",0) + (1-u)*v*tl.get("x",0) + u*v*tr.get("x",0)
        y = (1-u)*(1-v)*bl.get("y",0) + u*(1-v)*br.get("y",0) + (1-u)*v*tl.get("y",0) + u*v*tr.get("y",0)
        
        return (x, y)
    
    def execute(self, square: str = "A1") -> tuple[str, SkillResult]:
        """Position arm above given square with gripper open."""
        self._cancelled = False
        
        calibration = self._load_calibration()
        if not calibration:
            return "Calibration file not found", SkillResult.FAILURE
        
        pos = self._square_to_position(square, calibration)
        if pos is None:
            return f"Invalid square: {square}", SkillResult.FAILURE
        
        x, y = pos
        self.logger.info(f"[PositionAboveSquare] Moving to {square} at x={x:.3f}, y={y:.3f}, z={self.HEIGHT}")
        
        # Open gripper first
        self.manipulation.open_gripper(60)
        time.sleep(0.5)
        
        # Move to position
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=2
        )
        
        if not success:
            return "Failed to move to position", SkillResult.FAILURE
        
        return f"Positioned above {square} at 5cm", SkillResult.SUCCESS
    
    def cancel(self):
        self._cancelled = True
