#!/usr/bin/env python3
"""
Reset Chess Game Skill - Resets the board state to the starting position.
"""

import json
from pathlib import Path

import chess

from brain_client.skill_types import Skill, SkillResult


GAME_STATE_FILE = Path.home() / "chess_game_state.json"
CALIBRATION_FILE = Path.home() / "board_calibration.json"
REQUIRED_CORNERS = ("top_left", "top_right", "bottom_right", "bottom_left")

# Handicap: White starts without the a1 rook (no queenside castling)
HANDICAP_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/1NBQKBNR w Kkq - 0 1"


class ResetChessGame(Skill):
    """Reset the chess game state to the standard starting position."""

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "reset_chess_game"

    def guidelines(self):
        return (
            "Reset the chess game to the starting position. "
            "Requires board calibration to be present first. "
            "Clears the move history and sets the board to the initial FEN. "
            "Optionally pass robot_color ('white' or 'black') to set which side the robot plays."
        )

    def _is_calibrated(self) -> bool:
        """Return True when board calibration JSON exists and has all four corners."""
        if not CALIBRATION_FILE.exists():
            return False

        try:
            calibration = json.loads(CALIBRATION_FILE.read_text())
        except Exception:
            return False

        if not isinstance(calibration, dict):
            return False

        for corner in REQUIRED_CORNERS:
            pos = calibration.get(corner)
            if not isinstance(pos, dict):
                return False
            try:
                float(pos["x"])
                float(pos["y"])
                float(pos["z"])
            except Exception:
                return False

        return True

    def execute(self, robot_color: str = "white"):
        """
        Reset game state to starting position.

        Args:
            robot_color: Which side the robot plays ('white' or 'black').
        """
        robot_color = robot_color.strip().lower()
        if robot_color not in ("white", "black"):
            return f"Invalid robot_color '{robot_color}'. Must be 'white' or 'black'.", SkillResult.FAILURE
        if not self._is_calibrated():
            msg = (
                "Board is not calibrated. Run board calibration first, then reset the chess game."
            )
            self.logger.warning(f"[ResetChessGame] {msg}")
            self._send_feedback(msg)
            return msg, SkillResult.FAILURE

        state = {
            "fen": HANDICAP_FEN,
            "move_history": [],
            "last_detected_move": None,
            "turn": "white",
            "robot_color": robot_color,
        }

        try:
            GAME_STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            return f"Failed to write game state: {e}", SkillResult.FAILURE

        msg = f"Game reset to starting position. Robot plays {robot_color}."
        self.logger.info(f"[ResetChessGame] {msg}")
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def cancel(self):
        return "Reset cannot be cancelled"
