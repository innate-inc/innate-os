from typing import List
from brain_client.agent_types import Agent


class BoardCalibrationAgent(Agent):
    """
    Board Calibration Agent - guides user through 4-corner calibration.
    
    Workflow: torque off -> user moves to corner -> torque on (record FK) -> repeat for all 4 corners.
    """

    @property
    def id(self) -> str:
        return "board_calibration_agent"

    @property
    def display_name(self) -> str:
        return "Board Calibration Agent"

    def get_skills(self) -> List[str]:
        """Return torque control and position recording skills."""
        return [
            "torque_off",
            "torque_on",
            "record_position"
        ]

    def get_inputs(self) -> List[str]:
        """Enable microphone input."""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the calibration workflow prompt."""
        return """You are a calibration assistant. Guide user to calibrate 4 corners of the chess board.

WORKFLOW for each corner (TOP-LEFT → TOP-RIGHT → BOTTOM-RIGHT → BOTTOM-LEFT):
1. Call torque_off (arm goes limp)
2. Ask user to move arm to the corner center
3. When user says ready/done/record, call torque_on (locks arm)
4. Call record_position with corner parameter (e.g., corner="top_left")

Positions are saved to ~/board_calibration.json automatically.

If user asks to recalibrate or restart, begin again from TOP-LEFT.

After all 4 corners, confirm calibration is complete and saved.

Start by greeting user and begin with TOP-LEFT."""
