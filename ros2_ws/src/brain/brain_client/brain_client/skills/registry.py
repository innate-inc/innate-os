# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill registry — pure name<->id bookkeeping, no ROS. The single source of
truth for resolving cloud/LLM skill references (ids or display names)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypedDict


class _SkillMetaRequired(TypedDict):
    id: str
    name: str


class SkillMeta(_SkillMetaRequired, total=False):
    """One skill's metadata, as parsed from the skills server's roster message.

    Deliberately a dict, not a dataclass: it arrives as JSON (the
    ``AvailableSkills`` message), is republished as JSON to the webapp, and is
    handed to workspace agents — a class would need converting at every one of
    those boundaries.
    """

    type: str
    group: str
    guidelines: str
    guidelines_when_running: str
    inputs: dict[str, dict]
    in_training: bool
    episode_count: int
    directory: str
    wheeled: bool
    module: str  # import path of the class an agent file names for this skill; "" when none
    class_name: str


@dataclass
class SkillRegistry:
    """Immutable-ish view of the currently available skills.

    ``primitives`` maps skill id to its metadata; ``metadata`` is the ordered
    list used for registration.
    """

    primitives: dict[str, SkillMeta] = field(default_factory=dict)
    metadata: list[SkillMeta] = field(default_factory=list)
    name_to_id: dict[str, str] = field(default_factory=dict)
    id_to_name: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_metadata(
        cls, metadata_list: list[SkillMeta], on_duplicate: Callable[[str, str, str], None] | None = None
    ) -> SkillRegistry:
        """Build from metadata dicts (each with at least id + name);
        ``on_duplicate(name, existing_id, new_id)`` fires on name clashes
        (later wins)."""
        primitives: dict[str, SkillMeta] = {}
        name_to_id: dict[str, str] = {}
        id_to_name: dict[str, str] = {}

        for meta in metadata_list:
            skill_id = meta["id"]
            name = meta["name"]
            primitives[skill_id] = meta
            if name in name_to_id and on_duplicate is not None:
                on_duplicate(name, name_to_id[name], skill_id)
            name_to_id[name] = skill_id
            id_to_name[skill_id] = name

        return cls(
            primitives=primitives,
            metadata=list(metadata_list),
            name_to_id=name_to_id,
            id_to_name=id_to_name,
        )

    def resolve_skill_id(self, type_or_name: str) -> str | None:
        """Resolve a task ``type`` (an id) or a skill name to a skill id.

        Resolution order is id-first then name, applied consistently everywhere
        (the old node checked these in different orders at different call sites).
        Returns None if neither matches.
        """
        if type_or_name in self.primitives:
            return type_or_name
        return self.name_to_id.get(type_or_name)

    def name_for(self, skill_id: str) -> str:
        """Cosmetic name for a skill id, falling back to the id itself."""
        return self.id_to_name.get(skill_id, skill_id)

    def __bool__(self) -> bool:
        return bool(self.primitives)
