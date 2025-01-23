# message_types.py

from enum import Enum
from typing import Optional, List, Any
from pydantic import BaseModel


class TaskType(Enum):
    NAVIGATION_IN_SIGHT = "navigation_in_sight"
    NAVIGATION_TO_MEMORY = "navigation_out_of_sight"
    ACTION_WITH_ARM = "action_with_arm"
    ASK_FOR_INFORMATION = "ask_for_information"
    VELOCITY_CONTROL = "velocity_control"


class Task(BaseModel):
    type: TaskType
    description: str


class MessageType(str, Enum):
    AUTH = "auth"
    DIRECTIVE = "directive"
    IMAGE = "image"
    READY_FOR_IMAGE = "ready_for_image"
    WELL_RECEIVED = "well_received"
    ACTION_TO_DO = "action_to_do"
    VISION_AGENT_OUTPUT = "vision_agent_output"
    # Add more types as needed


class VisionAgentOutput(BaseModel):
    """Mirrors your orchestrator.agent.models.VisionAgentOutput shape."""

    stop_current_task: bool
    observation: str
    thoughts: str
    new_goal: Optional[str]
    next_task: Optional[Task] = None
    users_implicated: List[str]
    anticipation: Optional[str]
    to_tell_user: Optional[str]
