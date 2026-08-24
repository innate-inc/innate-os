#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Agent Type Definitions

Base class and types for robot agents.
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias, Union

from brain_client.common.script_paths import Source

if TYPE_CHECKING:
    from brain_client.inputs.types import InputDevice
    from brain_client.skills.types import Skill, TrainedSkill

# What get_skills() may list: the Skill class itself for code skills, the
# generated TrainedSkill ref for physical skills (both typed — an import
# error or rename is caught by the editor, not at runtime on the robot), or
# an id string.
SkillRef: TypeAlias = Union["type[Skill]", "type[TrainedSkill]", str]

# What get_inputs() may list: the InputDevice class itself (typed, same
# rationale as SkillRef) or a device-name string.
InputRef: TypeAlias = Union["type[InputDevice]", str]


@dataclass(frozen=True)
class TurnIntervals:
    """Optional per-agent overrides for the brain's visual observation cadence.

    ``None`` keeps the global ROS parameter.  A positive value is the pause
    after a completed model turn; model latency is additional.  Keeping this
    on the agent lets a visually supervised navigation agent look frequently
    without making every directive on the robot equally chatty and expensive.
    """

    idle: float | None = None
    supervision: float | None = None

    def __post_init__(self) -> None:
        for name, value in (("idle", self.idle), ("supervision", self.supervision)):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"turn interval {name} must be a finite positive number or None")


@dataclass(frozen=True)
class DepartureGuard:
    """Keep a search moving briefly after recognizing an already-known subject.

    A fast visual supervision loop can otherwise cancel the newly restarted
    search for the same unchanged person every second.  Agents opt in by
    naming the terminal result prefixes that establish an anchor and the
    skills that must be allowed to depart from it.  User speech always bypasses
    the guard.
    """

    trigger_skill_names: tuple[str, ...]
    trigger_result_prefixes: tuple[str, ...]
    protected_skill_ids: tuple[str, ...]
    minimum_departure_m: float
    maximum_hold_s: float

    def __post_init__(self) -> None:
        if not self.trigger_skill_names or not self.trigger_result_prefixes or not self.protected_skill_ids:
            raise ValueError("departure guard trigger names, result prefixes, and protected skills cannot be empty")
        for name, value in (
            ("minimum_departure_m", self.minimum_departure_m),
            ("maximum_hold_s", self.maximum_hold_s),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"departure guard {name} must be a finite positive number")


@dataclass(frozen=True)
class InteractionGuard:
    """Temporarily hide escape skills while a required interaction is unresolved."""

    trigger_skill_names: tuple[str, ...]
    trigger_result_prefixes: tuple[str, ...]
    blocked_skill_ids: tuple[str, ...]
    release_skill_names: tuple[str, ...]
    release_result_prefixes: tuple[str, ...]
    maximum_hold_s: float

    def __post_init__(self) -> None:
        if not all(
            (
                self.trigger_skill_names,
                self.trigger_result_prefixes,
                self.blocked_skill_ids,
                self.release_skill_names,
                self.release_result_prefixes,
            )
        ):
            raise ValueError("interaction guard trigger, blocked, and release fields cannot be empty")
        if not math.isfinite(self.maximum_hold_s) or self.maximum_hold_s <= 0:
            raise ValueError("interaction guard maximum_hold_s must be a finite positive number")


