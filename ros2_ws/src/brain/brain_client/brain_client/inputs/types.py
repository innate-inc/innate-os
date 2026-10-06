#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Input Device Type Definitions

Base class for robot input devices. Input devices are pure Python classes
with no ROS dependencies - they process data and send results via callbacks.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from brain_client.common.dynamic_loader import class_name_to_snake_case
from brain_client.common.logging import UniversalLogger
from innate_proxy import ProxyClient


class InputDevice(ABC):
    """
    Base class for all input devices.

    Input devices are pure Python classes with NO ROS dependencies.
    They process incoming data and send results via the data callback.

    The InputManagerNode handles all ROS communication (topics, services, etc.)
    and sets the logger and proxy attributes after instantiation.

    Usage in your input device:
        # Access proxy services
        conn = self.proxy.elevenlabs.realtime.connect_sync(...)
        self.proxy.cartesia.tts.sse(...)

        # Access config (models, voice IDs, etc.)
        model = self.proxy.config.get("elevenlabs_stt_model", "default")
    """

    def __init__(self):
        """Initialize the input device with default attributes."""
        self.logger = UniversalLogger(enabled=False)
        self._proxy: ProxyClient | None = None
        self._data_callback: Callable | None = None
        self._node = None  # ROS node (optional, for devices that need ROS subscriptions)
        self._active = False  # Start inactive

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Unique name for this input device.
        Must be defined by every subclass.
        """
        pass

    @abstractmethod
    def on_open(self):
        """
        Called when this input device is activated.

        This is where you should start your data collection logic:
        - Open connections (websockets, files, etc.)
        - Start threads for data collection
        - Initialize hardware interfaces

        The device should actively collect data and call self.send_data()
        whenever it has something to send to the agent.

        Should not block - if you need long-running operations, start them
        in a background thread.
        """
        pass

    @abstractmethod
    def on_close(self):
        """
        Called when this input device is deactivated.

        Clean up resources:
        - Close connections
        - Stop threads
        - Release hardware

        Should not block.
        """
        pass

    def initialize(self) -> bool:
        """
        Initialize the input device (optional).

        Override this if you need one-time setup that doesn't depend on
        activation state. This is called once when InputManagerNode starts,
        before any on_open() calls.

        Returns:
            True if initialization succeeded, False otherwise
        """
        return True

    def shutdown(self):  # noqa: B027
        """
        Final cleanup when InputManagerNode shuts down (optional).

        Override this for final cleanup. Note: on_close() is called before
        this, so you typically only need one or the other.
        """
        pass

    def set_data_callback(self, callback: Callable[[str, dict[str, Any], str], None]):
        """
        Set the callback function for sending data to the agent.

        This is called by the InputManagerNode to register the callback.

        Args:
            callback: Function with signature (device_name, data, data_type) -> None
        """
        self._data_callback = callback
        self.logger.debug(f"Data callback set for input device '{self.name}'")

    def send_data(self, data: dict[str, Any], data_type: str = "custom"):
        """
        Send processed data to the agent.

        Call this method when your input device has data to send to the agent.
        The data will be routed through the InputManagerNode and sent to
        the brain_client, which forwards it to the agent.

        Args:
            data: Dictionary containing the processed data
            data_type: Type of data - one of:
                      - "chat_in": Text input from user (voice, keyboard, etc.)
                      - "custom": Any other data the agent should see
                      - "telemetry": UI-only status the agent must NOT see
                        (published on /input_manager/telemetry for the webapp)

        Example:
            self.send_data({
                "text": "Hello robot",
                "confidence": 0.95,
                "source": "microphone"
            }, data_type="chat_in")
        """
        if self._data_callback:
            try:
                self._data_callback(self.name, data, data_type)
            except Exception as e:
                self.logger.error(f"Error sending data for input device '{self.name}': {e}")
        else:
            self.logger.warning(f"No data callback set for input device '{self.name}'")

    def set_active(self, active: bool):
        """
        Enable or disable this input device.

        When inactive, the device still receives data via process_data()
        but can choose to ignore it.

        Args:
            active: True to activate, False to deactivate
        """
        self._active = active
        status = "activated" if active else "deactivated"
        self.logger.info(f"Input device '{self.name}' {status}")

    def is_active(self) -> bool:
        """
        Check if this input device is currently active.

        Returns:
            True if active, False otherwise
        """
        return self._active

    @property
    def proxy(self) -> ProxyClient | None:
        """
        Access to proxy services (Cartesia, ElevenLabs, Gemini)

        Returns:
            ProxyClient instance or None if not configured
        """
        return self._proxy

    def set_proxy(self, proxy: ProxyClient | None):
        """
        Set the proxy client (called by InputLoader).

        Args:
            proxy: ProxyClient instance, or None when the proxy is not configured
        """
        self._proxy = proxy

    def set_tts_playing(self, is_playing: bool) -> None:  # noqa: B027 — optional hook, no-op by default
        """TTS ducking hook: the robot started/stopped speaking. Default: ignore.

        Devices that listen to audio (e.g. the microphone) override this to
        avoid hearing the robot's own speech.
        """

    @property
    def node(self):
        """
        Access to the ROS node (optional).

        Use this for devices that need ROS subscriptions or services.
        Not all devices need this - only use it if you need direct ROS access.

        Returns:
            ROS node instance or None if not set
        """
        return self._node

    def set_node(self, node):
        """
        Set the ROS node reference (called by InputManagerNode).

        Args:
            node: ROS node instance
        """
        self._node = node

    def set_logger(self, logger):
        """
        Set the logger instance (called by InputLoader).

        Args:
            logger: Logger instance (ROS logger or any logger)
        """
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)


def input_name_for_class(cls: type[InputDevice]) -> str:
    """The name a device class registers under: the instance ``name`` when the
    class instantiates cleanly, else snake_case(ClassName) minus the ``Input``
    suffix. The loader resolves through this too, so a class reference and its
    name string are interchangeable."""
    try:
        return cls().name
    except Exception:
        return class_name_to_snake_case(cls.__name__, strip_suffixes=("Input",))
