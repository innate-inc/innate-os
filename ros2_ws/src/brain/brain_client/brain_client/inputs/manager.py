# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Input-device management: load, route data, activate/deactivate, TTS ducking.

Bridges ROS topics to the pure-Python input devices. Input authors implement the
:class:`~brain_client.inputs.types.InputDevice` interface and never touch ROS; this
manager loads them, routes their data to the brain's topics, and opens/closes them
based on the active-inputs list. The node (`nodes/input_manager.py`) only owns the
pubs/subs/service and delegates here.
"""

from __future__ import annotations

import base64
import json
import time
from collections import deque
from typing import Any

from std_msgs.msg import String

from brain_client.common.logging import UniversalLogger
from brain_client.common.script_paths import get_input_directories
from brain_client.inputs.loader import InputLoader
from brain_client.inputs.types import InputDevice

MIC_DEVICE_NAME = "micro"


class InputDeviceManager:
    def __init__(self, node, proxy, *, chat_in_pub, custom_pub, barge_in_pub=None):
        self._node = node
        self._logger = UniversalLogger(enabled=True, wrapped_logger=node.get_logger())
        self._chat_in_pub = chat_in_pub
        self._custom_pub = custom_pub
        self._barge_in_pub = barge_in_pub
        self.input_devices: dict[str, InputDevice] = {}
        self._joint_hist: deque = deque()
        self._mic_enabled = True
        self._requested_inputs: set[str] = set()
        self._load(proxy)

    def _load(self, proxy) -> None:
        input_directories = [str(p) for p in get_input_directories()]

        loader = InputLoader(self._node.get_logger(), proxy=proxy)
        input_classes = loader.load_from_directories(input_directories)
        self.input_devices = loader.create_input_instances(input_classes, self._node.get_logger())

        if not self.input_devices:
            self._logger.warning("⚠️ No input devices found!")
        else:
            self._logger.info(f"✅ Loaded {len(self.input_devices)} input devices: {list(self.input_devices.keys())}")
            if MIC_DEVICE_NAME not in self.input_devices:
                self._logger.warning(
                    f"⚠️ No input device named '{MIC_DEVICE_NAME}' — the microphone toggle will not gate anything"
                )

        for device in self.input_devices.values():
            device.set_data_callback(self._handle_device_data)
            device.set_node(self._node)

        for name, device in self.input_devices.items():
            try:
                if device.initialize():
                    self._logger.info(f"✅ Input device '{name}' initialized")
                else:
                    self._logger.error(f"❌ Input device '{name}' failed to initialize")
            except Exception as e:
                self._logger.error(f"❌ Error initializing input device '{name}': {e}")

    # --- device -> brain data routing ---
    def _handle_device_data(self, device_name: str, data: Any, data_type: str) -> None:
        """Publish data an input device wants to send to the agent."""
        try:
            text = data if isinstance(data, str) else data.get("text", "")
            if data_type == "chat_in":
                data_dict = {"text": text, "sender": "user", "timestamp": time.time()}
            else:
                data_dict = {"text": data} if isinstance(data, str) else data.copy()
                data_dict["input_device"] = device_name

            msg = String(data=json.dumps(data_dict))
            if data_type == "chat_in":
                self._chat_in_pub.publish(msg)
                self._logger.debug(f"📤 Published brain/chat_in from '{device_name}': {text[:50]}")
            elif data_type == "custom":
                self._custom_pub.publish(msg)
                self._logger.debug(f"📤 Published custom data from '{device_name}'")
            elif data_type == "barge_in":
                if self._barge_in_pub is not None:
                    self._barge_in_pub.publish(msg)
                    self._logger.info(f"🙋 Published barge-in from '{device_name}': {msg.data[:100]}")
            else:
                self._logger.warning(
                    f"Unknown data type '{data_type}' from device '{device_name}'. Use 'chat_in' or 'custom'."
                )
        except Exception as e:
            self._logger.error(f"Error handling data from device '{device_name}': {e}")

    # --- activation ---
    def _apply(self, name: str, device: InputDevice, should_be_active: bool) -> bool:
        """Open/close one device. Returns False if its hook raised."""
        try:
            was_active = device.is_active()
            if should_be_active and not was_active:
                device.set_active(True)
                device.on_open()
                self._logger.info(f"🔌 Opened input device: {name}")
            elif not should_be_active and was_active:
                device.on_close()
                device.set_active(False)
                self._logger.info(f"💤 Closed input device: {name}")
            return True
        except Exception as e:
            if should_be_active:
                # Roll back the flag: a failed open left as "active" would make
                # every later open attempt a no-op (stuck half-open until restart).
                device.set_active(False)
            self._logger.error(f"Error {'opening' if should_be_active else 'closing'} input device '{name}': {e}")
            return False

    def _is_allowed(self, name: str) -> bool:
        return self._mic_enabled or name != MIC_DEVICE_NAME

    def handle_active_inputs(self, raw: str) -> None:
        """Open/close devices to match an active-inputs message: {"inputs": [...]}."""
        self._logger.debug(f"📥 Received active_inputs message: {raw}")
        try:
            required_inputs = json.loads(raw).get("inputs", [])
            self._logger.debug(f"🎯 Processing inputs: {required_inputs}")
            self._requested_inputs = set(required_inputs)
            for name, device in self.input_devices.items():
                self._apply(name, device, name in self._requested_inputs and self._is_allowed(name))
        except Exception as e:
            self._logger.error(f"Error handling active inputs: {e}")

    def set_all_active(self, active: bool) -> tuple[bool, str]:
        """Activate or deactivate every device (manual control). Returns (ok, msg)."""
        self._requested_inputs = set(self.input_devices) if active else set()
        failed = [
            name
            for name, device in self.input_devices.items()
            if not self._apply(name, device, active and self._is_allowed(name))
        ]
        if failed:
            return False, f"Failed to {'activate' if active else 'deactivate'} inputs: {', '.join(failed)}"
        return True, f"All inputs {'activated' if active else 'deactivated'}"

    def set_mic_enabled(self, enabled: bool) -> None:
        """Apply the robot-level microphone toggle."""
        if enabled != self._mic_enabled:
            self._logger.info(f"🎙️ Microphone {'enabled' if enabled else 'disabled'}")
        # Track the user's intent unconditionally (it gates handle_active_inputs
        # via _is_allowed), and re-apply even when unchanged: _apply is a no-op
        # when state already matches, so a re-publish of the same value retries
        # a previously failed open/close instead of being swallowed.
        self._mic_enabled = enabled
        device = self.input_devices.get(MIC_DEVICE_NAME)
        if device is not None:
            self._apply(MIC_DEVICE_NAME, device, enabled and MIC_DEVICE_NAME in self._requested_inputs)

    def handle_tts_status(self, raw: str) -> None:
        """Notify ducking-capable devices when the robot starts/stops speaking."""
        try:
            is_playing = raw.lower() in ("true", "1", "playing")
            for device in self.input_devices.values():
                if hasattr(device, "set_tts_playing"):
                    device.set_tts_playing(is_playing)
        except Exception as e:
            self._logger.error(f"Error handling TTS status: {e}")

    def handle_joint_state(self, positions) -> None:
        """Flag servo motion to interested devices (mic noise gating).

        Called at /joint_states rate (~200 Hz); compares against the position
        ~100 ms ago so slow gaze adjustments register but encoder jitter
        doesn't.
        """
        try:
            now = time.monotonic()
            pos = list(positions)
            self._joint_hist.append((now, pos))
            while self._joint_hist and now - self._joint_hist[0][0] > 0.12:
                self._joint_hist.popleft()
            old = self._joint_hist[0][1]
            if len(old) != len(pos):
                return
            if max(abs(a - b) for a, b in zip(pos, old, strict=True)) > 0.005:
                for device in self.input_devices.values():
                    if hasattr(device, "note_servo_motion"):
                        device.note_servo_motion()
        except Exception:
            pass  # 200 Hz path; never let it log-spam

    def handle_tts_ref(self, raw: str) -> None:
        """Route a /tts/ref_audio event (JSON, pcm base64) to interested devices."""
        try:
            event = json.loads(raw)
            if "pcm" in event:
                event["pcm"] = base64.b64decode(event["pcm"])
            for device in self.input_devices.values():
                if hasattr(device, "feed_tts_ref"):
                    device.feed_tts_ref(event)
        except Exception as e:
            self._logger.error(f"Error handling TTS ref audio: {e}")

    def shutdown(self) -> None:
        """Close active devices and shut them all down."""
        self._logger.info("Shutting down input devices...")
        for name, device in self.input_devices.items():
            try:
                if device.is_active():
                    device.on_close()
                device.shutdown()
                self._logger.info(f"✅ Shut down input device '{name}'")
            except Exception as e:
                self._logger.error(f"Error shutting down input device '{name}': {e}")
