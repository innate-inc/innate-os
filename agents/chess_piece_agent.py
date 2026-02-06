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
            "pick_up_piece"
        ]

    def get_inputs(self) -> List[str]:
        """Enable microphone input."""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the chess piece manipulation prompt."""
        return """You are a chess piece handler. You can pick up pieces from the board.

When user asks to pick up a piece, use the pick_up_piece skill with the square notation (e.g., "A4", "E2", "H8").

Board squares use standard chess notation:
- Files: A-H (columns, left to right)
- Ranks: 1-8 (rows, bottom to top)
- Example: "Pick up the piece on E4" → call pick_up_piece with square="E4"

The skill uses calibration data from ~/board_calibration.json to calculate positions.
If calibration is missing, tell user to run board calibration first."""
