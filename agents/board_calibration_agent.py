from brain_client.agent_types import Agent


class BoardCalibrationAgent(Agent):
    """
    Board Calibration Agent - guides user through 4-corner calibration.
    """

    @property
    def id(self) -> str:
        return "board_calibration_agent"

    @property
    def display_name(self) -> str:
        return "Board Calibration Agent"

    def get_skills(self) -> list[str]:
        """Return position recording skill."""
        return ["record_position"]

    def get_inputs(self) -> list[str]:
        """Enable microphone input."""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the calibration workflow prompt."""
        return """Calibration assistant. Be brief and direct - no unnecessary words.

Order: TOP-LEFT → TOP-RIGHT → BOTTOM-RIGHT → BOTTOM-LEFT

For each corner:
1. Say: "Move arm to [corner]. Say ready."
2. On ready: record_position(corner="...")
3. Move to next corner

Example responses:
- "Move arm to top-left. Say ready."
- "Recorded. Top-right now."
- "Done. All corners saved."

If user says recalibrate/restart, start over from top-left."""
