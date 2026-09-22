from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "ros2_ws" / "src" / "brain" / "manipulation" / "manipulation" / "lerobot_bridge.py"
PUBLISH_JOB_PATH = REPO_ROOT / "webapp" / "proxy" / "hub_publish.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def bridge_module() -> ModuleType:
    """The robot-side bridge, loaded straight from the ROS package so both halves are tested together."""
    return _load(BRIDGE_PATH)


@pytest.fixture(scope="session")
def publish_job_module() -> ModuleType:
    """The webapp's publish job, which reads the export record the converter writes."""
    return _load(PUBLISH_JOB_PATH)


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their annotations through sys.modules
    spec.loader.exec_module(module)
    return module