class Agent(ABC):
    """
    Base class for all agents.

    An agent provides personality and behavior guidelines for the robot,
    along with the list of skills that should be available when this
    agent is active.
    """

    # Stamped by the loader to "shipped" or "user" based on origin directory.
    # Subclasses must not set this themselves.
    source: Source = "user"

    # Stamped by the loader: the display_icon file base64-encoded, when the
    # agent declares one and it loads.
    display_icon_data: str | None = None

    # Every subclass registers itself here at definition time, the same model
    # as Skill._registry: defining an Agent in an imported module is what puts
    # it on the roster. Keyed by (module, qualname) so a re-import of the same
    # module replaces its own entries; stale entries are pruned at collect
    # time (see agents/loader.py).
    _registry: "dict[tuple[str, str], type[Agent]]" = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        Agent._registry[(cls.__module__, cls.__qualname__)] = cls

    @property
    @abstractmethod
    def id(self) -> str:
        """
        The name of the directive (used as identifier).
        Must be defined by every subclass.
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """
        The human-readable display name of the directive.
        Must be defined by every subclass.
        """
        pass

    @abstractmethod
    def get_skills(self) -> list[SkillRef]:
        """
        Returns the skills that should be available when this agent is
        active. Prefer classes: the Skill itself for code skills, the
        generated ref (``physical_skills`` package) for physical skills::

            from innate_skills.navigate_to_position import NavigateToPosition
            from physical_skills import PickSocks

            def get_skills(self):
                return [NavigateToPosition, PickSocks]

        Id strings (e.g. "innate-os/navigate_to_position") are equivalent.
        Ids are matched exactly against each available skill's id during
        registration — not by display name.

        Subclasses must implement this method.
        """
        pass

    def skill_ids(self) -> list[str]:
        """get_skills() normalized to id strings — the only form the rest of
        the system (registration, cloud agent, webapp) ever consumes. A class
        resolves through skill_id_for_class, the same derivation the catalog
        uses to id it, so a class reference and its id are interchangeable."""
        # lazy: keeps this module importable without the skill framework
        from brain_client.skills.types import Skill, TrainedSkill
        from brain_client.skills.workspace_import import skill_id_for_class

        ids = []
        for ref in self.get_skills():
            if isinstance(ref, str):
                ids.append(ref)
            elif isinstance(ref, type) and issubclass(ref, TrainedSkill):
                ids.append(ref.skill_id)
            elif isinstance(ref, type) and issubclass(ref, Skill):
                ids.append(skill_id_for_class(ref))
            else:
                raise TypeError(
                    f"{type(self).__name__}.get_skills() entries must be Skill classes, "
                    f"physical_skills refs, or skill-id strings, got {ref!r}"
                )
        return ids

    @abstractmethod
    def get_prompt(self) -> str | None:
        """
        Returns the prompt/description for this directive.
        This defines the robot's personality and behavior guidelines.

        Subclasses must implement this method.
        """
        pass

    @property
    def display_icon(self) -> str | None:
        """
        Optional path to a 32x32 pixel icon asset for this directive.

        Subclasses can override this property to specify an icon.
        Default: return None (no icon).

        Example:
            return "assets/my_directive_icon.png"
        """
        return None

    def get_inputs(self) -> list[InputRef]:
        """
        Returns the input devices that should be active when this agent is
        running. Prefer the InputDevice class over its name string::

            from inputs.micro_input import MicroInput

            def get_inputs(self):
                return [MicroInput]

        Name strings (e.g. "micro") are equivalent; they are matched exactly
        against each device's registered name.

        Subclasses can override this method to specify required inputs.
        Default: return empty list (no input devices required).
        """
        return []

    def get_turn_intervals(self) -> TurnIntervals:
        """Return optional per-agent idle and running-skill turn intervals.

        The global ``brain_client_node`` ROS parameters remain the defaults.
        Override this only when the directive needs a materially different
        visual reaction cadence.
        """
        return TurnIntervals()

    def get_departure_guard(self) -> DepartureGuard | None:
        """Optionally protect a running search from immediate repeat cancellation."""
        return None

    def get_interaction_guard(self) -> InteractionGuard | None:
        """Optionally keep escape skills hidden while an interaction is unresolved."""
        return None

    def input_names(self) -> list[str]:
        """get_inputs() normalized to device-name strings — the only form the
        input manager consumes. A class resolves through input_name_for_class,
        the same derivation the loader registers it under, so a class
        reference and its name are interchangeable."""
        # lazy: keeps this module importable without the input framework
        from brain_client.inputs.types import InputDevice, input_name_for_class

        names = []
        for ref in self.get_inputs():
            if isinstance(ref, str):
                names.append(ref)
            elif isinstance(ref, type) and issubclass(ref, InputDevice):
                names.append(input_name_for_class(ref))
            else:
                raise TypeError(
                    f"{type(self).__name__}.get_inputs() entries must be InputDevice classes "
                    f"or device-name strings, got {ref!r}"
                )
        return names

    def uses_gaze(self) -> bool:
        """
        Whether this agent uses person-tracking gaze.
        When True, the robot will look at detected people during conversation
        and pause gazing during skill execution.

        Subclasses can override to enable gazing.
        Default: False.
        """
        return False
