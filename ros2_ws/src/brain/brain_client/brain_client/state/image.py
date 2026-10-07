# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing camera frames. ROS-free on purpose."""

import base64
from typing import TypeVar

import numpy as np

_ImageT = TypeVar("_ImageT", bound="Image")


class Image(str):
    """A camera frame: the value IS the JPEG as base64 text, so anything that
    treated frames as base64 strings keeps working. ``.jpeg`` is the decoded
    bytes."""

    @classmethod
    def from_jpeg(cls: "type[_ImageT]", data: bytes) -> "_ImageT":
        image = cls(base64.b64encode(data).decode("ascii"))
        image._jpeg = data
        return image

    @property
    def jpeg(self) -> bytes:
        """The frame as raw JPEG bytes (decoded once, then cached)."""
        cached = self.__dict__.get("_jpeg")
        if cached is None:
            cached = base64.b64decode(self)
            self._jpeg = cached
        return cached


class MainImage(Image):
    """A main-camera frame, declared via ``image: MainImage``."""


class MainHighResImage(Image):
    """Native-resolution left frame; opt in with ``main_highres_image: MainHighResImage``."""


class WristImage(Image):
    """A wrist-camera frame, declared via ``image: WristImage``."""


class DepthMap(np.ndarray):
    """A (height, width) depth array (uint16 mm or float32 m), declared via
    ``depth: DepthMap``. A view-cast of the decoded frame; no state of its own."""
