# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

from functools import cached_property
from typing import Any

from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected
from lerobot.utils.errors import DeviceNotConnectedError

from .config_mars_passthrough_teleop import MarsPassthroughTeleopConfig
from .schema import ACTION_NAMES, action_features
from .wire import COMMAND_PREFIX, TOPIC_STATE, Header, Link

RobotAction = dict[str, Any]


class MarsPassthroughTeleop(Teleoperator):
    config_class = MarsPassthroughTeleopConfig
    name = "mars_passthrough"

    def __init__(self, config: MarsPassthroughTeleopConfig) -> None:
        super().__init__(config)
        self.config = config
        self._link = Link(config.remote_ip, config.port_actions, config.port_observations, TOPIC_STATE)
        self._command: dict[str, float] = {}

    @cached_property
    def action_features(self) -> dict[str, type]:
        return action_features()

    @cached_property
    def feedback_features(self) -> dict[str, type]:
        return {}

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
        header, _ = self._link.open(self.config.connect_timeout_s)
        self._command = _command_from(header)

    @check_if_not_connected
    def disconnect(self) -> None:
        self._link.close()

    @check_if_not_connected
    def get_action(self) -> RobotAction:
        self._link.heartbeat_if_due()
        message = self._link.latest(self.config.poll_timeout_ms)
        if message is not None:
            self._command = _command_from(message[0])
        return dict(self._command)

    def send_feedback(self, feedback: dict[str, Any]) -> None:
        pass


def _command_from(header: Header) -> dict[str, float]:
    try:
        return {name: float(header[COMMAND_PREFIX + name]) for name in ACTION_NAMES}
    except (KeyError, TypeError, ValueError) as e:
        raise DeviceNotConnectedError(f"Malformed MARS bridge header: {e}") from e
