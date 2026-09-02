# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Keep every navigation path planner consistent with keepout enforcement."""

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COSTMAP_YAML = _REPO_ROOT / "ros2_ws/src/mars_bot/mars_nav/config/costmap.yaml"
_KEEP_OUT_FILTERS = ["keepout_filter", "keepout_inflation"]
_FILTER_INFO_TOPIC = "/nav/keepout_filter_info"


def _costmap_params(config, *path):
    node = config
    for key in path:
        node = node[key]
    return node["ros__parameters"]


def test_mapfree_planner_enforces_the_controller_keepouts():
    """A local-frame path must not be planned through a zone the controller rejects."""
    config = yaml.safe_load(_COSTMAP_YAML.read_text())

    controller = _costmap_params(config, "local_costmap", "local_costmap")
    mapfree = _costmap_params(config, "mapfree", "global_costmap", "global_costmap")

    assert mapfree["global_frame"] == "odom"
    assert mapfree["filters"] == _KEEP_OUT_FILTERS
    assert mapfree["keepout_filter"] == controller["keepout_filter"]
    assert mapfree["keepout_inflation"] == controller["keepout_inflation"]
    assert mapfree["keepout_filter"]["filter_info_topic"] == _FILTER_INFO_TOPIC
