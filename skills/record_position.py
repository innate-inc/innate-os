#!/usr/bin/env python3
"""
Record Position Skill - Record current arm FK position, save to file, and send as feedback.
"""

import json
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


CALIBRATION_FILE = Path.home() / "board_calibration.json"


class RecordPosition(Skill):
    """Record current arm position, save to calibration file, and send as feedback."""
    
    manipulation = Interface(InterfaceType.MANIPULATION)
    
    def __init__(self, logger):
        super().__init__(logger)
    
    @property
    def name(self):
        return "record_position"
    
    def guidelines(self):
        return (
            "Record the current arm position for a board corner. "
            "Requires 'corner' parameter: 'top_left', 'top_right', 'bottom_right', or 'bottom_left'. "
            "Saves to calibration file and returns coordinates."
        )
    
    def execute(self, corner: str):
        """
        Record and save current FK position for a corner.
        
        Args:
            corner: One of 'top_left', 'top_right', 'bottom_right', 'bottom_left'
        """
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE
        
        valid_corners = ["top_left", "top_right", "bottom_right", "bottom_left"]
        corner = corner.lower().replace("-", "_").replace(" ", "_")
        if corner not in valid_corners:
            return f"Invalid corner '{corner}'. Must be one of: {valid_corners}", SkillResult.FAILURE
        
        fk_pose = self.manipulation.get_current_end_effector_pose()
        
        if not fk_pose:
            return "Could not get current position", SkillResult.FAILURE
        
        pos = fk_pose["position"]
        
        # Load existing calibration or create new
        calibration = {}
        if CALIBRATION_FILE.exists():
            try:
                calibration = json.loads(CALIBRATION_FILE.read_text())
            except:
                calibration = {}
        
        # Save corner position
        calibration[corner] = {"x": pos["x"], "y": pos["y"], "z": pos["z"]}
        CALIBRATION_FILE.write_text(json.dumps(calibration, indent=2))
        
        position_str = f"X={pos['x']:.4f}, Y={pos['y']:.4f}, Z={pos['z']:.4f}"
        self._send_feedback(f"RECORDED {corner.upper()}: {position_str}")
        self.logger.info(f"Saved {corner} to {CALIBRATION_FILE}")
        
        return f"{corner} recorded: {position_str}", SkillResult.SUCCESS
    
    def cancel(self):
        """Nothing to cancel."""
        return "Record position cannot be cancelled"
