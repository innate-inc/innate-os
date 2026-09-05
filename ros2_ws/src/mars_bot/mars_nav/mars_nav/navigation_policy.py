# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure navigation-mode policy shared by the lifecycle manager and router.

Keep this module ROS-free.  Besides making the safety rules explicit in one
place, that lets the host-only test job exercise mode routing without a ROS
installation.
"""

import math
from dataclasses import dataclass
from enum import Enum


class NavigationMode(Enum):
    NAV = "navigation"
    MAPPING = "mapping"
    AUTONOMOUS_MAPPING = "autonomous_mapping"
    MAPFREE = "mapfree"


VALID_NAVIGATION_MODES = frozenset(mode.value for mode in NavigationMode)
MAPPING_MODES = frozenset(
    {
        NavigationMode.MAPPING.value,
        NavigationMode.AUTONOMOUS_MAPPING.value,
    }
)

# Mapping is intentionally a slow-driving activity.  The final envelope starts
# from the lower of the robot's manual and Nav2 limits on each axis, then
# applies the fixed Slow preset. Launch-time motion_control/nav overrides are
# passed to cmd_vel_mux. Keep the ratio aligned with
# mars_control::drive::SPEED_MODES.
DEFAULT_MANUAL_MAX_LINEAR_MPS = 0.4
DEFAULT_MANUAL_MAX_ANGULAR_RPS = 1.0
DEFAULT_NAV_MAX_LINEAR_MPS = 0.45
DEFAULT_NAV_MAX_ANGULAR_RPS = 0.6
MAPPING_SLOW_SCALE = 0.35
MAPPING_SPEED_LIMIT_SERVICE = "/nav/set_mapping_speed_limit"

# ``switching`` is published before active navigation is cancelled.  It must
# remain limited so a mode exit cannot briefly speed up a still-settling goal.
# Unknown/startup state also fails slow until a stable non-mapping mode arrives.
_UNLIMITED_DRIVE_MODES = frozenset(
    {
        NavigationMode.NAV.value,
        NavigationMode.MAPFREE.value,
    }
)


def mapping_speed_limit_active(current_mode: str | None) -> bool:
    """Whether final base commands must remain inside the Slow envelope."""

    mode = (current_mode or "").strip().lower()
    return mode not in _UNLIMITED_DRIVE_MODES


def starts_new_mapping_session(previous_mode: str | None, target_mode: str, *, first_start: bool = False) -> bool:
    """Whether starting ``target_mode`` creates a new live SLAM frame.

    Manual and autonomous mapping share the same active slam_toolbox when
    handing off between each other. A cold start or entry from a non-mapping
    mode activates SLAM and therefore starts a new coordinate-frame session.
    """

    return target_mode in MAPPING_MODES and (first_start or previous_mode not in MAPPING_MODES)


def mapping_slow_limits(
    manual_max_linear_mps: float,
    manual_max_angular_rps: float,
    nav_max_linear_mps: float,
    nav_max_angular_rps: float,
) -> tuple[float, float]:
    """Final mapping limits shared by manual and autonomous drive sources."""

    limits = (
        manual_max_linear_mps,
        manual_max_angular_rps,
        nav_max_linear_mps,
        nav_max_angular_rps,
        MAPPING_SLOW_SCALE,
    )
    if not all(math.isfinite(value) and value > 0.0 for value in limits):
        return 0.0, 0.0
    return (
        min(manual_max_linear_mps, nav_max_linear_mps) * MAPPING_SLOW_SCALE,
        min(manual_max_angular_rps, nav_max_angular_rps) * MAPPING_SLOW_SCALE,
    )


def mapping_speed_factor(
    speed_limit_active: bool,
    linear_x: float,
    angular_z: float,
    max_linear_mps: float,
    max_angular_rps: float,
) -> float:
    """Uniform Twist scale that fits the mapping Slow envelope.

    Scaling both axes by one factor preserves curvature.  An absolute envelope
    avoids applying Slow twice to teleop commands that mars_app already shaped
    at the Slow preset, while still constraining Fast/Mad, skills, Nav2, and
    recovery commands at the final mux.
    """

    if not speed_limit_active:
        return 1.0
    values = (linear_x, angular_z, max_linear_mps, max_angular_rps)
    if not all(math.isfinite(value) for value in values) or max_linear_mps <= 0.0 or max_angular_rps <= 0.0:
        return 0.0
    return min(
        1.0,
        max_linear_mps / abs(linear_x) if linear_x else 1.0,
        max_angular_rps / abs(angular_z) if angular_z else 1.0,
    )


# Nodes that should only be configured (not activated) in specific modes.
# The shared BT constructs both planner action nodes even though its Switch
# ticks only one.  A configured planner exposes the action endpoint without
# running its costmap or accepting planning goals.
configure_only_nodes = {
    NavigationMode.AUTONOMOUS_MAPPING.value: {"mapfree/planner_server"},
    NavigationMode.MAPFREE.value: {"navigation/planner_server"},
}

# Nodes that should only be deactivated (not cleaned up/unconfigured) to avoid
# the observed Zenoh/Nav2 teardown crashes.
skip_cleanup_nodes = {
    "controller_server",
    "mapfree/planner_server",
    "navigation/planner_server",
}

# Ordered lifecycle targets. Autonomous mapping excludes the saved-map
# localization stack and every competing /map or map->odom owner: map_server,
# AMCL, grid_localizer and the mapfree null-map node. slam_toolbox is activated
# first so its live map and transform exist before the navigation costmap comes
# up; bt_navigator is last so all action servers it binds are already present.
modes_nodes = {
    NavigationMode.MAPPING.value: ["slam_toolbox"],
    NavigationMode.AUTONOMOUS_MAPPING.value: [
        "slam_toolbox",
        "mapfree/planner_server",
        "navigation/planner_server",
        "controller_server",
        "behavior_server",
        "velocity_smoother",
        "bt_navigator",
    ],
    NavigationMode.MAPFREE.value: [
        "null_map_node",
        "navigation/planner_server",
        "mapfree/planner_server",
        "controller_server",
        "bt_navigator",
        "behavior_server",
        "velocity_smoother",
    ],
    NavigationMode.NAV.value: [
        "navigation_map_server",
        "navigation_grid_localizer",
        "navigation_amcl",
        "mapfree/planner_server",
        "navigation/planner_server",
        "controller_server",
        "bt_navigator",
        "behavior_server",
        "velocity_smoother",
    ],
}


@dataclass(frozen=True)
class NavigationRoute:
    planner: str
    goal_checker: str


_ROUTABLE_SELECTORS = {
    NavigationMode.NAV.value: frozenset({"navigation", "mapfree"}),
    NavigationMode.AUTONOMOUS_MAPPING.value: frozenset({"navigation", "autonomous_mapping"}),
    NavigationMode.MAPFREE.value: frozenset({"mapfree"}),
}


def resolve_navigation_route(current_mode: str | None, requested_selector: str | None = None) -> NavigationRoute | None:
    """Resolve a planner selector, failing closed when its server is inactive.

    ``NavigateToPose.behavior_tree`` is currently used as a lightweight
    planner selector by the root router.  Unknown strings (including a real BT
    XML path), switching/none, and manual mapping all reject instead of falling
    through to mapfree.
    """

    mode = (current_mode or "").strip().lower()
    allowed = _ROUTABLE_SELECTORS.get(mode)
    if allowed is None:
        return None

    selector = (requested_selector or "").strip().lower()
    if not selector:
        selector = mode
    if selector not in allowed:
        return None
    if selector == NavigationMode.AUTONOMOUS_MAPPING.value:
        selector = "navigation"

    if selector == "navigation":
        return NavigationRoute(planner="navigation", goal_checker="goal_checker")
    return NavigationRoute(planner="mapfree", goal_checker="goal_checker_precise")
