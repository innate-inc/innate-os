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
    HEIGHT_SAFE = 0.25   # 20cm safe travel height (won't knock pieces)
    HEIGHT_ABOVE = 0.18  # 10cm above board for positioning
    HEIGHT_PICK = 0.1   # 4cm above ground for picking
    
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
        
        Coordinate system (from robot's perspective):
        - X positive = forward (away from robot)
        - Y positive = left, Y negative = right
        
        Board layout:
        - top_left: high X, high Y (forward-left) = A8
        - top_right: high X, low Y (forward-right) = H8
        - bottom_left: low X, high Y (close-left) = A1
        - bottom_right: low X, low Y (close-right) = H1
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
        # Mapping: u=0 is file A (left), u=1 is file H (right)
        #          v=0 is rank 1 (bottom/close), v=1 is rank 8 (top/far)
        # So: A1=(u=0,v=0)=bottom_left, H1=(u=1,v=0)=bottom_right
        #     A8=(u=0,v=1)=top_left, H8=(u=1,v=1)=top_right
        
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
        self.logger.info(f"[PickUpPiece] Target {square} at X={x:.4f}, Y={y:.4f}")
        self._send_feedback(f"Moving to {square} at X={x:.4f}, Y={y:.4f}")
        
        # Step 1: Move to safe height (20cm) at current position first
        self.logger.info(f"[PickUpPiece] Step 1: Moving to safe height {self.HEIGHT_SAFE}m")
        self._send_feedback("Moving to safe height...")
        current_pose = self.manipulation.get_current_end_effector_pose()
        if current_pose:
            curr_x, curr_y = current_pose["position"]["x"], current_pose["position"]["y"]
            success = self.manipulation.move_to_cartesian_pose(
                x=curr_x, y=curr_y, z=self.HEIGHT_SAFE,
                roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
                duration=1
            )
            if not success:
                return "Failed to move to safe height", SkillResult.FAILURE
            time.sleep(1.5)
        
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED
        
        # Step 2: Move horizontally to above target square at safe height
        self.logger.info(f"[PickUpPiece] Step 2: Moving horizontally to X={x:.4f}, Y={y:.4f}, Z={self.HEIGHT_SAFE}")
        self._send_feedback(f"Moving above {square}...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_SAFE,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=2
        )
        if not success:
            return "Failed to move above square", SkillResult.FAILURE
        time.sleep(2.5)
        
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED
        
        # Step 3: Open gripper while above the piece
        self.logger.info("[PickUpPiece] Step 3: Opening gripper")
        self._send_feedback("Opening gripper...")
        self.manipulation.open_gripper(60)
        time.sleep(0.7)
        
        # Step 4: Descend to picking height (4cm)
        self.logger.info(f"[PickUpPiece] Step 4: Descending to pick height {self.HEIGHT_PICK}m")
        self._send_feedback("Descending to pick...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_PICK,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=2.5
        )
        if not success:
            return "Failed to descend to pick height", SkillResult.FAILURE
        time.sleep(1.5)
        
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED
        
        # Step 5: Close gripper to grab piece
        self.logger.info("[PickUpPiece] Step 5: Closing gripper")
        self._send_feedback("Grabbing piece...")
        self.manipulation.close_gripper()
        time.sleep(2.0)
        
        # Step 6: Lift back to safe height
        self.logger.info(f"[PickUpPiece] Step 6: Lifting to safe height {self.HEIGHT_SAFE}m")
        self._send_feedback("Lifting piece...")
        success = self.manipulation.move_to_cartesian_pose(
            x=x, y=y, z=self.HEIGHT_SAFE,
            roll=self.FIXED_ROLL, pitch=self.FIXED_PITCH, yaw=self.FIXED_YAW,
            duration=2
        )
        if not success:
            return "Failed to lift", SkillResult.FAILURE
        time.sleep(2.0)
        
        self.logger.info(f"[PickUpPiece] Complete: Picked up piece from {square}")
        self._send_feedback(f"Piece picked up from {square}")
        return f"Picked up piece from {square}", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the pick up operation."""
        self._cancelled = True
        return "Pick up cancelled"
