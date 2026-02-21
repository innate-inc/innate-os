#!/usr/bin/env python3
"""
Input Device Type Definitions

Base class for robot input devices. Input devices are pure Python classes
with no ROS dependencies - they process data and send results via callbacks.
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable
from enum import Enum

from innate_proxy import ProxyClient
from brain_client.logging_config import UniversalLogger


class InputDeviceType(Enum):
    """Types of input devices"""
    AUDIO = "audio"  # Microphone, voice
    VISION = "vision"  # Camera, visual sensors
    SENSOR = "sensor"  # IMU, proximity, temperature, etc.
    HAPTIC = "haptic"  # Touch sensors, pressure sensors
    CUSTOM = "custom"  # Custom sensor types


class InputDevice(ABC):
    """
    Base class for all input devices.
    
    Input devices are pure Python classes with NO ROS dependencies.
    They process incoming data and send results via the data callback.
    
    The InputManagerNode handles all ROS communication (topics, services, etc.)
    and sets the logger and proxy attributes after instantiation.
    
    Usage in your input device:
        # Access proxy services
        self.proxy.openai.realtime.connect_sync(...)
        self.proxy.cartesia.tts.sse(...)
        
        # Access config (models, voice IDs, etc.)
        model = self.proxy.config.get("openai_realtime_model", "default")
    """

    def __init__(self):
        """Initialize the input device with default attributes."""
        self.logger = UniversalLogger(enabled=False)
        self._proxy: Optional[ProxyClient] = None
        self._data_callback: Optional[Callable] = None
        self._node = None  # ROS node (optional, for devices that need ROS subscriptions)
        self._active = False  # Start inactive
        self._config: Dict[str, Any] = {}

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

    def shutdown(self):
        """
        Final cleanup when InputManagerNode shuts down (optional).
        
        Override this for final cleanup. Note: on_close() is called before
        this, so you typically only need one or the other.
        """
        pass

    def set_data_callback(self, callback: Callable[[str, Dict[str, Any], str], None]):
        """
        Set the callback function for sending data to the agent.
        
        This is called by the InputManagerNode to register the callback.
        
        Args:
            callback: Function with signature (device_name, data, data_type) -> None
        """
        self._data_callback = callback
        self.logger.debug(f"Data callback set for input device '{self.name}'")

    def send_data(self, data: Dict[str, Any], data_type: str = "custom"):
        """
        Send processed data to the agent.
        
        Call this method when your input device has data to send to the agent.
        The data will be routed through the InputManagerNode and sent to
        the brain_client, which forwards it to the agent.
        
        Args:
            data: Dictionary containing the processed data
            data_type: Type of data - one of:
                      - "chat_in": Text input from user (voice, keyboard, etc.)
                      - "custom": Any other data type
        
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
    def proxy(self) -> Optional[ProxyClient]:
        """
        Access to proxy services (Cartesia, OpenAI, etc.)
        
        Returns:
            ProxyClient instance or None if not configured
        """
        return self._proxy
    
    def set_proxy(self, proxy: ProxyClient):
        """
        Set the proxy client (called by InputLoader).
        
        Args:
            proxy: ProxyClient instance
        """
        self._proxy = proxy
    
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

    def set_config(self, config: Dict[str, Any]):
        """
        Set configuration parameters for this input device.
        
        Override this if your device needs runtime configuration.
        
        Args:
            config: Configuration dictionary
        """
        self._config = config

    def get_config(self) -> Dict[str, Any]:
        """
        Get configuration/metadata for this input device.
        
        Override this to provide device-specific information.
        
        Returns:
            Dictionary with device configuration and metadata
        """
        return {
            "active": self._active
        }

    def get_description(self) -> str:
        """
        Get a human-readable description of this input device.
        
        Override this to provide a helpful description.
        
        Returns:
            Description string
        """
        return self.name
