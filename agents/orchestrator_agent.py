from typing import List, Optional

from brain_client.agent_types import Agent


class OrchestratorAgent(Agent):
    """
    Default entry-point directive: meta layer that routes to specialist agents.

    All agent modules are loaded at brain startup; this directive does not invoke
    skills/tools itself. The brain client switches the active directive (another
    agent) when routing decides a specialist should run—only that agent's skills
    are registered with the cloud model.
    """

    @property
    def id(self) -> str:
        return "orchestrator_agent"

    @property
    def display_name(self) -> str:
        return "Agent Orchestrator"

    def get_skills(self) -> List[str]:
        # No primitives while orchestrator is active — specialists own skills after switch.
        return []

    def get_inputs(self) -> List[str]:
        return ["micro"]

    def get_routing_description(self) -> Optional[str]:
        return (
            "Default orchestrator entry point; conversation and routing only; "
            "does not run skills — switches to specialists who do."
        )

    def get_prompt(self) -> str:
        return """You are the agent orchestrator for this robot: the default persona users meet first.
Be helpful and concise. Clarify the user's goal. You do not have tool or skill actions in this mode;
do not pretend to navigate, manipulate the robot, or call primitives. Other agents (loaded at startup)
are activated automatically when the task fits them (security patrol, chess, calibration, demos, etc.).
Acknowledge when a specialist mode will take over. Until then, answer conversationally only."""
