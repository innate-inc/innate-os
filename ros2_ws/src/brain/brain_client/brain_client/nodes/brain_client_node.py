#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Composition root for the brain node.

This file does no behaviour of its own: it declares config, builds the
perception / skills / brain collaborators, wires them together, exposes the ROS
service surface, and spins. The agent loop itself lives in
``brain_client/brain/agent.py``.
"""

from __future__ import annotations

import json
import threading
import time

import rclpy
from brain_messages.srv import ForgetMemory, GetAvailableDirectives, GetChatHistory, ReloadSkillsAgents, ResetBrain
from geometry_msgs.msg import Twist
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger

from brain_client.agents.initializer import initialize_agents
from brain_client.brain.agent import BrainAgent
from brain_client.brain.memory_search import MemorySearch
from brain_client.brain.search_server import MemorySearchServer
from brain_client.brain.transport import pick_rest
from brain_client.brain.utils import EventKind
from brain_client.common.script_paths import get_innate_os_root
from brain_client.core.config import BrainConfig
from brain_client.core.lifecycle import BrainLifecycle
from brain_client.core.state import BrainState
from brain_client.memory.recorder import MemoryRecorder
from brain_client.memory.store import MemoryStore
from brain_client.perception.battery import BatteryMonitor
from brain_client.perception.camera import CameraCapture
from brain_client.perception.gaze_control import GazeController
from brain_client.perception.pose_tracking import PoseTracker
from brain_client.perception.scan_health import ScanHealthMonitor
from brain_client.robot.arm_recovery import ArmRecovery
from brain_client.robot.rest_pose import ArmRestPose
from brain_client.skills.hot_reload import ReloadCoordinator
from brain_client.skills.roster import SkillRoster
from brain_client.skills.runner import PrimitiveRunner
from brain_client.skills.workspace_import import format_load_error, unique_key
from brain_client.transport.chat import ChatManager
from brain_client.transport.tts import TTSHandler

LATCHED_QOS = QoSProfile(
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


class BrainClientNode(Node):
    def __init__(self):
        super().__init__("brain_client_node")

        self.config = BrainConfig.load(self)
        self.state = BrainState(log_everything=self.config.log_everything)
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
        # has no audio device, so the webapp is the speaker.
        self.tts_audio_pub = self.create_publisher(String, "/tts/audio", 10)
        # Live agent state, so clients see a stop/start/directive change made
        # from another device without polling. Latched + heartbeat because
        # bridges (rws) that subscribe after boot miss the latched sample.
        self.agent_status_pub = self.create_publisher(String, "/brain/agent_status", LATCHED_QOS)
        # Brain health for the webapp telemetry widget (topic name kept from the
        # cloud era; the brain is local now, so it reports Gemini readiness).
        self.brain_status_pub = self.create_publisher(String, "/brain/websocket_status", LATCHED_QOS)
        # Deep agent-loop telemetry (JSON) for the webapp's Brain page
        # (webapp/js/brain/): turn lifecycle, tool calls, queue snapshots.
        self.brain_trace_pub = self.create_publisher(String, "/brain/trace", 10)
        self._agent_status_heartbeat = None

        self._proxy = self._init_proxy()
        self._tts_handler = self._init_tts()
        self.add_on_set_parameters_callback(self._on_parameter_change)

        # --- helper node for synchronous service calls (not spun by the executor) ---
        self._service_call_node = rclpy.create_node("brain_client_service_caller")
        self._reload_primitives_client = self._service_call_node.create_client(Trigger, "/brain/reload_primitives")
        self._reload_skills_client = self._service_call_node.create_client(ReloadSkillsAgents, "/brain/reload_skills")

        self._build_collaborators()
        self._create_always_on_subscriptions()
        self._create_services()
        self._startup()

        self.get_logger().info(
            f"\033[1;92m[BrainClient] BrainClientNode initialized (local Gemini brain via {self.brain.backend})\033[0m"
        )

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

    def _on_parameter_change(self, params: list[Parameter]) -> SetParametersResult:
        """Apply the parameters that take effect without a restart.

        Only the TTS voice does: every other field was copied into a collaborator at
        construction, so accepting a write would leave the robot running the old value.
        Those are stored for the next boot, which is what the Settings page's `live`
        flag says about them.
        """
        for param in params:
            if param.name != "cartesia_voice_id":
                continue
            voice_id = str(param.value).strip()
            if not voice_id:
                return SetParametersResult(successful=False, reason="cartesia_voice_id must not be empty")
            if self._tts_handler is None:
                return SetParametersResult(successful=False, reason="TTS is unavailable (no proxy)")
            self._tts_handler.set_voice(voice_id)
        return SetParametersResult(successful=True)

    def _build_collaborators(self) -> None:
        cfg, state = self.config, self.state
        self.chat = ChatManager(self.get_logger(), self.chat_out_pub, self.task_status_pub, self._tts_handler)
        self.camera = CameraCapture(self, cfg)
        self.pose_tracker = PoseTracker(self, odom_topic=cfg.odom_topic, nav_mode_topic=cfg.current_nav_mode_topic)
        self.scan_health = ScanHealthMonitor(self, scan_topic=cfg.scan_topic, stale_after_sec=cfg.scan_stale_after_sec)
        self.battery = BatteryMonitor(self, self.chat, lambda: state.is_brain_active)
        # Spatial memory: the recorder builds it whenever the robot drives well-
        # localized (brain active or not); skills recall over it through the
        # /brain/search_memory action — the agent itself knows nothing of it.
        self.memory_store = MemoryStore(get_innate_os_root() / "data")
        rest = pick_rest(self._proxy)
        self.memory_search = (
            MemorySearch(self.memory_store, rest, model=cfg.gemini_model, logger=self.get_logger())
            if rest is not None
            else None
        )
        self.memory_recorder = MemoryRecorder(
            self,
            cfg,
            store=self.memory_store,
            pose_tracker=self.pose_tracker,
            warm_search=self.memory_search.warm if self.memory_search is not None else None,
            cache_state=self.memory_search.cache_state if self.memory_search is not None else None,
            positions_pub=self.create_publisher(String, "/brain/memory_positions", LATCHED_QOS),
        )
        # Search verdicts for the webapp's map (latched: a page opened after the
        # search still sees the last one; clients gate on the payload's stamp).
        self.memory_search_pub = self.create_publisher(String, "/brain/memory_search", LATCHED_QOS)
        self.memory_search_server = None
        if self.memory_search is not None:
            self.memory_search.on_result = lambda payload: self.memory_search_pub.publish(
                String(data=json.dumps(payload))
            )
            # Recall as a capability: skills reach the same cache-backed search
            # through /brain/search_memory (see brain/search_server.py).
            self.memory_search_server = MemorySearchServer(self.memory_search)
        self.gaze = GazeController(self, state)
        self.runner = PrimitiveRunner(
            self,
            self.chat,
            state,
            stop_robot=self._stop_robot,
            on_task_finished=self.gaze.resume,
        )
        self.roster = SkillRoster(self, state)
        self.brain = BrainAgent(
            self,
            state,
            cfg,
            camera=self.camera,
            pose_tracker=self.pose_tracker,
            runner=self.runner,
            roster=self.roster,
            chat=self.chat,
            gaze=self.gaze,
            proxy=self._proxy,
            scan_health=self.scan_health,
            battery=self.battery,
            trace=lambda payload: self.brain_trace_pub.publish(String(data=payload)),
        )
        # Heavy traces (request bodies, frames — hundreds of KB per turn) are
        # only serialized while a monitor actually subscribes to /brain/trace.
        self.brain.trace_has_audience = lambda: self.brain_trace_pub.get_subscription_count() > 0
        # Terminal skill results and feedback land in the brain's next turn.
        self.runner.on_event = self.brain.on_skill_event
        self.runner.on_feedback = self.brain.on_skill_feedback
        # Scene motion (someone walks in, waves) wakes the brain for an
        # immediate turn instead of waiting out the idle interval. Suppressed
        # while the robot moves itself — a skill running, or the base driven
        # directly (joystick teleop): ego-motion changes every pixel.
        self.camera.motion_suppressed = lambda: self.state.primitive_running is not None or self.camera.recently_driven
        self.camera.on_motion = self._on_camera_motion
        self.arm_recovery = ArmRecovery(self, state, runner=self.runner, chat=self.chat, brain=self.brain)
        self.rest_pose = ArmRestPose(self, state)
        self.lifecycle = BrainLifecycle(
            self,
            state,
            brain=self.brain,
            camera=self.camera,
            pose_tracker=self.pose_tracker,
            runner=self.runner,
            gaze=self.gaze,
            chat=self.chat,
            rest_pose=self.rest_pose,
            active_inputs_pub=self.active_inputs_pub,
            stop_robot=self._stop_robot,
            publish_status=self.publish_agent_status,
        )
        self.reload = ReloadCoordinator(
            self,
            state,
            self.lifecycle,
            self._service_call_node,
            self._reload_primitives_client,
            self._reload_skills_client,
        )

    def _create_always_on_subscriptions(self) -> None:
        self.create_subscription(String, "/brain/chat_in", self._on_chat_in, 10)
        self.create_subscription(String, "/input_manager/custom", self._on_custom_input, 10)
        self.create_subscription(String, "/brain/tts", self._on_tts, 10)
        self.create_subscription(String, "/brain/set_directive", self._on_set_directive, 10)
        self.create_subscription(String, "/brain/set_active_skills", self._on_set_active_skills, 10)
        self.create_subscription(String, "/brain/manual_skill_event", self._on_manual_skill_event, 10)

    def _create_services(self) -> None:
        self.create_service(GetChatHistory, "/brain/get_chat_history", self._svc_get_chat_history)
        self.create_service(SetBool, "/brain/set_logging_config", self._svc_set_logging_config)
        self.create_service(ResetBrain, "/brain/reset_brain", self._svc_reset_brain)
        self.create_service(SetBool, "/brain/set_brain_active", self._svc_set_brain_active)
        self.create_service(Trigger, "/brain/reload", self._svc_reload)
        self.create_service(Trigger, "/brain/clear_memories", self._svc_clear_memories)
        self.create_service(ForgetMemory, "/brain/forget_memory", self._svc_forget_memory)
        self.create_service(ReloadSkillsAgents, "/brain/reload_skills_agents", self._svc_reload_skills_agents)
        self.create_service(GetAvailableDirectives, "/brain/get_available_directives", self._svc_get_directives)

    def _startup(self) -> None:
        # Monotonic: a boot-time NTP step would truncate a wall-clock wait.
        # 60s covers the skills server's first load (torch, Nav2) on the Jetson.
        self.get_logger().info("Waiting for /brain/available_skills topic...")
        deadline = time.monotonic() + 60.0
        while not self.state.registry and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.5)
        if not self.state.registry:
            self.get_logger().warn("No primitives received from /brain/available_skills after 60s")

        # None skips per-agent skill validation: against an empty registry every
        # agent would "fail" it.
        self.state.directives, self.state.current_directive, self.state.broken_agents = initialize_agents(
            self.get_logger(), self.state.registry.primitives or None
        )
        self.state.active_skill_ids = (
            list(self.state.current_directive.skill_ids()) if self.state.current_directive else []
        )
        self.gaze.update()
        self.reload.start_watcher()

        self.publish_agent_status()
        self._agent_status_heartbeat = self.create_timer(3.0, self.publish_agent_status)

    # ================= status =================
    def _stop_robot(self) -> None:
        stop = Twist()
        stop.linear.x = 0.0
        stop.angular.z = 0.0
        self.cmd_vel_pub.publish(stop)

    def publish_agent_status(self) -> None:
        """Broadcast the live agent state (latched + heartbeat, see publisher docs)."""
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
        # Health is live, not just configured: a couple of consecutive failed
        # turns (bad key, network down, model gone) flips the report to
        # "error"/offline instead of staying green while nothing works.
        streak = self.brain.error_streak
        if not self.brain.available:
            state = "invalid_config"
        elif streak >= 2:
            state = "error"
        else:
            state = "ready"
        self.brain_status_pub.publish(
            String(
                data=json.dumps(
                    {
                        "connected": state == "ready",
                        "hosted": False,
                        "state": state,
                        "backend": self.brain.backend,
                        **({"error_streak": streak} if streak else {}),
                    }
                )
            )
        )

    def _on_camera_motion(self) -> None:
        if not self.state.is_brain_active:
            return
        # MOTION lets the brain dashboard show the wake-up cue.
        self.brain.add_event(
            "Motion detected in the camera view — something or someone is moving nearby.", kind=EventKind.MOTION
        )

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
        self.brain.on_user_message(data["text"])
        self.get_logger().info(f"User message: {data['text']}")

    def _on_custom_input(self, msg: String) -> None:
        if not self.state.is_brain_active:
            self.get_logger().warn("[BrainClient] Brain is not active. Skipping custom input.")
            return
        try:
            data = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            self.get_logger().warn("[BrainClient] Ignoring /input_manager/custom: invalid JSON payload.")
            return
        self.get_logger().info(f"Received custom input from {data.get('input_device', 'unknown')}")
        self.brain.on_custom_input(data)

    def _on_tts(self, msg: String) -> None:
        text = msg.data
        if text and text.strip():
            self.get_logger().info(f"TTS request received: {text[:50]}...")
            self.chat.speak(text)

    def _on_set_directive(self, msg: String) -> None:
        self.lifecycle.set_directive(msg.data)

    def _on_set_active_skills(self, msg: String) -> None:
        """Update which skills are enabled for the current directive."""
        try:
            payload = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            self.get_logger().warn("[BrainClient] Ignoring /brain/set_active_skills: invalid JSON payload.")
            return
        if not isinstance(payload, dict):
            self.get_logger().warn("[BrainClient] Ignoring /brain/set_active_skills: payload must be a JSON object.")
            return
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

        unknown_skills = self.roster.set_active_skill_ids(payload.get("skills", []))
        if unknown_skills:
            self.get_logger().warn(
                f"Ignoring unavailable skills for directive '{self.state.current_directive.id}': {unknown_skills}"
            )
        self.get_logger().info(
            f"Active skills for directive '{self.state.current_directive.id}': {self.state.active_skill_ids}"
        )
        self.publish_agent_status()

    def _on_manual_skill_event(self, msg: String) -> None:
        """Record manually-triggered skill runs in the chat and the brain's context."""
        try:
            payload = json.loads(msg.data)
            status = payload["status"]
            skill_id = payload["skill_id"]
        except (json.JSONDecodeError, TypeError, KeyError):
            self.get_logger().warn(
                "[BrainClient] Ignoring /brain/manual_skill_event: expected a JSON object with 'status' and 'skill_id'."
            )
            return
        primitive_name = payload.get("skill_name") or self.state.registry.name_for(skill_id)
        primitive_id = payload.get("primitive_id") or f"manual_{skill_id}_{int(time.time() * 1000)}"
        reason = payload.get("reason")

        self.get_logger().info(f"Manual skill event: {status} {primitive_name} ({skill_id})")
        # The runner mirrors the run into the skill slot (under its slot lock,
        # so a concurrent brain-owned start can't be clobbered or cleared).
        # The RAW payload id goes to the mirror, not the synthesized fallback:
        # a per-message synthesized id would make the terminal's id "mismatch"
        # the running one and wedge the slot shut for id-less publishers.
        self.runner.mirror_manual_event(
            status, primitive_name=primitive_name, primitive_id=payload.get("primitive_id"), skill_id=skill_id
        )
        if self.state.is_brain_active:
            detail = reason or "triggered manually from the app"
            self.brain.add_event(f"The user manually ran skill '{primitive_name}' ({status}): {detail}")
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
                self.lifecycle.activate_brain()
                response.message = "Brain activated."
        else:
            if not self.state.is_brain_active:
                response.message = "Brain is already inactive."
            else:
                self.lifecycle.deactivate_brain()
                response.message = "Brain deactivated."
        response.success = True
        return response

    def _svc_clear_memories(self, request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        cleared = self.memory_store.clear()
        if self.memory_search is not None:
            # Forgetting must reach the server-side copies too: the context
            # cache and the uploaded frames would otherwise keep serving the
            # forgotten views until their own expiry.
            self.memory_search.forget()
        self.get_logger().info(f"[BrainClient] Cleared {cleared} spatial memories on user request")
        response.success = True
        response.message = f"Forgot {cleared} remembered views"
        return response

    def _svc_forget_memory(
        self, request: ForgetMemory.Request, response: ForgetMemory.Response
    ) -> ForgetMemory.Response:
        memory = self.memory_store.forget(request.memory_id, request.fingerprint)
        if memory is None:
            response.success = False
            response.message = "No such memory — the map may have been re-made; refresh and retry"
            return response
        if self.memory_search is not None:
            # Forgetting must reach the server-side copies too (same reason as
            # clear): the frame's upload and the cache embedding it would
            # otherwise keep serving the forgotten view until their own expiry.
            self.memory_search.forget_frame(memory)
        self.get_logger().info(f"[BrainClient] Forgot spatial memory {memory.id} on user request")
        response.success = True
        response.message = f"Forgot the view at x {memory.x:.2f}, y {memory.y:.2f}"
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
        # Load-time broken agents, plus any that pass loading but fail while
        # this response is built — those roster broken too instead of silently
        # dropping out of the picker (the vanishing this field exists to end).
        broken = dict(self.state.broken_agents)
        for agent_id, directive in self.state.directives.items():
            try:
                details.append(
                    {
                        "id": directive.id,
                        "display_name": directive.display_name,
                        "display_icon": directive.display_icon_data,
                        "prompt": directive.get_prompt(),
                        "skills": directive.skill_ids(),
                        "source": getattr(directive, "source", "user"),
                    }
                )
            except Exception as e:  # noqa: BLE001 — one bad agent must not take the roster down
                error = format_load_error(e)
                broken[unique_key(broken, agent_id)] = error
                self.get_logger().error(f"Error reading directive '{agent_id}': {error}")
        response.directives = [
            json.dumps(details),
            json.dumps(
                {
                    "skills": self.state.registry.metadata,
                    "active_skills": self.roster.active_skill_ids(),
                    "brain_active": self.state.is_brain_active,
                    # Agents whose module failed to import or whose class failed
                    # to build — not selectable, shown disabled with the error.
                    # In the meta dict (not the agent list) so clients that
                    # don't know the field never offer them for selection.
                    "broken_agents": [
                        {"id": name, "display_name": name, "load_error": error}
                        for name, error in sorted(broken.items())
                    ],
                }
            ),
        ]
        response.current_directive = self.state.current_directive.id if self.state.current_directive else ""
        return response

    # ================= teardown =================
    def destroy_node(self):
        self.exit_event.set()
        self.brain.shutdown()
        if self.memory_search_server is not None:
            self.memory_search_server.shutdown()
        if self._agent_status_heartbeat is not None and not self._agent_status_heartbeat.is_canceled():
            self._agent_status_heartbeat.cancel()
        self.reload.stop_watcher()
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
