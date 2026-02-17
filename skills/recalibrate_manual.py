#!/usr/bin/env python3
"""
Recalibrate Manual Skill - Human positions the arm above a top corner square
(A8 or H8), records the position, and recomputes the full board calibration
using square geometry with the other top corner held fixed.
"""

import base64
import json
import math
from datetime import datetime
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType, RobotState, RobotStateType


CALIBRATION_FILE = Path.home() / "board_calibration.json"
CAPTURES_DIR = Path.home() / "innate-os/captures/corners"


class RecalibrateManual(Skill):
    """Record the arm's current position as a top corner (A8 or H8),
    keep the other top corner from existing calibration, and recompute
    all four corners using square geometry."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    image = RobotState(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "recalibrate_manual"

    def guidelines(self):
        return (
            "Manually recalibrate one top corner of the chessboard. "
            "The human positions the arm above the center of A8 or H8, "
            "then this skill records the position and recomputes the full "
            "board calibration using square geometry. "
            "Requires 'corner' parameter: 'A8' or 'H8'."
        )

    def _load_calibration(self):
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except Exception:
            return None

    def _recompute_calibration_from_top_corners(self, new_a8, new_h8, z):
        """Given A8 (top_left) and H8 (top_right) center positions,
        derive A1 (bottom_left) and H1 (bottom_right) using square geometry.

        The board is a square: the bottom edge is obtained by rotating the
        top edge (A8->H8) 90 deg clockwise (toward the robot, i.e. -X direction).
        """
        # Top side vector: A8 -> H8
        side_x = new_h8[0] - new_a8[0]
        side_y = new_h8[1] - new_a8[1]

        # Perpendicular vector pointing toward bottom of board (-X direction)
        # Rotate (side_x, side_y) by -90 deg -> (side_y, -side_x)
        down_x = side_y
        down_y = -side_x

        # Bottom corners
        a1_x = new_a8[0] + down_x
        a1_y = new_a8[1] + down_y
        h1_x = new_h8[0] + down_x
        h1_y = new_h8[1] + down_y

        updated = {
            "top_left":     {"x": new_a8[0], "y": new_a8[1], "z": z},
            "top_right":    {"x": new_h8[0], "y": new_h8[1], "z": z},
            "bottom_left":  {"x": a1_x, "y": a1_y, "z": z},
            "bottom_right": {"x": h1_x, "y": h1_y, "z": z},
        }

        side_len = math.sqrt(side_x**2 + side_y**2)
        self.logger.info(
            f"[RecalibrateManual] Square geometry: side={side_len*100:.1f}cm "
            f"A8=({new_a8[0]:.4f},{new_a8[1]:.4f}) H8=({new_h8[0]:.4f},{new_h8[1]:.4f}) "
            f"A1=({a1_x:.4f},{a1_y:.4f}) H1=({h1_x:.4f},{h1_y:.4f})"
        )
        return updated

    def _save_corner_image(self, corner: str, pos: dict):
        """Save the latest wrist camera frame as a corner snapshot."""
        if not self.image:
            self.logger.warning("[RecalibrateManual] No wrist camera image available to save")
            return
        try:
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = CAPTURES_DIR / f"manual_{corner}_{ts}.jpg"
            path.write_bytes(base64.b64decode(self.image))
            self.logger.info(f"[RecalibrateManual] Corner image saved: {path}")
        except Exception as e:
            self.logger.warning(f"[RecalibrateManual] Failed to save corner image: {e}")

    def execute(self, corner: str):
        """
        Record current arm FK position as a top corner and recompute full board.

        Args:
            corner: 'A8' (top_left) or 'H8' (top_right)
        """
        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        corner = corner.upper().strip()
        if corner not in ("A8", "H8"):
            return f"Invalid corner '{corner}'. Must be 'A8' or 'H8'.", SkillResult.FAILURE

        calibration = self._load_calibration()
        if calibration is None:
            calibration = {}

        # Need the other top corner to exist
        if corner == "A8":
            this_key = "top_left"
            other_key = "top_right"
            other_name = "H8"
        else:
            this_key = "top_right"
            other_key = "top_left"
            other_name = "A8"

        # Read current arm position
        fk_pose = self.manipulation.get_current_end_effector_pose()
        if not fk_pose:
            return "Could not get current arm position", SkillResult.FAILURE

        pos = fk_pose["position"]
        new_pos = (pos["x"], pos["y"])
        z = pos["z"]

        position_str = f"X={pos['x']:.4f}, Y={pos['y']:.4f}, Z={pos['z']:.4f}"
        self.logger.info(f"[RecalibrateManual] Recording {corner} at {position_str}")
        self._send_feedback(f"Recording {corner} at {position_str}")

        # Save snapshot
        self._save_corner_image(corner, pos)

        # If the other top corner is missing, save only this corner
        if other_key not in calibration:
            calibration[this_key] = {"x": pos["x"], "y": pos["y"], "z": z}
            try:
                CALIBRATION_FILE.write_text(json.dumps(calibration, indent=2))
                self.logger.info(f"[RecalibrateManual] Calibration saved to {CALIBRATION_FILE}")
            except Exception as e:
                return f"Failed to save calibration: {e}", SkillResult.FAILURE

            msg = (
                f"Recorded {corner} at {position_str}. "
                f"Other corner {other_name} not yet recorded — record it to compute full board."
            )
            self.logger.info(f"[RecalibrateManual] {msg}")
            self._send_feedback(msg)
            return msg, SkillResult.SUCCESS

        # Build the two top corners
        other = calibration[other_key]
        other_pos = (other["x"], other["y"])

        if corner == "A8":
            new_a8 = new_pos
            new_h8 = other_pos
        else:
            new_a8 = other_pos
            new_h8 = new_pos

        # Use z from existing calibration if available, else from current pose
        cal_z = calibration.get("top_right", calibration.get("top_left", {})).get("z", z)

        # Recompute full board
        updated = self._recompute_calibration_from_top_corners(new_a8, new_h8, cal_z)

        try:
            CALIBRATION_FILE.write_text(json.dumps(updated, indent=2))
            self.logger.info(f"[RecalibrateManual] Calibration saved to {CALIBRATION_FILE}")
        except Exception as e:
            return f"Failed to save calibration: {e}", SkillResult.FAILURE

        # Report
        side_x = new_h8[0] - new_a8[0]
        side_y = new_h8[1] - new_a8[1]
        side_len = math.sqrt(side_x**2 + side_y**2)

        msg = (
            f"Recorded {corner} at {position_str}. "
            f"Recomputed full board from {corner} (new) + {other_name} (fixed). "
            f"Board side={side_len*100:.1f}cm."
        )
        self.logger.info(f"[RecalibrateManual] {msg}")
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def cancel(self):
        return "Recalibrate manual cannot be cancelled"
