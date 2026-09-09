# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The people node's ROS parameters as plain data: what the launch file
declares, the frozen config those values parse into, and the two left-eye
topics ``tick_source`` chooses between. PURE module: no rclpy, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from brain_client.common.enums import StrEnum
from brain_client.common.script_paths import get_innate_os_root

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

COMPRESSED_IMAGE_TOPIC = "/mars/main_camera/left/image_raw/compressed"
RAW_IMAGE_TOPIC = "/mars/main_camera/left/image_raw"


class TickSource(StrEnum):
    """Which left-eye topic the engine ticks on; wire-visible (a parameter)."""

    COMPRESSED = "compressed"
    RAW = "raw"


@dataclass(frozen=True)
class PeopleNodeConfig:
    """The node's ROS parameters as plain data. Whether to run at all is not
    one of them: the launch file reads that from settings.yaml before starting
    the process, because a node that exits is a node ``respawn`` restarts every
    two seconds."""

    always_on: bool = False
    seek_faces: bool = False
    scribe: bool = True
    prefer_backend: str = "opencv"
    tick_source: str = TickSource.COMPRESSED
    allow_model_download: bool = True
    retention_unnamed_days: float = 14.0
    retention_named_days: float = 548.0
    simulator_mode: bool = False
    camera_height_m: float = 0.26
    gemini_model: str = "gemini-3.6-flash"

    @property
    def data_dir(self) -> Path:
        """Sim evidence never mixes with hardware evidence (RFC 6.1)."""
        return get_innate_os_root() / "data" / ("people_sim" if self.simulator_mode else "people")

    @property
    def models_dir(self) -> Path:
        return get_innate_os_root() / "data" / "models" / "people"

    @property
    def image_topic(self) -> str:
        return RAW_IMAGE_TOPIC if self.tick_source == TickSource.RAW else COMPRESSED_IMAGE_TOPIC


PARAM_DEFAULTS: dict[str, bool | str | float] = {
    "always_on": False,
    "seek_faces": False,
    "scribe": True,
    "prefer_backend": "opencv",
    "tick_source": str(TickSource.COMPRESSED),
    # True so a robot provisioned without the model files under
    # data/models/people still recognizes a face after one fetch.
    "allow_model_download": True,
    "retention_unnamed_days": 14.0,
    "retention_named_days": 548.0,
    "simulator_mode": False,
    "camera_height_m": 0.26,
    "gemini_model": "gemini-3.6-flash",
}


def config_from_params(values: Mapping[str, Any]) -> PeopleNodeConfig:
    """The declared parameter values as a config. A value of the wrong type
    keeps the default: a mistyped setting must not stop the node from seeing."""
    fields: dict[str, Any] = {}
    for name, default in PARAM_DEFAULTS.items():
        value = values.get(name, default)
        if isinstance(default, bool):
            fields[name] = bool(value)
        elif isinstance(default, float):
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            fields[name] = float(value) if numeric else default
        else:
            fields[name] = value if isinstance(value, str) and value else default
    if fields["tick_source"] not in (TickSource.COMPRESSED, TickSource.RAW):
        fields["tick_source"] = str(TickSource.COMPRESSED)
    return PeopleNodeConfig(**fields)
