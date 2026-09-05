# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Public authoring namespace for Innate skills.

Everything a skill file needs, under one import:

    from innate import MainImage, Mobility, Skill
    from innate_skills.gripper_open import GripperOpen

    class WaveAtCamera(Skill):
        \"\"\"Wave the arm at whoever the camera sees.\"\"\"

        mobility: Mobility
        image: MainImage

        def execute(self):
            ...

The class docstring is the agent-facing guidelines, the class name is the
skill name (snake_cased), and everything the skill consumes is declared with
a bare type annotation — the type identifies the feed.

``CAMERAS`` and ``Manipulation.JOINT_NAMES`` enumerate the robot's cameras
and arm joints by name.

One rule covers interfaces, cameras and robot state: annotate what you read.
``battery: Battery``, ``odom: Odometry``, ``pose: Pose``, ``nav_mode: NavMode``, ``lidar: Lidar``,
``arm: Arm``, ``map: Map``, ``joint_states: JointStates``,
``head_position: HeadState``, ``image: MainImage`` / ``WristImage`` /
``DepthMap``, ``mobility: Mobility``, ``head: Head``,
``memory: SpatialMemory`` (recall over the robot's spatial memory). A plain annotation is
guaranteed inside execute() — the server waits for the first value and fails
the run up front if none arrives — so no None guards are needed; ``| None``
(``head: Head | None``) makes it best effort instead, injected when available
and None otherwise. Reading an undeclared feed raises, and your editor flags
it before you ship.

execute() returns the run's result: the message str, or
``SkillOutput(message, data)`` to attach a structured payload for chaining
callers (``image=jpeg_bytes`` attaches an evidence image the agent sees in
the completion event), or None. Call ``self.fail(message)`` to end the run
as a failure.
Callers of other skills get that SkillOutput back — ``out = self.turn(...)``
then ``out.message`` / ``out.data`` / ``out.ok``, with ``out.status`` a
SkillResult enum, never a bare string. (Legacy ``(message, SkillResult)``
tuple returns still work but are deprecated.)

Cancellation is the framework's job, not yours. Use ``self.sleep(seconds)``
instead of ``time.sleep`` and write loops as if cancel didn't exist: every
blocking framework call (``self.sleep``, ``self.wait_for``, sub-skill calls,
interface helpers) raises SkillCancelled the moment a Stop lands, the base
is braked automatically, and the run reports CANCELLED. ``try/finally`` in
execute() is your cleanup hook; ``self.on_cancel(...)`` exists only to
forward a cancel to an external action goal.

:mod:`innate.exceptions` groups every exception a skill raises or catches.

Agents are authored from the same namespace: ``from innate import Agent``,
with ``SkillRef``/``InputRef`` typing what ``get_skills()``/``get_inputs()``
may list.
"""

from typing import TYPE_CHECKING

from brain_client.agents.types import Agent, InputRef, SkillRef
from brain_client.robot.exceptions import ArmFailed, ArmUnhealthy
from brain_client.skills.types import (
    PhysicalSkill,
    Skill,
    SkillCancelled,
    SkillFailed,
    SkillOutput,
    SkillResult,
    SkillReturn,
    TrainedSkill,
    resource,
)
from brain_client.state.arm import Arm
from brain_client.state.battery import Battery
from brain_client.state.head import HeadState
from brain_client.state.image import DepthMap, Image, MainImage, WristImage
from brain_client.state.joint_states import JointStates
from brain_client.state.lidar import Lidar
from brain_client.state.map import Map
from brain_client.state.nav_mode import NavMode
from brain_client.state.odometry import Odometry
from brain_client.state.pose import Pose

CAMERAS: dict[str, type[Image]] = {"main": MainImage, "wrist": WristImage}
"""Camera name → the type to annotate; the main camera also serves ``DepthMap``."""

__all__ = [
    "Agent",
    "Arm",
    "ArmFailed",
    "ArmUnhealthy",
    "CAMERAS",
    "PhysicalSkill",
    "Battery",
    "DepthMap",
    "Head",
    "HeadState",
    "Image",
    "InputRef",
    "JointStates",
    "Lidar",
    "MainImage",
    "Manipulation",
    "Map",
    "Mobility",
    "NavMode",
    "Odometry",
    "Pose",
    "RecallVerdict",
    "Skill",
    "SkillCancelled",
    "SkillFailed",
    "SkillOutput",
    "SkillRef",
    "SkillResult",
    "SkillReturn",
    "SpatialMemory",
    "TrainedSkill",
    "Waypoint",
    "WristImage",
    "resource",
]

# The interface classes pull ROS/Nav2 modules, so they resolve lazily
# (PEP 562): `from innate import Mobility` imports them on first use only.
# Type checkers can't follow __getattr__, so they read the imports below.
if TYPE_CHECKING:
    from brain_client.robot.head import Head
    from brain_client.robot.manipulation import Manipulation, Waypoint
    from brain_client.robot.mobility import Mobility
    from brain_client.robot.spatial_memory import RecallVerdict, SpatialMemory

_LAZY_INTERFACES = {
    "Mobility": ("brain_client.robot.mobility", "Mobility"),
    "Manipulation": ("brain_client.robot.manipulation", "Manipulation"),
    "Head": ("brain_client.robot.head", "Head"),
    "Waypoint": ("brain_client.robot.manipulation", "Waypoint"),
    "SpatialMemory": ("brain_client.robot.spatial_memory", "SpatialMemory"),
    "RecallVerdict": ("brain_client.robot.spatial_memory", "RecallVerdict"),
}


def __getattr__(name: str):
    target = _LAZY_INTERFACES.get(name)
    if target is None:
        raise AttributeError(f"module 'innate' has no attribute {name!r}")
    module_name, class_name = target
    import importlib

    return getattr(importlib.import_module(module_name), class_name)
