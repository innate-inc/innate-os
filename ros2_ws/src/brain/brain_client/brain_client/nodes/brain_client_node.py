#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Composition root for the brain client node.

This file does no behaviour of its own: it declares config, builds the perception /
skills / transport / core collaborators, wires them together, exposes the ROS service
surface, and spins. All logic lives in the concept modules under ``brain_client/``.
"""

from __future__ import annotations

import json
import threading
import time

import rclpy
from brain_messages.srv import GetAvailableDirectives, GetChatHistory, ReloadSkillsAgents, ResetBrain
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger

from brain_client.agents.initializer import initialize_agents
from brain_client.core.config import BrainConfig
from brain_client.core.lifecycle import BrainLifecycle
from brain_client.core.orchestrator import Orchestrator
from brain_client.core.state import BrainState
from brain_client.core.vision_output import VisionOutputHandler
from brain_client.navigation.map import MapState
from brain_client.perception.camera import CameraCapture
from brain_client.perception.gaze_control import GazeController
from brain_client.perception.pose_tracking import PoseTracker
from brain_client.perception.scan_health import ScanHealthMonitor
from brain_client.skills.hot_reload import ReloadCoordinator
from brain_client.skills.lifecycle import primitive_lifecycle_message
from brain_client.skills.registration import SkillCatalog
from brain_client.skills.runner import PrimitiveRunner
from brain_client.transport.chat import ChatManager
from brain_client.transport.messages import MessageIn, MessageInType, MessageOutType
from brain_client.transport.tts import TTSHandler
from brain_client.transport.websocket import WSBridge
from brain_client.transport.ws_manager import WebSocketManager


class BrainClientNode(Node):
    def __init__(self):
        super().__init__("brain_client_node")

        self.config = BrainConfig.load(self)
        self.state = BrainState(token=self.config.token, log_everything=self.config.log_everything)
        self.exit_event = threading.Event()
        self.get_logger().info(
            f"BrainClient running in {'simulator' if self.config.simulator_mode else 'real robot'} mode"
        )

        # --- publishers (created before TTS, which needs tts_status_pub) ---
        self.cmd_vel_pub = self.create_publisher(Twist, self.config.cmd_vel_topic, 10)
        self.active_inputs_pub = self.create_publisher(String, "/input_manager/active_inputs", 10)
        self.chat_out_pub = self.create_publisher(String, "/brain/chat_out", 10)
        self.task_status_pub = self.create_publisher(String, "/brain/skill_status_update", 10)
        self.tts_status_pub = self.create_publisher(String, "/tts/is_playing", 10)
        # Synthesized speech (base64 WAV) for clients to play. Sim-only: the sim
        # has no audio device, so the webapp is the speaker. On the real robot
        # speech plays through the robot's own speaker and nothing is published
        # here — a client playing it too would double the voice.
        self.tts_audio_pub = self.create_publisher(String, "/tts/audio", 10)
        self.memory_positions_pub = self.create_publisher(String, "/brain/memory_positions", 10)
        # Live agent state, so clients see a stop/start/directive change made
        # from another device without polling get_available_directives (see
        # publish_agent_status for the latched + heartbeat delivery).
        agent_status_qos = QoSProfile(
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE,
        )
        self.agent_status_pub = self.create_publisher(String, "/brain/agent_status", agent_status_qos)
        self._agent_status_heartbeat = None

        self._proxy = self._init_proxy()
        self._tts_handler = self._init_tts()

        # --- transport (WebSocket runs in-process; no separate ws_client node) ---
        # Incoming messages arrive on the socket's asyncio thread and are handed to
        # the executor thread by WSBridge before any handler runs.
        self.declare_parameter("client_version", "")
        self._ws_manager = WebSocketManager(
            self,
            uri=self.config.websocket_uri,
            token=self.config.token,
            client_version=self.get_parameter("client_version").get_parameter_value().string_value,
            on_incoming=None,
        )
        self.ws_bridge = WSBridge(self, transport=self._ws_manager)
        self._ws_manager.on_incoming = self.ws_bridge.enqueue_incoming

        # --- helper node for synchronous service calls (not spun by the executor) ---
        self._service_call_node = rclpy.create_node("brain_client_service_caller")
        self._reload_primitives_client = self._service_call_node.create_client(Trigger, "/brain/reload_primitives")
        self._reload_skills_client = self._service_call_node.create_client(ReloadSkillsAgents, "/brain/reload_skills")

        self._build_collaborators()
        self._register_ws_handlers()
        self._create_always_on_subscriptions()
        self._create_services()
        self._startup()

        self.get_logger().info("\033[1;92m[BrainClient] BrainClientNode initialized\033[0m")

    # ================= construction helpers =================
    def _init_proxy(self):
        from innate_proxy import ProxyClient

        try:
            proxy = ProxyClient(config=self.config.proxy_config)
            if proxy.is_available():
                self.get_logger().info("✅ Proxy client initialized")
                return proxy
            self.get_logger().warning("⚠️ Proxy not configured (check INNATE_PROXY_URL, INNATE_SERVICE_KEY)")
        except Exception as e:
            self.get_logger().warning(f"⚠️ Could not initialize proxy: {e}")
        return None

    def _init_tts(self):
        if self._proxy is None:
            self.get_logger().info("🔇 Text-to-speech disabled (proxy not available)")
            return None
        handler = TTSHandler(
            logger=self.get_logger(),
            proxy=self._proxy,
            tts_status_pub=self.tts_status_pub,
            tts_audio_pub=self.tts_audio_pub,
            simulator_mode=self.config.simulator_mode,
        )
        if handler.is_available():
            self.get_logger().info(f"🗣️ Text-to-speech enabled (voice: {handler.voice_id})")
        else:
            self.get_logger().warning("⚠️ TTS handler created but Cartesia client unavailable")
        return handler

    def _stop_robot(self) -> None:
        stop = Twist()
        stop.linear.x = 0.0
        stop.angular.z = 0.0
        self.cmd_vel_pub.publish(stop)

    def publish_agent_status(self) -> None:
        """Broadcast the live agent state on the latched /brain/agent_status topic.

        Published on every activate/deactivate/directive/active-skills change so
        every client learns about changes made from any other client. Latched for
        late joiners, plus re-emitted by a low-rate heartbeat because bridges
        (rws) that subscribe after boot don't get the latched sample — the same
        reason /brain/available_skills is heartbeated.
        """
        self.agent_status_pub.publish(
            String(
                data=json.dumps(
                    {
                        "brain_active": self.state.is_brain_active,
                        "current_directive": self.state.current_directive.id if self.state.current_directive else "",
                        "active_skills": list(self.state.active_skill_ids or []),
                        # Speech needs the hosted proxy (an Innate service
                        # key); clients gray out their TTS input without it.
                        "tts_available": bool(self._tts_handler is not None and self._tts_handler.is_available()),
                    }
                )
            )
        )

    def _build_collaborators(self) -> None:
        cfg, state = self.config, self.state
        self.chat = ChatManager(self.get_logger(), self.chat_out_pub, self.task_status_pub, self._tts_handler)
        self.camera = CameraCapture(self, cfg)
        self.pose_tracker = PoseTracker(
            self,
            amcl_pose_topic=cfg.amcl_pose_topic,
            odom_topic=cfg.odom_topic,
            nav_mode_topic=cfg.current_nav_mode_topic,
            use_odom_as_amcl_pose=cfg.use_odom_as_amcl_pose,
        )
        self.map_state = MapState(self, cfg.map_topic)
        self.scan_health = ScanHealthMonitor(self, scan_topic=cfg.scan_topic, stale_after_sec=cfg.scan_stale_after_sec)
        self.gaze = GazeController(self, state)
        self.catalog = SkillCatalog(self, self.ws_bridge, state)
        self.runner = PrimitiveRunner(
            self,
            self.ws_bridge,
            self.chat,
            state,
            stop_robot=self._stop_robot,
            on_task_finished=lambda: (self.gaze.resume(), self.catalog.drain_pending_reregistration()),
        )
        self.orchestrator = Orchestrator(
            self,
            state,
            cfg,
            ws_bridge=self.ws_bridge,
            camera=self.camera,
            pose_tracker=self.pose_tracker,
            map_state=self.map_state,
            chat=self.chat,
            catalog=self.catalog,
            active_inputs_pub=self.active_inputs_pub,
            memory_positions_pub=self.memory_positions_pub,
            scan_health=self.scan_health,
        )
        self.lifecycle = BrainLifecycle(
            self,
            state,
            cfg,
            ws_bridge=self.ws_bridge,
            camera=self.camera,
            pose_tracker=self.pose_tracker,
            runner=self.runner,
            gaze=self.gaze,
            chat=self.chat,
            orchestrator=self.orchestrator,
            catalog=self.catalog,
            active_inputs_pub=self.active_inputs_pub,
            stop_robot=self._stop_robot,
            publish_status=self.publish_agent_status,
        )
        # Close the one irreducible cycle: Orchestrator needs the lifecycle that
        # can only be built after it.
        self.orchestrator.set_lifecycle(self.lifecycle)
        self.vision_output = VisionOutputHandler(
            self, state, runner=self.runner, chat=self.chat, gaze=self.gaze, pose_tracker=self.pose_tracker
        )
        self.reload = ReloadCoordinator(
            self,
            state,
            self.lifecycle,
            self.catalog,
            self._service_call_node,
            self._reload_primitives_client,
            self._reload_skills_client,
        )

    def _register_ws_handlers(self) -> None:
        b = self.ws_bridge
        b.register_handler(MessageOutType.READY_FOR_IMAGE, self.orchestrator.handle_ready_for_image)
        b.register_handler(MessageOutType.VISION_AGENT_OUTPUT, self.vision_output.handle_message)
        b.register_handler(MessageOutType.CHAT_OUT, lambda msg: self.chat.emit("robot", msg.payload.get("text", "")))
        b.register_handler(MessageOutType.PRIMITIVES_AND_DIRECTIVE_REGISTERED, self.orchestrator.handle_registered)
        b.register_handler(MessageOutType.MEMORY_POSITIONS, self.orchestrator.handle_memory_positions)

    def _create_always_on_subscriptions(self) -> None:
        self.create_subscription(String, "/brain/chat_in", self._on_chat_in, 10)
        self.create_subscription(String, "/input_manager/custom", self._on_custom_input, 10)
        self.create_subscription(String, "/brain/tts", self._on_tts, 10)
        self.create_subscription(String, "/brain/set_directive", lambda m: self.lifecycle.set_directive(m.data), 10)
        self.create_subscription(String, "/brain/set_active_skills", self._on_set_active_skills, 10)
        self.create_subscription(String, "/brain/manual_skill_event", self._on_manual_skill_event, 10)
        self.create_subscription(String, "/brain/backend_config", self.orchestrator.on_backend_config, 10)
        self.create_subscription(String, "/brain/websocket_status", self.orchestrator.on_ws_status, 10)

    def _create_services(self) -> None:
        self.create_service(GetChatHistory, "/brain/get_chat_history", self._svc_get_chat_history)
        self.create_service(SetBool, "/brain/set_logging_config", self._svc_set_logging_config)
        self.create_service(ResetBrain, "/brain/reset_brain", self._svc_reset_brain)
        self.create_service(SetBool, "/brain/set_brain_active", self._svc_set_brain_active)
        self.create_service(Trigger, "/brain/reload", self._svc_reload)
        self.create_service(ReloadSkillsAgents, "/brain/reload_skills_agents", self._svc_reload_skills_agents)
        self.create_service(GetAvailableDirectives, "/brain/get_available_directives", self._svc_get_directives)

    def _startup(self) -> None:
        # Wait for the first available-skills message (transient_local replays the last).
        self.get_logger().info("Waiting for /brain/available_skills topic...")
        deadline = time.time() + 25.0
        while not self.state.registry and time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.5)
        if not self.state.registry:
            self.get_logger().warn("No primitives received from /brain/available_skills after 25s")

        self.state.directives, self.state.current_directive = initialize_agents(
            self.get_logger(), self.state.registry.primitives
        )
        self.state.active_skill_ids = (
            list(self.state.current_directive.get_skills()) if self.state.current_directive else []
        )
        if self.state.current_directive:
            self.lifecycle.activate_directive_inputs()
        self.gaze.update()

        self.catalog.register()
        self.reload.start_watcher()
        self.lifecycle.start_agent_timer()
        self.orchestrator.start_ready_for_connection_broadcast()
        self.orchestrator.start_initial_active_inputs()

        # First status sample now that directives are initialized; the heartbeat
        # also converges any state change that doesn't publish explicitly (e.g.
        # hot reload swapping the current directive).
        self.publish_agent_status()
        self._agent_status_heartbeat = self.create_timer(3.0, self.publish_agent_status)

    # ================= always-on subscription callbacks =================
    def _on_chat_in(self, msg: String) -> None:
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            self.get_logger().warn("[BrainClient] Ignoring /brain/chat_in: invalid JSON payload.")
            return
        if not isinstance(data, dict) or "text" not in data:
            self.get_logger().warn(
                "[BrainClient] Ignoring /brain/chat_in: payload must be a JSON object with a 'text' field."
            )
            return
        if not self.state.is_brain_active:
            self.get_logger().warn("[BrainClient] Brain is not active. Skipping chat_in message.")
            return
        self.chat.history.append(data)

        payload = {"text": data["text"]}
        if image_b64 := self.camera.latest_image_b64():
            payload["image_b64"] = image_b64
        if nav := self.orchestrator.build_nav_payload():
            payload["depth"] = nav.get("depth")
            payload["map"] = nav.get("map")
            payload["camera_info"] = nav.get("camera_info")
            payload["robot_coords"] = nav.get("robot_coords")
        if "image_b64" in payload:
            self.state.pose_at_image_send = self.pose_tracker.current_pose_xyt()

        self.ws_bridge.send_message(MessageIn(type=MessageInType.CHAT_IN, payload=payload))
        self.get_logger().info(f"Sent MessageIn: {payload['text']}")

    def _on_custom_input(self, msg: String) -> None:
        if not self.state.is_brain_active:
            self.get_logger().warn("[BrainClient] Brain is not active. Skipping custom input.")
            return
        try:
            data = json.loads(msg.data)
            self.get_logger().info(f"Received custom input from {data.get('input_device', 'unknown')}")
            self.ws_bridge.send_message(MessageIn(type=MessageInType.CUSTOM_INPUT, payload=data))
        except Exception as e:
            self.get_logger().error(f"Error processing custom input: {e}")

    def _on_tts(self, msg: String) -> None:
        text = msg.data
        if text and text.strip():
            self.get_logger().info(f"TTS request received: {text[:50]}...")
            self.chat.speak(text)

    def _on_set_active_skills(self, msg: String) -> None:
        """Update which primitives are registered for the current directive."""
        payload = json.loads(msg.data)
        if self.state.current_directive is None:
            self.get_logger().warn("No directive selected; ignoring active skills update")
            return

        requested_agent_id = payload.get("agent_id")
        if requested_agent_id and requested_agent_id != self.state.current_directive.id:
            self.get_logger().warn(
                "Ignoring active skills update for stale directive "
                f"'{requested_agent_id}'; current is '{self.state.current_directive.id}'"
            )
            return

        unknown_skills = self.catalog.set_active_skill_ids(payload.get("skills", []))
        if unknown_skills:
            self.get_logger().warn(
                f"Ignoring unavailable skills for directive '{self.state.current_directive.id}': {unknown_skills}"
            )
        self.get_logger().info(
            f"Active skills for directive '{self.state.current_directive.id}': {self.state.active_skill_ids}"
        )
        self.catalog.register()
        self.publish_agent_status()

    def _skill_name_for_id(self, skill_id: str) -> str:
        for metadata in self.state.registry.metadata:
            if metadata.get("id") == skill_id:
                return str(metadata.get("name") or skill_id)
        return skill_id

    def _on_manual_skill_event(self, msg: String) -> None:
        """Record manually-triggered simulator skill runs in brain/agent history."""
        payload = json.loads(msg.data)
        status = payload["status"]
        skill_id = payload["skill_id"]
        primitive_name = payload.get("skill_name") or self._skill_name_for_id(skill_id)
        primitive_id = payload.get("primitive_id") or f"manual_{skill_id}_{int(time.time() * 1000)}"
        reason = payload.get("reason")

        self.get_logger().info(f"Manual skill event: {status} {primitive_name} ({skill_id})")
        if self.state.is_brain_active:
            self.ws_bridge.send_message(
                primitive_lifecycle_message(
                    status=status,
                    primitive_name=primitive_name,
                    primitive_id=primitive_id,
                    reason=reason,
                )
            )
        self.chat.publish_task_status(
            primitive_name=primitive_name,
            primitive_id=primitive_id,
            status=status,
            skill_id=skill_id,
            reason=reason,
        )
        self.chat.history.append(
            {
                "sender": "task_activated",
                "text": primitive_name,
                "timestamp": payload.get("timestamp", time.time()),
                "taskStatus": status,
                "primitiveId": primitive_id,
                "skillId": skill_id,
                **({"failureReason": reason} if reason else {}),
            }
        )

    # ================= service handlers =================
    def _svc_get_chat_history(self, request, response):
        response.history = self.chat.history_json()
        return response

    def _svc_set_logging_config(self, request, response):
        self.state.log_everything = request.data
        response.success = True
        response.message = f"Logging configuration set: log_everything={self.state.log_everything}"
        return response

    def _svc_reset_brain(self, request, response):
        self.get_logger().info("[BrainClient] Received /brain/reset_brain request")
        self.lifecycle.perform_brain_reset(request.memory_state)
        response.success = True
        return response

    def _svc_set_brain_active(self, request, response):
        if request.data:
            if self.state.is_brain_active:
                response.message = "Brain is already active."
            else:
                self.lifecycle.reactivate_brain()
                response.message = "Brain reactivated and reset initiated."
        else:
            if not self.state.is_brain_active:
                response.message = "Brain is already inactive."
            else:
                self.lifecycle.deactivate_brain()
                response.message = "Brain deactivated."
        response.success = True
        return response

    def _svc_reload(self, request, response):
        self.get_logger().info("[BrainClient] Received /brain/reload request")
        try:
            self.reload.perform_full()
            response.success = True
            response.message = (
                f"Reloaded {len(self.state.registry.primitives)} primitives, {len(self.state.directives)} directives"
            )
        except Exception as e:
            response.success = False
            response.message = f"Reload failed: {e}"
        return response

    def _svc_reload_skills_agents(self, request, response):
        skill_names = list(request.skills) if request.skills else []
        agent_names = list(request.agents) if request.agents else []
        self.get_logger().info(f"[BrainClient] Selective reload: skills={skill_names}, agents={agent_names}")
        try:
            reloaded_skills, reloaded_agents = self.reload.perform_selective(skill_names, agent_names)
            response.success = True
            response.reloaded_skills = reloaded_skills
            response.reloaded_agents = reloaded_agents
            response.message = f"Reloaded {len(reloaded_skills)} skills, {len(reloaded_agents)} agents"
        except Exception as e:
            response.success = False
            response.message = f"Selective reload failed: {e}"
            response.reloaded_skills = []
            response.reloaded_agents = []
        return response

    def _svc_get_directives(self, request, response):
        details = []
        for directive in self.state.directives.values():
            details.append(
                {
                    "id": directive.id,
                    "display_name": directive.display_name,
                    "display_icon": directive.display_icon_data,
                    "prompt": directive.get_prompt(),
                    "skills": directive.get_skills(),
                    "source": getattr(directive, "source", "user"),
                }
            )
        response.directives = [
            json.dumps(details),
            json.dumps(
                {
                    "skills": self.state.registry.metadata,
                    "active_skills": self.catalog.active_skill_ids_for_registration(),
                    "brain_active": self.state.is_brain_active,
                }
            ),
        ]
        response.current_directive = self.state.current_directive.id if self.state.current_directive else ""
        return response

    # ================= teardown =================
    def destroy_node(self):
        self.exit_event.set()
        if self.orchestrator.pose_image_timer and not self.orchestrator.pose_image_timer.is_canceled():
            self.orchestrator.pose_image_timer.cancel()
        if self.lifecycle.agent_timer and not self.lifecycle.agent_timer.is_canceled():
            self.lifecycle.agent_timer.cancel()
        if self._agent_status_heartbeat is not None and not self._agent_status_heartbeat.is_canceled():
            self._agent_status_heartbeat.cancel()
        self.reload.stop_watcher()
        self._ws_manager.shutdown()
        if self._tts_handler is not None:
            self._tts_handler.close()
        self._service_call_node.destroy_node()
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = BrainClientNode()
    try:
        # Manual spin so transient deserialization errors (corrupted CompressedImage,
        # type-hash mismatches) are logged and skipped instead of killing the node.
        while rclpy.ok():
            try:
                timeout = 0.1 if node.state.is_brain_active else 1.0
                rclpy.spin_once(node, timeout_sec=timeout)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if "RCLError" in type(e).__name__:
                    node.get_logger().warn(f"Skipping deserialization error (message dropped): {e}")
                else:
                    raise
    except KeyboardInterrupt:
        node.get_logger().info("KeyboardInterrupt, shutting down.")
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
