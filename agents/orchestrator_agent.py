from typing import List, Optional

from brain_client.agent_types import Agent


class OrchestratorAgent(Agent):
    """
    Default entry-point directive: meta layer that represents orchestration and
    general interaction before switching to a specialist agent.
    """

    @property
    def id(self) -> str:
        return "orchestrator_agent"

    @property
    def display_name(self) -> str:
        return "Agent Orchestrator"

    def get_skills(self) -> List[str]:
        return [
            "innate-os/navigate_to_position",
            "innate-os/navigate_with_vision",
        ]

    def get_inputs(self) -> List[str]:
        return ["micro"]

    def get_routing_description(self) -> Optional[str]:
        return (
            "Default orchestrator entry point; general conversation and navigation; "
            "routes to specialists when appropriate."
        )

    def get_prompt(self) -> str:
        return """You are the agent orchestrator for this robot: the default persona users meet first.
Be helpful and concise. Clarify the user's goal. When a task clearly needs a specialist
(chess, board calibration, security patrol, demos, etc.), acknowledge that the system may
switch to that mode. You can use general navigation skills when movement is needed."""
