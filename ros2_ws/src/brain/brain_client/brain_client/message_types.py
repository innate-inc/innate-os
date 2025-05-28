# message_types.py

from enum import Enum
from typing import Dict, Optional, List, Any
from pydantic import BaseModel, field_serializer


class TaskType(Enum):
    NAVIGATE_TO_POSITION = "navigate_to_position"
    SEND_EMAIL = "send_email"
    SEND_PICTURE_VIA_EMAIL = "send_picture_via_email"
    PICK_UP_TRASH = "pick_up_trash"
    

class NavigationToPosition(BaseModel):
    x: float
    y: float


class Task(BaseModel):
    type: TaskType
    inputs: Dict[str, Any]
    primitive_id: str

    @field_serializer("type")
    def serialize_task_type(self, value: TaskType) -> str:
        return value.value


# These types represent messages coming in from the client (e.g. user or robot data)
class MessageInType(str, Enum):
    AUTH = "auth"
    DIRECTIVE = "directive"
    IMAGE = "image"
    POSE_IMAGE = "pose_image"
    CHAT_IN = "chat_in"
    PRIMITIVE_ACTIVATED = "primitive_activated"
    PRIMITIVE_COMPLETED = "primitive_completed"
    PRIMITIVE_INTERRUPTED = "primitive_interrupted"
    PRIMITIVE_FAILED = "primitive_failed"
    REGISTER_PRIMITIVES_AND_DIRECTIVE = "register_primitives_and_directive"
    RESET = "reset"


# Outgoing messages from the server/agent
class MessageOutType(str, Enum):
    READY_FOR_IMAGE = "ready_for_image"
    VISION_AGENT_OUTPUT = "vision_agent_output"
    CHAT_OUT = "chat_out"
    THOUGHTS = "thoughts"
    ERROR = "error"
    STOP_AND_GO_BACK = "stop_and_go_back"
    PRIMITIVES_AND_DIRECTIVE_REGISTERED = "primitives_and_directive_registered"


class MessageIn(BaseModel):
    type: MessageInType
    payload: Dict[str, Any]


class MessageOut(BaseModel):
    type: MessageOutType
    payload: Dict[str, Any]

    @field_serializer("type")
    def serialize_message_out_type(self, value: MessageOutType) -> str:
        return value.value


class InternalMessageType(str, Enum):
    READY_FOR_CONNECTION = "ready_for_connection"


class InternalMessage(BaseModel):
    type: InternalMessageType


class VisionAgentOutput(BaseModel):
    """
    Represents the output received from the vision agent backend.
    If a 'next_task' is provided, Task's serializer will ensure the TaskType is properly converted.
    """

    stop_current_task: bool
    observation: str
    thoughts: str
    new_goal: Optional[str]
    next_task: Optional[Task] = None
    anticipation: Optional[str]
    to_tell_user: Optional[str]
