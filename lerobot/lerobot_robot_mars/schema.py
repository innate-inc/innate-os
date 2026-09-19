# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What MARS observes and does, named once.

The live client, the passthrough teleoperator, and the HDF5 converter all build their
feature dicts from here, so a dataset recorded live and one converted from the on-robot
recorder are interchangeable to a trainer.
"""

from __future__ import annotations

from lerobot.utils.constants import ACTION, OBS_STR
from lerobot.utils.feature_utils import combine_feature_dicts, hw_to_dataset_features

FPS = 30
ROBOT_TYPE = "mars"

JOINTS: tuple[str, ...] = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")  # joint6 is the gripper
STATE_NAMES: tuple[str, ...] = tuple(f"{joint}.pos" for joint in JOINTS)
BASE_NAMES: tuple[str, ...] = ("x.vel", "theta.vel")
ACTION_NAMES: tuple[str, ...] = STATE_NAMES + BASE_NAMES

CAMERA_ORDER: tuple[str, ...] = ("head", "wrist")
CAMERA_SHAPE: tuple[int, int, int] = (480, 640, 3)  # (h, w, c), RGB


def state_features() -> dict[str, type]:
    return dict.fromkeys(STATE_NAMES, float)


def action_features() -> dict[str, type]:
    return dict.fromkeys(ACTION_NAMES, float)


def camera_features() -> dict[str, tuple[int, int, int]]:
    return dict.fromkeys(CAMERA_ORDER, CAMERA_SHAPE)


def observation_features() -> dict[str, type | tuple[int, int, int]]:
    return {**state_features(), **camera_features()}


def dataset_features(use_videos: bool = True) -> dict[str, dict]:
    return combine_feature_dicts(
        hw_to_dataset_features(observation_features(), OBS_STR, use_videos),
        hw_to_dataset_features(action_features(), ACTION, use_videos),
    )


def image_key(camera: str) -> str:
    return f"{OBS_STR}.images.{camera}"
