#!/usr/bin/env python3
"""
Shared Protocol Definitions for Waypoint Inference System

Defines message formats for WebSocket communication between:
- Robot client (streams camera/odom, receives waypoints)
- Inference server (receives stream, sends waypoint commands)
"""

from dataclasses import dataclass, asdict
from typing import List, Tuple, Optional, Dict, Any
import json
import base64
import struct
import time

# Message types
MSG_TYPE_SENSOR_DATA = "sensor_data"
MSG_TYPE_WAYPOINT_COMMAND = "waypoint_command"
MSG_TYPE_TASK_START = "task_start"
MSG_TYPE_TASK_STOP = "task_stop"
MSG_TYPE_HEARTBEAT = "heartbeat"
MSG_TYPE_STATUS = "status"
MSG_TYPE_ERROR = "error"
MSG_TYPE_KILL = "kill"

# Default ports
DEFAULT_INFERENCE_PORT = 8780


@dataclass
class Odometry:
    """Robot odometry state."""
    x: float  # meters, in odom frame
    y: float  # meters, in odom frame
    yaw: float  # radians
    timestamp: float  # seconds since epoch
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: dict) -> 'Odometry':
        return cls(**d)


@dataclass
class SensorData:
    """Sensor data streamed from robot to server."""
    # Image data (JPEG compressed, base64 encoded)
    image_b64: str
    image_width: int
    image_height: int
    
    # Robot state
    odom: Odometry
    head_pitch_deg: float  # Head pitch angle in degrees
    
    # Timing
    timestamp: float  # Sensor timestamp
    seq: int  # Sequence number
    
    def to_json(self) -> str:
        return json.dumps({
            "type": MSG_TYPE_SENSOR_DATA,
            "image_b64": self.image_b64,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "odom": self.odom.to_dict(),
            "head_pitch_deg": self.head_pitch_deg,
            "timestamp": self.timestamp,
            "seq": self.seq
        })
    
    @classmethod
    def from_json(cls, json_str: str) -> 'SensorData':
        d = json.loads(json_str)
        return cls(
            image_b64=d["image_b64"],
            image_width=d["image_width"],
            image_height=d["image_height"],
            odom=Odometry.from_dict(d["odom"]),
            head_pitch_deg=d["head_pitch_deg"],
            timestamp=d["timestamp"],
            seq=d["seq"]
        )


@dataclass
class Waypoint:
    """A single waypoint in world (odom) frame."""
    x: float  # meters
    y: float  # meters
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, d: dict) -> 'Waypoint':
        return cls(**d)


@dataclass
class WaypointCommand:
    """Waypoint command from server to robot."""
    waypoints: List[Waypoint]  # List of waypoints in odom frame
    timestamp: float  # Server timestamp when command was generated
    task_label: str  # Task description for logging
    seq: int  # Command sequence number
    
    def to_json(self) -> str:
        return json.dumps({
            "type": MSG_TYPE_WAYPOINT_COMMAND,
            "waypoints": [w.to_dict() for w in self.waypoints],
            "timestamp": self.timestamp,
            "task_label": self.task_label,
            "seq": self.seq
        })
    
    @classmethod
    def from_json(cls, json_str: str) -> 'WaypointCommand':
        d = json.loads(json_str)
        return cls(
            waypoints=[Waypoint.from_dict(w) for w in d["waypoints"]],
            timestamp=d["timestamp"],
            task_label=d["task_label"],
            seq=d["seq"]
        )


@dataclass
class TaskStart:
    """Command to start a task."""
    task_label: str
    num_waypoints: int  # Number of waypoints to predict
    inference_interval_ms: int  # How often to run inference
    
    def to_json(self) -> str:
        return json.dumps({
            "type": MSG_TYPE_TASK_START,
            "task_label": self.task_label,
            "num_waypoints": self.num_waypoints,
            "inference_interval_ms": self.inference_interval_ms
        })
    
    @classmethod
    def from_json(cls, json_str: str) -> 'TaskStart':
        d = json.loads(json_str)
        return cls(
            task_label=d["task_label"],
            num_waypoints=d["num_waypoints"],
            inference_interval_ms=d["inference_interval_ms"]
        )


@dataclass
class TaskStop:
    """Command to stop current task."""
    reason: str = "user_stop"
    
    def to_json(self) -> str:
        return json.dumps({
            "type": MSG_TYPE_TASK_STOP,
            "reason": self.reason
        })
    
    @classmethod
    def from_json(cls, json_str: str) -> 'TaskStop':
        d = json.loads(json_str)
        return cls(reason=d.get("reason", "user_stop"))


@dataclass
class Kill:
    """Emergency kill command - robot stops immediately."""
    def to_json(self) -> str:
        return json.dumps({"type": MSG_TYPE_KILL})


@dataclass
class Status:
    """Status update from robot or server."""
    source: str  # "robot" or "server"
    state: str  # Current state
    message: str  # Human-readable message
    data: Optional[Dict[str, Any]] = None  # Additional data
    
    def to_json(self) -> str:
        return json.dumps({
            "type": MSG_TYPE_STATUS,
            "source": self.source,
            "state": self.state,
            "message": self.message,
            "data": self.data or {}
        })
    
    @classmethod
    def from_json(cls, json_str: str) -> 'Status':
        d = json.loads(json_str)
        return cls(
            source=d["source"],
            state=d["state"],
            message=d["message"],
            data=d.get("data")
        )


@dataclass  
class Heartbeat:
    """Heartbeat to keep connection alive."""
    timestamp: float
    
    def to_json(self) -> str:
        return json.dumps({
            "type": MSG_TYPE_HEARTBEAT,
            "timestamp": self.timestamp
        })


def parse_message(json_str: str) -> Any:
    """Parse a JSON message and return the appropriate dataclass."""
    try:
        d = json.loads(json_str)
        msg_type = d.get("type")
        
        if msg_type == MSG_TYPE_SENSOR_DATA:
            return SensorData.from_json(json_str)
        elif msg_type == MSG_TYPE_WAYPOINT_COMMAND:
            return WaypointCommand.from_json(json_str)
        elif msg_type == MSG_TYPE_TASK_START:
            return TaskStart.from_json(json_str)
        elif msg_type == MSG_TYPE_TASK_STOP:
            return TaskStop.from_json(json_str)
        elif msg_type == MSG_TYPE_STATUS:
            return Status.from_json(json_str)
        elif msg_type == MSG_TYPE_HEARTBEAT:
            return Heartbeat(timestamp=d["timestamp"])
        elif msg_type == MSG_TYPE_KILL:
            return Kill()
        else:
            return d  # Return raw dict for unknown types
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse message: {e}")


def body_to_world(x_body: float, y_body: float, 
                  robot_x: float, robot_y: float, robot_yaw: float) -> Tuple[float, float]:
    """
    Transform a point from robot body frame to world (odom) frame.
    
    Args:
        x_body, y_body: Point in robot body frame (forward, left)
        robot_x, robot_y: Robot position in world frame
        robot_yaw: Robot heading in world frame (radians)
    
    Returns:
        (x_world, y_world): Point in world frame
    """
    import math
    cos_yaw = math.cos(robot_yaw)
    sin_yaw = math.sin(robot_yaw)
    
    # Rotate body point by robot yaw
    x_world = robot_x + x_body * cos_yaw - y_body * sin_yaw
    y_world = robot_y + x_body * sin_yaw + y_body * cos_yaw
    
    return x_world, y_world

