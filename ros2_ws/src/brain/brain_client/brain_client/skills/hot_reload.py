# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Reload coordination: full reload, selective reload, and the hot-reload queue.

Drives PEAS (the skill action server) to reload skill code and reloads agents
locally. The file watcher runs on a background thread and only *queues* work;
the actual reload runs on the ROS executor thread via :meth:`process_queue`
(a node timer callback). The brain picks up the rebuilt registry on its next
turn — there is nothing to re-register.
"""

from __future__ import annotations

import threading
import time

import rclpy
from brain_messages.msg import AvailableSkills
from brain_messages.srv import ReloadSkillsAgents
from rclpy.executors import SingleThreadedExecutor
from std_srvs.srv import Trigger

from brain_client.agents.initializer import initialize_agents
from brain_client.common.ros_services import call_service_sync
from brain_client.common.script_paths import get_agent_directories
from brain_client.skills.hot_reload_watcher import HotReloadWatcher
from brain_client.skills.registry import SkillRegistry
from brain_client.skills.roster import AVAILABLE_SKILLS_QOS, registry_from_skills_msg

# Collapse /brain/reload bursts: a full reload reloads all on-disk state, so one
# that just ran already covers requests arriving within this window.
_RELOAD_COALESCE_SEC = 2.0


class ReloadCoordinator:
    def __init__(self, node, state, lifecycle, service_call_node, reload_primitives_client, reload_skills_client):
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._lifecycle = lifecycle
        self._service_call_node = service_call_node
        self._reload_primitives_client = reload_primitives_client
        self._reload_skills_client = reload_skills_client

        self._pending = None  # (skill_names, agent_names)
        self._lock = threading.Lock()
        self._watcher = None
        self._timer = None
        self._last_full_reload = 0.0  # monotonic time of the last perform_full

    # --- watcher lifecycle ---
    def start_watcher(self) -> None:
        # Agent packages are watched as recursive roots — helper modules,
        # folder agents, and subpackages all trigger — and any .py change
        # means "re-import the agent packages": the import model has no
        # per-file reload (same as the SAS catalog watcher). Skill changes
        # are PEAS/SAS's watcher, not this one.
        self._watcher = HotReloadWatcher(
            logger=self._logger,
            skills_directories=[],  # skill hot reload is handled by PEAS/SAS
            agents_directories=[],
            on_reload=lambda _skills, _agents: self.queue([], ["agents"]),
            debounce_seconds=1.0,
            workspace_roots=[str(p) for p in get_agent_directories()],
        )
        self._watcher.start()
        self._timer = self._node.create_timer(0.5, self.process_queue)

    def stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    # --- queue (watchdog thread -> executor thread) ---
    def queue(self, skill_names: list, agent_names: list) -> None:
        with self._lock:
            if self._pending is not None:
                existing_skills, existing_agents = self._pending
                self._pending = (
                    list(set(existing_skills + skill_names)),
                    list(set(existing_agents + agent_names)),
                )
            else:
                self._pending = (list(skill_names), list(agent_names))
        self._logger.info(f"Queued hot reload: skills={skill_names}, agents={agent_names}")

    def process_queue(self) -> None:
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None:
            return
        skill_names, agent_names = pending
        self._logger.info(f"Processing hot reload: skills={skill_names}, agents={agent_names}")
        try:
            self.perform_selective(skill_names, agent_names)
        except Exception as e:
            self._logger.error(f"Hot reload failed: {e}")

    # --- reload operations ---
    def perform_selective(self, skill_names: list, agent_names: list) -> tuple:
        reloaded_skills: list = []
        reloaded_agents: list = []
        try:
            self._lifecycle.deactivate_brain()

            if skill_names:
                req = ReloadSkillsAgents.Request()
                req.skills = skill_names
                req.agents = []  # PEAS doesn't handle agents
                result = call_service_sync(
                    self._service_call_node,
                    self._logger,
                    self._reload_skills_client,
                    req,
                    "PEAS selective reload",
                    timeout_sec=15.0,
                )
                if result and result.success:
                    reloaded_skills = list(result.reloaded_skills)

            if agent_names:
                # The roster swap is wholesale: an empty roster (every agent
                # now broken) still replaced the previous one. The brain reads
                # the rebuilt state on its next turn — nothing to re-register.
                reloaded_agents = self._reload_agents()

            self._logger.info(
                f"[BrainClient] Selective reload complete: {len(reloaded_skills)} skills, {len(reloaded_agents)} agents"
            )
        except Exception as e:
            self._logger.error(f"[BrainClient] Selective reload failed: {e}")
            raise
        return reloaded_skills, reloaded_agents

    def perform_full(self) -> None:
        now = time.monotonic()
        if now - self._last_full_reload < _RELOAD_COALESCE_SEC:
            self._logger.info("Skipping /brain/reload — a full reload ran recently (coalescing)")
            return
        try:
            self._lifecycle.deactivate_brain()
            self._state.directives = {}
            self._state.broken_agents = {}
            self._state.registry = SkillRegistry()
            self._state.current_directive = None
            self._state.active_skill_ids = []

            call_service_sync(
                self._service_call_node,
                self._logger,
                self._reload_primitives_client,
                Trigger.Request(),
                "PEAS reload",
                timeout_sec=30.0,
                wait_timeout_sec=10.0,
            )

            registry = self._await_available_skills(timeout_sec=5.0)
            if registry is None:
                self._logger.warn("No primitives received from topic after reload")
            else:
                self._state.registry = registry

            # `or None` skips per-agent skill validation against an empty
            # registry (every agent would "fail" it) — same as _startup.
            self._state.directives, self._state.current_directive, self._state.broken_agents = initialize_agents(
                self._logger, self._state.registry.primitives or None
            )
            self._state.active_skill_ids = (
                list(self._state.current_directive.skill_ids()) if self._state.current_directive else []
            )
            self._logger.info(
                f"[BrainClient] Reloaded {len(self._state.registry.primitives)} primitives, "
                f"{len(self._state.directives)} directives"
            )
        except Exception as e:
            self._logger.error(f"[BrainClient] Reload failed: {e}")
        finally:
            # Record even on failure so a persistent error can't drive a reload storm.
            self._last_full_reload = time.monotonic()

    def _await_available_skills(self, timeout_sec: float):
        """Wait for the latched /brain/available_skills sample after a reload.

        perform_full runs inside a service callback on the global executor, so it
        cannot pump the main node from here without re-entering that executor. A
        throwaway node on its own executor receives the latched message
        independently. Returns the rebuilt registry, or None on timeout.
        """
        received: dict = {}
        waiter = rclpy.create_node("brain_client_reload_skills_waiter")
        executor = SingleThreadedExecutor()
        try:

            def on_skills(msg: AvailableSkills) -> None:
                received.setdefault("registry", registry_from_skills_msg(msg))

            waiter.create_subscription(
                AvailableSkills,
                "/brain/available_skills",
                on_skills,
                AVAILABLE_SKILLS_QOS,
            )
            executor.add_node(waiter)
            deadline = time.time() + timeout_sec
            while "registry" not in received and time.time() < deadline:
                executor.spin_once(timeout_sec=0.2)
            return received.get("registry")
        finally:
            executor.remove_node(waiter)
            waiter.destroy_node()

    def _reload_agents(self) -> list:
        """Re-import the agent packages and swap the roster wholesale — the
        import model has no per-file reload (same as the skill catalog).

        The current directive survives by id: its new instance is rebound and
        the active skill set intersected, so a mid-activation edit doesn't widen
        the skills the user had narrowed. If it no longer loads (now broken),
        fall back to the default agent (demo_agent) rather than keep running a
        stale instance whose file no longer says what it does.
        """
        agents, default_agent, broken = initialize_agents(self._logger, self._state.registry.primitives or None)
        current_id = self._state.current_directive.id if self._state.current_directive else None
        self._state.directives = agents
        self._state.broken_agents = broken
        if current_id is not None:
            replacement = agents.get(current_id)
            if replacement is not None:
                self._state.current_directive = replacement
                # None means "never narrowed"; [] is a deliberate everything-off
                # choice and must not re-widen.
                previous_skill_ids = (
                    set(self._state.active_skill_ids)
                    if self._state.active_skill_ids is not None
                    else set(replacement.skill_ids())
                )
                self._state.active_skill_ids = [
                    skill_id for skill_id in replacement.skill_ids() if skill_id in previous_skill_ids
                ]
                self._logger.info(f"Updated current directive: {current_id}")
            else:
                self._logger.warning(f"Current directive '{current_id}' did not survive reload; using default")
                self._state.current_directive = default_agent
                self._state.active_skill_ids = list(default_agent.skill_ids()) if default_agent else []
        return sorted(agents)
