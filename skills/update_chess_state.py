#!/usr/bin/env python3
"""
Update Chess State Skill - Validates and applies a UCI move to the persisted
board state in ~/chess_game_state.json.

Called by the agent after every move (robot's own move or detected opponent move).
"""

import json
from pathlib import Path

import chess

from brain_client.skill_types import Skill, SkillResult

GAME_STATE_FILE = Path.home() / "chess_game_state.json"


class UpdateChessState(Skill):
    """Validate a UCI move and apply it to the persisted game state."""

    def __init__(self, logger):
        super().__init__(logger)

    @property
    def name(self):
        return "update_chess_state"

    def guidelines(self):
        return (
            "Apply a chess move (UCI notation, e.g. 'e2e4') to the game state. "
            "Validates the move is legal, updates the FEN and move history in "
            "~/chess_game_state.json. Call this after every move — both the "
            "robot's own moves and detected opponent moves."
        )

    def execute(self, move_uci: str):
        """
        Validate and apply a UCI move to the game state.

        Args:
            move_uci: Move in UCI notation (e.g. 'e2e4', 'd7d5', 'e1g1').
        """
        move_uci = move_uci.strip().lower()

        # Load current state
        if not GAME_STATE_FILE.exists():
            return "No game state found. Call reset_chess_game first.", SkillResult.FAILURE
        try:
            state = json.loads(GAME_STATE_FILE.read_text())
        except Exception as e:
            return f"Failed to load game state: {e}", SkillResult.FAILURE

        fen = state.get("fen", chess.STARTING_FEN)
        move_history = list(state.get("move_history", []))
        robot_color = state.get("robot_color", "white")

        # Validate
        board = chess.Board(fen)
        try:
            move = chess.Move.from_uci(move_uci)
        except ValueError:
            return f"Invalid UCI notation: '{move_uci}'", SkillResult.FAILURE

        if move not in board.legal_moves:
            legal = [m.uci() for m in board.legal_moves]
            return (
                f"Move {move_uci} is not legal. Legal moves: {legal}",
                SkillResult.FAILURE,
            )

        # Apply
        san = board.san(move)
        board.push(move)
        new_fen = board.fen()
        move_history.append(move_uci)

        turn = "white" if board.turn == chess.WHITE else "black"
        new_state = {
            "fen": new_fen,
            "move_history": move_history,
            "last_move": move_uci,
            "turn": turn,
            "robot_color": robot_color,
        }

        try:
            GAME_STATE_FILE.write_text(json.dumps(new_state, indent=2))
        except Exception as e:
            return f"Failed to save game state: {e}", SkillResult.FAILURE

        # Check for game-ending conditions
        status = ""
        if board.is_checkmate():
            winner = "Black" if board.turn == chess.WHITE else "White"
            status = f" Checkmate — {winner} wins!"
        elif board.is_stalemate():
            status = " Stalemate — draw!"
        elif board.is_check():
            status = " Check!"

        msg = f"Applied {san} ({move_uci}). Turn: {turn}. FEN: {new_fen}{status}"
        self.logger.info(f"[UpdateChessState] {msg}")
        self._send_feedback(msg)
        return msg, SkillResult.SUCCESS

    def cancel(self):
        return "Update cannot be cancelled"
