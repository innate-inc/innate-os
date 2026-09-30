# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill roster: track the available skills and the directive's active subset.

Subscribes to the latched ``/brain/available_skills`` topic (published by the
skills server) and rebuilds the shared
:class:`~brain_client.skills.registry.SkillRegistry`. The brain reads the
registry directly each turn, so there is nothing to register anywhere — a
roster change simply shows up in the next turn's tools.
"""

from __future__ import annotations

import json

from brain_messages.msg import AvailableSkills
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.skills.registry import SkillMeta, SkillRegistry

AVAILABLE_SKILLS_QOS = QoSProfile(
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


def registry_from_skills_msg(msg: AvailableSkills, on_duplicate=None) -> SkillRegistry:
    """Build a :class:`SkillRegistry` from an ``AvailableSkills`` message.

    Skills that failed to load are on the roster for the UI only — they are
    left out of the registry so the brain never declares one as a tool.
    """
    metadata: list[SkillMeta] = [
        {
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "group": s.group,
            "guidelines": s.guidelines,
            "guidelines_when_running": s.guidelines_when_running,
            "inputs": json.loads(s.inputs_json) if s.inputs_json else {},
            "in_training": s.in_training,
            "episode_count": s.episode_count,
            "directory": s.directory,
            "wheeled": s.wheeled,
            "module": s.module,
            "class_name": s.class_name,
        }
        for s in msg.skills
        if not s.load_error
    ]
    return SkillRegistry.from_metadata(metadata, on_duplicate=on_duplicate)


class SkillRoster:
    def __init__(self, node, state):
        self._node = node
        self._logger = node.get_logger()
        self._state = state
        self._last_skills: object = None  # last msg.skills, compared field-wise (rosidl __eq__)
        self._sub = node.create_subscription(
            AvailableSkills, "/brain/available_skills", self._on_available_skills, AVAILABLE_SKILLS_QOS
        )

    def _on_available_skills(self, msg: AvailableSkills) -> None:
        # The roster is latched and re-published on a heartbeat so late
        # subscribers (the webapp, via rws) can catch it. Ignore unchanged
        # repeats so each beat doesn't rebuild the registry. Message equality
        # is field-wise (rosidl __eq__), so this can never fall behind the
        # Skill schema the way a hand-written signature tuple would.
        if msg.skills == self._last_skills:
            return
        self._last_skills = msg.skills

        def _warn_dup(name, existing_id, new_id):
            self._logger.warn(f"Duplicate skill name '{name}': ID '{existing_id}' overwritten by '{new_id}'")

        self._state.registry = registry_from_skills_msg(msg, on_duplicate=_warn_dup)

        counts = {t: sum(1 for s in msg.skills if s.type == t) for t in ("code", "learned", "replay")}
        self._logger.info(
            f"Received {len(msg.skills)} skills from topic: "
            f"{counts['code']} code, {counts['learned']} learned, {counts['replay']} replay"
        )

    def available_skill_ids(self) -> list[str]:
        return [p["id"] for p in self._state.registry.metadata]

    def active_skill_ids(self) -> list[str]:
        """The available skills the current directive has enabled, in roster order."""
        if self._state.current_directive is None:
            return []
        current_skill_ids = (
            self._state.active_skill_ids
            if self._state.active_skill_ids is not None
            else list(self._state.current_directive.skill_ids())
        )
        current_skill_set = set(current_skill_ids)
        return [skill_id for skill_id in self.available_skill_ids() if skill_id in current_skill_set]

    def set_active_skill_ids(self, requested_skills: list[str]) -> list[str]:
        """Set the active subset; returns the requested ids that aren't available."""
        available_skill_ids = self.available_skill_ids()
        requested_skill_set = set(requested_skills)
        self._state.active_skill_ids = [skill_id for skill_id in available_skill_ids if skill_id in requested_skill_set]
        return sorted(requested_skill_set - set(available_skill_ids))
