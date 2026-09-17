# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import logging
import time
from functools import cached_property

import cv2
import numpy as np
from lerobot.robots.robot import Robot
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected
from lerobot.utils.errors import DeviceNotConnectedError

from .config_mars import MarsConfig
from .schema import (
    ACTION_NAMES,
    BASE_NAMES,
    CAMERA_ORDER,
    CAMERA_SHAPE,
    STATE_NAMES,
    action_features,
    camera_features,
    observation_features,
)
from .wire import CAMERAS_KEY, TOPIC_OBS, Header, Link, Message

RobotObservation = dict[str, object]
RobotAction = dict[str, object]

logger = logging.getLogger(__name__)


class Mars(Robot):
    """The Innate MARS as seen from a lerobot process: a client of the bridge on the robot."""

    config_class = MarsConfig
    name = "mars"

    def __init__(self, config: MarsConfig) -> None:
        super().__init__(config)
        self.config = config
        # Recording scripts size their image-writer pool by len(robot.cameras).
        self.cameras: dict[str, tuple[int, int, int]] = camera_features()
        self._link = Link(config.remote_ip, config.port_actions, config.port_observations, TOPIC_OBS)
        self._state: dict[str, float] = {}
        self._frames: dict[str, np.ndarray] = {}

    @cached_property
    def observation_features(self) -> dict[str, type | tuple[int, int, int]]:
        return observation_features()

    @cached_property
    def action_features(self) -> dict[str, type]:
        return action_features()

    @property
    def is_connected(self) -> bool:
        return self._link.is_open

    @property
    def is_calibrated(self) -> bool:
        return True

    def calibrate(self) -> None:
        pass

    def configure(self) -> None:
        pass

    @check_if_already_connected
    def connect(self, calibrate: bool = True) -> None:
        deadline = time.monotonic() + self.config.connect_timeout_s
        self._absorb(self._link.open(self.config.connect_timeout_s))
        while self._missing_cameras() and time.monotonic() < deadline:
            message = self._link.latest(timeout_ms=200)
            if message is not None:
                self._absorb(message)
        if self._missing_cameras():
            logger.warning(
                "MARS bridge is not sending %s frames yet; recording blank frames until it does",
                self._missing_cameras(),
            )

    def _missing_cameras(self) -> list[str]:
        return [camera for camera in CAMERA_ORDER if camera not in self._frames]

    @check_if_not_connected
    def disconnect(self) -> None:
        self._link.close()

    @check_if_not_connected
    def get_observation(self) -> RobotObservation:
        self._link.heartbeat_if_due()
        message = self._link.latest(self.config.poll_timeout_ms)
        if message is not None:
            self._absorb(message)
        observation: RobotObservation = dict(self._state)
        for camera in CAMERA_ORDER:
            observation[camera] = self._frames.get(camera, _blank_frame())
        return observation

    @check_if_not_connected
    def send_action(self, action: RobotAction) -> RobotAction:
        missing = [name for name in STATE_NAMES if name not in action]
        if missing:
            raise ValueError(f"MARS action needs all six joint targets; missing {missing}")
        payload = {name: float(action[name]) for name in STATE_NAMES}
        payload.update({name: float(action.get(name, 0.0)) for name in BASE_NAMES})
        if self.config.external_commands:
            self._link.heartbeat_if_due()
        else:
            self._link.send(payload)
        return {name: payload[name] for name in ACTION_NAMES}

    def _absorb(self, message: Message) -> None:
        header, jpegs = message
        self._state = _state_from(header)
        cameras = header.get(CAMERAS_KEY)
        if not isinstance(cameras, list):
            return
        for camera, jpeg in zip(cameras, jpegs, strict=False):
            frame = _decode(jpeg)
            if frame is not None:
                self._frames[str(camera)] = frame


def _state_from(header: Header) -> dict[str, float]:
    try:
        return {name: float(header[name]) for name in STATE_NAMES}
    except (KeyError, TypeError, ValueError) as e:
        raise DeviceNotConnectedError(f"Malformed MARS bridge header: {e}") from e


def _decode(jpeg: bytes) -> np.ndarray | None:
    if not jpeg:
        return None
    bgr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def _blank_frame() -> np.ndarray:
    return np.zeros(CAMERA_SHAPE, dtype=np.uint8)
