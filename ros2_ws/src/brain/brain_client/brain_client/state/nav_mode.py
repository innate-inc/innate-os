# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing navigation lifecycle mode. ROS-free on purpose."""

from dataclasses import dataclass


@dataclass(frozen=True)
class NavMode:
    """Current navigation lifecycle mode, read via ``self.nav_mode``."""

    value: str

    @property
    def is_mapping(self) -> bool:
        return self.value in {"mapping", "autonomous_mapping"}

    @property
    def is_autonomous_mapping(self) -> bool:
        return self.value == "autonomous_mapping"

    @property
    def supports_map_navigation(self) -> bool:
        return self.value in {"navigation", "autonomous_mapping"}
