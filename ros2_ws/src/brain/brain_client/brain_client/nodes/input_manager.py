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
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool, String
from std_srvs.srv import SetBool

from brain_client.common.logging import UniversalLogger
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
        self.barge_in_pub = self.create_publisher(String, "/barge_in", 10)
        self.manager = InputDeviceManager(
            self,
            proxy,
            chat_in_pub=self.chat_in_pub,
            custom_pub=self.custom_pub,
            barge_in_pub=self.barge_in_pub,
        )

        self.create_subscription(String, "/input_manager/active_inputs", self._on_active_inputs, 10)
        self.create_subscription(String, "/tts/is_playing", self._on_tts_status, 10)
        self.create_subscription(String, "/tts/ref_audio", self._on_tts_ref, 50)
        # Servo motion is loud at the gripper mic (measured -23 dBFS vs -50
        # floor) and is not in the TTS reference; barge-in must ignore it.
        self.create_subscription(JointState, "/joint_states", self._on_joint_state, 10)
        self.create_service(SetBool, "/input_manager/set_input_active", self._svc_set_input_active)

        mic_state_qos = QoSProfile(depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        self.create_subscription(Bool, "/microphone_enabled", self._on_mic_enabled, mic_state_qos)

        self.logger.info("✅ Input Manager Node started successfully")

    def _init_proxy(self):
        # Credentials come from env (INNATE_PROXY_URL, INNATE_SERVICE_KEY); config from params.
        self.declare_parameter("openai_realtime_model", "gpt-4o-realtime-preview")
        self.declare_parameter("openai_realtime_url", "wss://api.openai.com/v1/realtime")
        self.declare_parameter("openai_transcribe_model", "gpt-4o-mini-transcribe")
        self.declare_parameter("cartesia_voice_id", "9fdaae0b-f885-4813-b589-3c07cf9d5fea")
        self.declare_parameter("barge_in_enabled", True)
        self.declare_parameter("barge_in_threshold_db", 6.0)
        self.declare_parameter("barge_in_min_ms", 150)
        self.declare_parameter("barge_in_flush_tail", False)
        self.declare_parameter("barge_in_model_path", "")
        self.declare_parameter("barge_in_reverb_decay", 0.87)
        self.declare_parameter("barge_in_debug_dir", "")
        proxy_config = {
            "openai_realtime_model": self.get_parameter("openai_realtime_model").value,
            "openai_realtime_url": self.get_parameter("openai_realtime_url").value,
            "openai_transcribe_model": self.get_parameter("openai_transcribe_model").value,
            "cartesia_voice_id": self.get_parameter("cartesia_voice_id").value,
            "barge_in_enabled": self.get_parameter("barge_in_enabled").value,
            "barge_in_threshold_db": self.get_parameter("barge_in_threshold_db").value,
            "barge_in_min_ms": self.get_parameter("barge_in_min_ms").value,
            "barge_in_flush_tail": self.get_parameter("barge_in_flush_tail").value,
            "barge_in_model_path": self.get_parameter("barge_in_model_path").value,
            "barge_in_reverb_decay": self.get_parameter("barge_in_reverb_decay").value,
            "barge_in_debug_dir": self.get_parameter("barge_in_debug_dir").value,
        }
        try:
            proxy = ProxyClient(config=proxy_config)
            if proxy.is_available():
                self.logger.info(f"✅ Proxy client initialized (URL: {proxy.proxy_url[:30]}...)")
                return proxy
            self.logger.warning("⚠️ Proxy not configured - input devices won't have proxy access")
        except Exception as e:
            self.logger.warning(f"⚠️ Could not initialize proxy client: {e}")
        return None

    def _on_active_inputs(self, msg: String) -> None:
        self.manager.handle_active_inputs(msg.data)

    def _on_tts_status(self, msg: String) -> None:
        self.manager.handle_tts_status(msg.data)

    def _on_tts_ref(self, msg: String) -> None:
        self.manager.handle_tts_ref(msg.data)

    def _on_joint_state(self, msg: JointState) -> None:
        self.manager.handle_joint_state(msg.position)

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
