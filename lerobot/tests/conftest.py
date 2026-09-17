from __future__ import annotations

import importlib.util
import socket
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "ros2_ws" / "src" / "brain" / "manipulation" / "manipulation" / "lerobot_bridge.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def bridge_module() -> ModuleType:
    """The robot-side bridge, loaded straight from the ROS package so both halves are tested together."""
    spec = importlib.util.spec_from_file_location("lerobot_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
