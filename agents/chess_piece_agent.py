from typing import List
from brain_client.agent_types import Agent


class ChessPieceAgent(Agent):
    """
    Chess Piece Agent - picks up pieces from the board using calibration data.
    """

    @property
    def id(self) -> str:
        return "chess_piece_agent"

    @property
    def display_name(self) -> str:
        return "Chess Piece Agent"

    def get_skills(self) -> List[str]:
        """Return piece manipulation skills."""
        return [
            "torque_on",
            "pick_up_piece"
        ]

    def get_inputs(self) -> List[str]:
        """Enable microphone input."""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the chess piece manipulation prompt."""
        return """Chess piece handler. Be brief.

When picking up a piece:
1. Call torque_on first (enables arm motors)
2. Call pick_up_piece with square (e.g., square="E4")

Squares use chess notation: A-H (files), 1-8 (ranks).

If calibration missing, tell user to run board calibration first."""
