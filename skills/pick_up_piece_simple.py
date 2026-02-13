#!/usr/bin/env python3
"""
Pick Up Piece Simple Skill - Pick up and place chess pieces using only
calibration data and arm orientation, without Gemini vision or base driving.

Orientation strategy by rank/column:
  Ranks 4-6:  pitch=1.57 (straight down), yaw=0
  Ranks 7-8:  pitch=1.09 (tilted, 1.57-0.48), yaw=0
  Ranks 1-3, cols A-D:  pitch=1.57, yaw=-1.57 (rotated left)
  Ranks 1-3, cols E-H:  pitch=1.57, yaw=+1.57 (rotated right)
"""

import json
import time
from pathlib import Path
from brain_client.skill_types import Skill, SkillResult, Interface, InterfaceType


CALIBRATION_FILE = Path.home() / "board_calibration.json"


class PickUpPieceSimple(Skill):
    """Pick up a chess piece and place it on another square using calibration
    positions only.  No vision correction, no base driving."""

    manipulation = Interface(InterfaceType.MANIPULATION)

    # Orientation constants
    FIXED_ROLL = 0.0
    PITCH_DOWN = 1.57          # straight down
    PITCH_TILTED = 1.57 - 0.48  # tilted for far ranks (7-8)
    YAW_CENTER = 0.0
    YAW_LEFT = -1.57           # rotated left for ranks 1-3, cols A-D
    YAW_RIGHT = 1.57           # rotated right for ranks 1-3, cols E-H

    # Heights in meters
    HEIGHT_SAFE = 0.15          # 20cm safe travel height
    HEIGHT_PICK = 0.08         # 8cm pick height for tall pieces
    HEIGHT_PICK_PAWN = 0.05    # 5cm pick height for pawns

    # Gripper parameters
    GRIPPER_OPEN_PERCENT = 40
    GRIPPER_CLOSE_STRENGTH = 0.4

    # Number of intermediate Z steps for vertical moves
    VERTICAL_STEPS = 4

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._speed = 1.0

    @property
    def name(self):
        return "pick_up_piece_simple"

    def guidelines(self):
        return (
            "Pick up a piece from one square and place it on another without "
            "using Gemini vision or base driving.  Uses arm orientation changes "
            "to reach all ranks.  Parameters: square (source, e.g. 'E2'), "
            "place_square (target, e.g. 'E4'), is_pawn (bool), speed (float)."
        )

    # ── Helpers ───────────────────────────────────────────────────────

    def _load_calibration(self):
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except Exception:
            return None

    def _square_to_position(self, square, calibration):
        """Convert chess notation (e.g. 'E4') to (x, y) robot coordinates."""
        if len(square) != 2:
            return None
        file_char = square[0].upper()
        rank_char = square[1]
        if file_char not in "ABCDEFGH" or rank_char not in "12345678":
            return None

        file_idx = ord(file_char) - ord('A')  # A=0, H=7
        rank_idx = int(rank_char) - 1          # 1=0, 8=7

        u = file_idx / 7.0  # 0 at A, 1 at H
        v = rank_idx / 7.0  # 0 at rank 1, 1 at rank 8

        tl = calibration.get("top_left")
        tr = calibration.get("top_right")
        bl = calibration.get("bottom_left")
        br = calibration.get("bottom_right")
        if not all([tl, tr, bl, br]):
            return None

        x = (1-u)*(1-v)*bl["x"] + u*(1-v)*br["x"] + (1-u)*v*tl["x"] + u*v*tr["x"]
        y = (1-u)*(1-v)*bl["y"] + u*(1-v)*br["y"] + (1-u)*v*tl["y"] + u*v*tr["y"]
        return x, y

    def _orientation_for_square(self, square):
        """Return (pitch, yaw) for reaching a given square.

        Ranks 4-6:  straight down, center yaw
        Ranks 7-8:  tilted pitch, center yaw
        Ranks 1-3:  straight down, yaw left (A-D) or right (E-H)
        """
        rank = int(square[1])
        col = square[0].upper()

        if rank >= 7:
            return self.PITCH_TILTED, self.YAW_CENTER
        elif rank >= 4:
            return self.PITCH_DOWN, self.YAW_CENTER
        else:  # ranks 1-3
            if col in "ABCD":
                return self.PITCH_DOWN, self.YAW_LEFT
            else:
                return self.PITCH_DOWN, self.YAW_RIGHT

    def _d(self, seconds: float) -> float:
        """Scale a duration by the speed factor."""
        return seconds / self._speed

    def _w(self, seconds: float):
        """Sleep for a scaled duration."""
        time.sleep(seconds / self._speed)

    def _move_arm(self, x, y, z, pitch, yaw, duration, wait=None, gripper_position=None):
        """Move arm to pose and optionally wait. Returns True on success."""
        kwargs = dict(x=x, y=y, z=z, roll=self.FIXED_ROLL, pitch=pitch, yaw=yaw,
                      duration=self._d(duration))
        if gripper_position is not None:
            kwargs['gripper_position'] = gripper_position
        success = self.manipulation.move_to_cartesian_pose(**kwargs)
        if success and wait is not None:
            self._w(wait)
        return success

    def _vertical_move(self, x, y, from_z, to_z, pitch, yaw, gripper_position=None):
        """Move vertically in VERTICAL_STEPS increments with fixed X, Y.

        This ensures the IK solver follows a straight vertical path without
        drifting in X/Y.  Returns error string or None.
        """
        direction = "Descending" if to_z < from_z else "Lifting"
        n = self.VERTICAL_STEPS
        for i in range(1, n + 1):
            frac = i / n
            z = from_z + (to_z - from_z) * frac
            self.logger.info(f"[PickUpPieceSimple] {direction} step {i}/{n} -> z={z:.3f}m")
            if not self._move_arm(x, y, z, pitch, yaw, 1.0, wait=1.0,
                                  gripper_position=gripper_position):
                return f"Failed at {direction.lower()} step {i}/{n} z={z:.3f}m"
            if self._cancelled:
                return "Cancelled"
        return None

    def _go_to_safe_pose(self, pitch, yaw):
        """Return arm to the resting safe pose."""
        self._move_arm(0.15, 0.1, 0.1, pitch, yaw, 2, wait=2.0)

    # ── Main logic ────────────────────────────────────────────────────

    def execute(self, square: str, place_square: str, is_pawn: bool = True, speed: float = 1.0):
        """
        Pick up a piece from square and place it on place_square.

        Args:
            square: Source square in chess notation (e.g. 'A4')
            place_square: Target square (e.g. 'D5')
            is_pawn: If True, use lower pick height for pawns
            speed: Speed multiplier (1.0 = normal)
        """
        self._speed = max(0.1, speed)
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        calibration = self._load_calibration()
        if calibration is None:
            return "No calibration data found. Run board calibration first.", SkillResult.FAILURE

        src_pos = self._square_to_position(square, calibration)
        if src_pos is None:
            return f"Invalid source square '{square}'", SkillResult.FAILURE
        dst_pos = self._square_to_position(place_square, calibration)
        if dst_pos is None:
            return f"Invalid target square '{place_square}'", SkillResult.FAILURE

        src_x, src_y = src_pos
        dst_x, dst_y = dst_pos
        pick_height = self.HEIGHT_PICK_PAWN if is_pawn else self.HEIGHT_PICK
        src_pitch, src_yaw = self._orientation_for_square(square)
        dst_pitch, dst_yaw = self._orientation_for_square(place_square)

        self.logger.info(
            f"[PickUpPieceSimple] Pick {square} ({src_x:.4f},{src_y:.4f}) "
            f"pitch={src_pitch:.2f} yaw={src_yaw:.2f} -> "
            f"Place {place_square} ({dst_x:.4f},{dst_y:.4f}) "
            f"pitch={dst_pitch:.2f} yaw={dst_yaw:.2f}"
        )

        # ── 1. Move above source square at safe height ──

        self._send_feedback(f"Moving above {square}...")
        if not self._move_arm(src_x, src_y, self.HEIGHT_SAFE, src_pitch, src_yaw, 2, wait=2.5):
            return f"Failed to move above {square}", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # ── 2. Open gripper ──

        self._send_feedback("Opening gripper...")
        self.manipulation.open_gripper(self.GRIPPER_OPEN_PERCENT)
        self._w(1.5)

        # ── 3. Descend to pick height (4 steps) ──

        self._send_feedback(f"Descending to pick {square}...")
        err = self._vertical_move(src_x, src_y, self.HEIGHT_SAFE, pick_height,
                                  src_pitch, src_yaw)
        if err:
            return f"Pick descent failed: {err}", SkillResult.FAILURE

        # ── 4. Grab ──

        self._send_feedback("Grabbing piece...")
        self.manipulation.close_gripper(strength=self.GRIPPER_CLOSE_STRENGTH, blocking=True)
        self._w(2.0)
        grip_position = self.manipulation.GRIPPER_CLOSED - self.GRIPPER_CLOSE_STRENGTH

        # ── 5. Lift to safe height (4 steps) ──

        self._send_feedback("Lifting piece...")
        err = self._vertical_move(src_x, src_y, pick_height, self.HEIGHT_SAFE,
                                  src_pitch, src_yaw, gripper_position=grip_position)
        if err:
            return f"Lift failed: {err}", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # ── 6. Move above target square at safe height ──

        self._send_feedback(f"Moving above {place_square}...")
        if not self._move_arm(dst_x, dst_y, self.HEIGHT_SAFE, dst_pitch, dst_yaw, 2, wait=2.5,
                              gripper_position=grip_position):
            return f"Failed to move above {place_square}", SkillResult.FAILURE
        if self._cancelled:
            return "Cancelled", SkillResult.CANCELLED

        # ── 7. Descend to place height (4 steps) ──

        self._send_feedback(f"Descending to place on {place_square}...")
        err = self._vertical_move(dst_x, dst_y, self.HEIGHT_SAFE, pick_height,
                                  dst_pitch, dst_yaw, gripper_position=grip_position)
        if err:
            return f"Place descent failed: {err}", SkillResult.FAILURE

        # ── 8. Release ──

        self._send_feedback("Releasing piece...")
        self.manipulation.open_gripper(self.GRIPPER_OPEN_PERCENT)
        self._w(1.5)

        # ── 9. Lift to safe height (4 steps) ──

        self._send_feedback("Lifting after place...")
        err = self._vertical_move(dst_x, dst_y, pick_height, self.HEIGHT_SAFE,
                                  dst_pitch, dst_yaw)
        if err:
            return f"Post-place lift failed: {err}", SkillResult.FAILURE

        # ── 10. Return to safe pose ──

        self._send_feedback("Returning to safe pose...")
        self._go_to_safe_pose(self.PITCH_DOWN, self.YAW_CENTER)

        msg = f"Moved piece from {square} to {place_square}"
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Pick up piece simple cancelled"
