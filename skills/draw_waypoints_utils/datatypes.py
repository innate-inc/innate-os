from typing import Optional
from enum import Enum
from dataclasses import dataclass

class ActionType(Enum):
    WAYPOINT = 0
    LIFT = 1
    DROP = 2

@dataclass(frozen = True)
class Waypoint:
    x: float
    y: float

@dataclass(frozen = True)
class Action:
    type: ActionType
    waypoint: Optional[Waypoint]
