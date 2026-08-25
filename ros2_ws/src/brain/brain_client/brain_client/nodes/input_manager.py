#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Composition root for the input manager node.

Owns the proxy config, the pubs/subs/service surface, and an
:class:`~brain_client.inputs.manager.InputDeviceManager` that does the actual
device loading, data routing, and activation. No device logic lives here.
"""

from __future__ import annotations

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile
from std_msgs.msg import Bool, String
from std_srvs.srv import SetBool

from brain_client.common.logging import UniversalLogger
from brain_client.inputs.batch_stt import DEFAULT_KEYTERMS
from brain_client.inputs.manager import InputDeviceManager
from innate_proxy import ProxyClient


class InputManagerNode(Node):
    def __init__(self):
        super().__init__("input_manager_node")
        self.logger = UniversalLogger(enabled=True, wrapped_logger=self.get_logger())
        self.logger.info("🔌 Starting Input Manager Node...")

        proxy = self._init_proxy()

        self.chat_in_pub = self.create_publisher(String, "/brain/chat_in", 10)
        self.custom_pub = self.create_publisher(String, "/input_manager/custom", 10)
        self.telemetry_pub = self.create_publisher(String, "/input_manager/telemetry", 10)
        self.manager = InputDeviceManager(
            self, proxy, chat_in_pub=self.chat_in_pub, custom_pub=self.custom_pub, telemetry_pub=self.telemetry_pub
        )

        self.create_subscription(String, "/input_manager/active_inputs", self._on_active_inputs, 10)
        self.create_subscription(String, "/tts/is_playing", self._on_tts_status, 10)
        # A teleop operator talking through the robot's speaker (mars_cam webrtc talkback) is a voice the
        # robot is emitting, not one it is hearing — duck the mic for it exactly as for its own speech.
        self.create_subscription(String, "/talkback/is_playing", self._on_talkback_status, 10)
        self.create_service(SetBool, "/input_manager/set_input_active", self._svc_set_input_active)

        mic_state_qos = QoSProfile(depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(Bool, "/microphone_enabled", self._on_mic_enabled, mic_state_qos)

        self.logger.info("✅ Input Manager Node started successfully")

    def _init_proxy(self):
        # Credentials come from env (INNATE_PROXY_URL, INNATE_SERVICE_KEY); config from params.
        self.declare_parameter("stt_backend", "elevenlabs_batch")
        self.declare_parameter("stt_vad_engine", "silero")
        self.declare_parameter("stt_language", "en")
        # Batch backends only; an empty list disables biasing and its ElevenLabs surcharge.
        self.declare_parameter("stt_keyterms", list(DEFAULT_KEYTERMS))
        # The batch (silero) and realtime (vendor) VAD knobs are separate on
        # purpose: silero's speech probability and the vendors' sensitivity
        # scales are unrelated, so one number cannot tune both.
        self.declare_parameter("stt_vad_threshold", 0.2)
        self.declare_parameter("stt_vad_silence_secs", 0.5)
        self.declare_parameter("stt_realtime_vad_threshold", 0.3)
        self.declare_parameter("stt_realtime_vad_silence_secs", 0.7)
        self.declare_parameter("stt_energy_threshold", 0.01)
        self.declare_parameter("elevenlabs_batch_stt_model", "scribe_v2")
        self.declare_parameter("gemini_stt_model", "gemini-3.6-flash")
        self.declare_parameter("elevenlabs_stt_model", "scribe_v2_realtime")
        self.declare_parameter("openai_realtime_model", "gpt-4o-realtime-preview")
        self.declare_parameter("openai_realtime_url", "wss://api.openai.com/v1/realtime")
        self.declare_parameter("openai_transcribe_model", "gpt-4o-mini-transcribe")
        self.declare_parameter("cartesia_voice_id", "9fdaae0b-f885-4813-b589-3c07cf9d5fea")
        proxy_config = {
            "stt_backend": self.get_parameter("stt_backend").value,
            "stt_vad_engine": self.get_parameter("stt_vad_engine").value,
            "stt_language": self.get_parameter("stt_language").value,
            "stt_keyterms": self.get_parameter("stt_keyterms").value,
            "stt_vad_threshold": self.get_parameter("stt_vad_threshold").value,
            "stt_vad_silence_secs": self.get_parameter("stt_vad_silence_secs").value,
            "stt_realtime_vad_threshold": self.get_parameter("stt_realtime_vad_threshold").value,
            "stt_realtime_vad_silence_secs": self.get_parameter("stt_realtime_vad_silence_secs").value,
            "stt_energy_threshold": self.get_parameter("stt_energy_threshold").value,
            "elevenlabs_batch_stt_model": self.get_parameter("elevenlabs_batch_stt_model").value,
            "gemini_stt_model": self.get_parameter("gemini_stt_model").value,
            "elevenlabs_stt_model": self.get_parameter("elevenlabs_stt_model").value,
            "openai_realtime_model": self.get_parameter("openai_realtime_model").value,
            "openai_realtime_url": self.get_parameter("openai_realtime_url").value,
            "openai_transcribe_model": self.get_parameter("openai_transcribe_model").value,
            "cartesia_voice_id": self.get_parameter("cartesia_voice_id").value,
        }
        try:
            proxy = ProxyClient(config=proxy_config)
        except Exception as e:
            self.logger.warning(f"⚠️ Could not initialize proxy client: {e}")
            return None
        if proxy.is_available():
            self.logger.info(f"✅ Proxy client initialized (URL: {proxy.proxy_url[:30]}...)")
        else:
            # Returned anyway: it still carries the settings above, and the
            # devices that can run on a direct key (Gemini) need them.
            self.logger.warning("⚠️ Proxy not configured - only direct-key backends will work")
        return proxy

    def _on_active_inputs(self, msg: String) -> None:
        self.manager.handle_active_inputs(msg.data)

    def _on_tts_status(self, msg: String) -> None:
        self.manager.handle_speaker_status("tts", msg.data)

    def _on_talkback_status(self, msg: String) -> None:
        self.manager.handle_speaker_status("talkback", msg.data)

    def _on_mic_enabled(self, msg: Bool) -> None:
        self.manager.set_mic_enabled(msg.data)

    def _svc_set_input_active(self, request, response):
        response.success, response.message = self.manager.set_all_active(request.data)
        return response

    def destroy_node(self):
        self.manager.shutdown()
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = InputManagerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        # Guard against double-shutdown on signal teardown (matches the other nodes).
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
