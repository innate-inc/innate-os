#!/usr/bin/env python3
"""
Record Position Skill - Record current arm FK position, save to file, and send as feedback.
"""

import base64
import json
from datetime import datetime
from pathlib import Path

from brain_client.skill_types import Interface, InterfaceType, RobotState, RobotStateType, Skill, SkillResult

CALIBRATION_FILE = Path.home() / "board_calibration.json"
CORNER_CAPTURES_DIR = Path("/home/jetson1/innate-os/captures/corners")


class RecordPosition(Skill):
    """Record current arm position, save to calibration file, and send as feedback."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)

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
            except Exception:
                calibration = {}

        # Save corner position
        calibration[corner] = {"x": pos["x"], "y": pos["y"], "z": pos["z"]}
        CALIBRATION_FILE.write_text(json.dumps(calibration, indent=2))

        self._save_corner_image(corner, pos)

        position_str = f"X={pos['x']:.4f}, Y={pos['y']:.4f}, Z={pos['z']:.4f}"
        self._send_feedback(f"RECORDED {corner.upper()}: {position_str}")
        self.logger.info(f"Saved {corner} to {CALIBRATION_FILE}")

        return f"{corner} recorded: {position_str}", SkillResult.SUCCESS

    def _save_corner_image(self, corner: str, pos: dict):
        """Save the latest wrist camera frame as a corner snapshot."""
        if not self.image:
            self.logger.warning("No wrist camera image available to save")
            return
        try:
            CORNER_CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = CORNER_CAPTURES_DIR / f"corner_{corner}_{ts}.jpg"
            path.write_bytes(base64.b64decode(self.image))
            self.logger.info(f"Corner image saved: {path}")
        except Exception as e:
            self.logger.warning(f"Failed to save corner image: {e}")

    def cancel(self):
        """Nothing to cancel."""
        return "Record position cannot be cancelled"
