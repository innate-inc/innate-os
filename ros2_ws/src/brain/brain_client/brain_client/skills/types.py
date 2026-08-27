# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import inspect
import json
import os
import sys
import threading
import time
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path
from types import GeneratorType, UnionType  # stdlib `types`, not this module
from typing import TYPE_CHECKING, Any, Generic, NoReturn, TypeVar, Union, get_args, get_origin, overload

from rclpy.node import Node
from std_msgs.msg import String
from typing_extensions import Self

from brain_client.common.dynamic_loader import class_name_to_snake_case
from brain_client.common.logging import UniversalLogger
from brain_client.common.script_paths import Source

if TYPE_CHECKING:
    from brain_client.skills.invoker import SkillInvoker

T = TypeVar("T")
_T_resource = TypeVar("_T_resource")

# What execute() may return: the result message (SkillOutput to attach a
# structured payload for chaining callers), or None. Failure is self.fail();
# cancellation is the framework's. Legacy (message, SkillResult[, data])
# tuples still normalize at runtime but are deprecated.
SkillReturn = Union[None, str, "SkillOutput"]

TTS_TOPIC = "/brain/tts"
TTS_STATUS_TOPIC = "/tts/is_playing"


class SkillResult(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class SkillFailed(Exception):
    """A composed skill reported FAILURE. Raised by Skill.__call__ and fail()."""


class SkillCancelled(BaseException):
    """A composed skill was cancelled; unwinds the routine.

    BaseException, not Exception — same reasoning as asyncio.CancelledError:
    a skill's broad ``except Exception`` (around a Gemini call, file IO, a
    chained child) must never swallow a cancellation. Catch it explicitly
    only to clean up, and re-raise.
    """


# The active run's cancel latch. One skill runs at a time (the server's
# execution slot enforces it), so module state is exact: the server swaps
# this around each execute(), and interface helpers block on it so their
# loops unwind on cancel without any plumbing from the skill.
_run_cancel = threading.Event()


def swap_run_cancel(event: "threading.Event | None") -> threading.Event:
    global _run_cancel
    previous = _run_cancel
    _run_cancel = event if event is not None else threading.Event()
    return previous


def cancellable_sleep(seconds: float) -> None:
    """time.sleep that raises SkillCancelled the moment the run is cancelled."""
    if _run_cancel.wait(seconds):
        raise SkillCancelled("cancelled")


class SkillOutput:
    """The result of a skill run: the message, the status (a SkillResult, not
    a string), an optional structured payload for chaining callers, and an
    optional evidence image (JPEG bytes) that travels with the result — the
    agent sees it in the completion event, so a skill can show what it found,
    not just say it (``return SkillOutput("found it", image=jpeg)``).

    ``str(output)`` and f-strings give the message; ``output.ok`` is the
    success check. Unpacking as the legacy ``(message, status)`` tuple still
    works but is deprecated.
    """

    __slots__ = ("message", "data", "status", "image")

    def __init__(
        self, message: str, data: Any = None, status: "SkillResult" = SkillResult.SUCCESS, image: bytes | None = None
    ):
        self.message = str(message)
        self.data = data
        self.status = status
        self.image = image

    @property
    def ok(self) -> bool:
        return self.status is SkillResult.SUCCESS

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        data = f", data={self.data!r}" if self.data is not None else ""
        return f"SkillOutput({self.message!r}, status={self.status.value}{data})"

    def __iter__(self):
        # legacy `message, status = ...` unpacking, from when results were tuples
        _warn_once(
            "unpack",
            "Unpacking a skill result as (message, status) is deprecated — "
            "use output.message / output.status / output.ok.",
        )
        yield self.message
        yield self.status


# Deprecation keys already warned about, once per process.
_deprecation_warned: set[str] = set()


def _warn_once(key: str, message: str, logger=None) -> None:
    if key in _deprecation_warned:
        return
    _deprecation_warned.add(key)
    if logger is not None:
        logger.warning(message)
    else:
        warnings.warn(message, DeprecationWarning, stacklevel=3)


def normalize_skill_result(result, skill_name: str = "Skill", logger=None) -> SkillOutput:
    """Turn any execute() return into a SkillOutput.

    Legacy (message, SkillResult[, data]) tuples still normalize, with a
    once-per-skill deprecation warning; anything else raises SkillFailed so
    the author sees the contract as the run's failure message.
    """
    if result is None:
        return SkillOutput(f"{skill_name} completed")
    if isinstance(result, SkillOutput):
        return result  # forwarded child output: keep .data and .status
    if isinstance(result, str):
        return SkillOutput(result)
    if isinstance(result, (tuple, list)) and len(result) in (2, 3) and isinstance(result[1], SkillResult):
        _warn_once(
            f"return:{skill_name}",
            f"{skill_name}.execute() returned a (message, SkillResult) tuple — deprecated. "
            "Return the message str on success (SkillOutput(message, data) to attach a payload); "
            "call self.fail(message) to fail. Cancellation is the framework's.",
            logger,
        )
        message, status = result[0], result[1]
        data = result[2] if len(result) == 3 else None
        return SkillOutput(message, data, status)
    raise SkillFailed(
        f"{skill_name}.execute() returned {type(result).__name__} — return a str, "
        "SkillOutput(message, data), or None; call self.fail(message) to fail."
    )


class RobotStateType(Enum):
    LAST_MAIN_CAMERA_IMAGE_B64 = "last_main_camera_image_b64"
    LAST_WRIST_CAMERA_IMAGE_B64 = "last_wrist_camera_image_b64"
    LAST_DEPTH_IMAGE = "last_depth_image"
    LAST_ODOM = "last_odom"
    LAST_MAP = "last_map"
    LAST_HEAD_POSITION = "last_head_position"
    LAST_JOINT_STATES = "last_joint_states"
    LAST_BATTERY = "last_battery"
    LAST_POSE = "last_pose"
    LAST_LIDAR = "last_lidar"
    LAST_ARM = "last_arm"


class InterfaceType(Enum):
    MANIPULATION = "manipulation"
    MOBILITY = "mobility"
    HEAD = "head"
    MEMORY = "memory"


class SkillStorage:
    """Persistent per-skill key-value store: a JSON file with dict access."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        data = self._data
        if data is None:
            try:
                data = json.loads(self._path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            self._data = data
        return data

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._load().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._load()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._load()[key] = value
        self._save()

    def __delitem__(self, key: str) -> None:
        del self._load()[key]
        self._save()

    def __contains__(self, key: str) -> bool:
        return key in self._load()


def _storage_dir() -> Path:
    root = Path(os.environ.get("INNATE_OS_ROOT", Path.home() / "innate-os"))
    return root / "workspace" / "skill_storage"


class _Injected:
    """Descriptor plumbing shared by RobotState and Interface: the injected
    value is stored on the instance under a private name, None until set."""

    _attr_prefix = "_injected_"

    def __init__(self, required: bool = False):
        self.required = required
        self._attr_name: str = ""  # set by __set_name__ before any access

    def __set_name__(self, owner: type, name: str):
        self._attr_name = f"{self._attr_prefix}{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return getattr(obj, self._attr_name, None)

    def __set__(self, obj: Any, value: Any):
        setattr(obj, self._attr_name, value)


class RobotState(_Injected):
    """Descriptor behind robot-state declarations (``odom: Odometry``).

    A required (non-``| None``) state is guaranteed before execute(); the run
    fails if no message arrives. Legacy explicit declarations
    (``odom = RobotState(RobotStateType.LAST_ODOM)``) keep the old
    tolerate-None behavior.
    """

    _attr_prefix = "_robot_state_"

    def __init__(self, state_type: RobotStateType, required: bool = False):
        super().__init__(required)
        self.state_type = state_type


class Camera(RobotState):
    """Descriptor behind camera-feed declarations (``image: MainImage``).

    Cameras must be declared: the server starts them per run — frame encoding
    is too expensive to keep warm. A required camera fails the run if no frame
    arrives within the grace; an optional (``| None``) one never delays the
    start — wait in execute() (``self.wait_for(lambda: self.image)``).
    """

    def __init__(self, feed: RobotStateType, required: bool = True):
        if feed not in _camera_feed_keys():
            raise ValueError(
                "Not a camera feed; declare cameras via annotations — image: MainImage / WristImage / DepthMap"
            )
        super().__init__(feed, required=required)


class Interface(_Injected):
    """Descriptor behind interface declarations (``mobility: Mobility``).

    Declaring is requiring: the run fails up front when a declared interface
    is unavailable; ``| None`` makes it best effort instead. Legacy explicit
    declarations (``head = Interface(InterfaceType.HEAD)``) keep the old
    tolerate-None behavior.
    """

    _attr_prefix = "_interface_"

    def __init__(self, interface_type: InterfaceType, required: bool = False):
        super().__init__(required)
        self.interface_type = interface_type


class SubSkill(_Injected):
    """Descriptor behind sub-skill declarations (``gripper_open: GripperOpen``).

    Composition runs the class you see: for each run the child is constructed
    and wired like a root skill (same run node, interfaces, feeds, invoker,
    feedback) and shares the parent's cancel latch, then sits on the attribute
    as a callable — ``self.gripper_open(percent=50)`` raises SkillFailed /
    SkillCancelled instead of returning a status. Override by subclassing the
    parent and re-declaring the attribute with your class — never by name
    shadowing. Note: a shared cancel unwinds children via the latch
    (``cancelled`` / ``check_cancelled``) and fires ``on_cancel`` hooks down
    the wired tree; a child's ``cancel()`` *override* is only invoked when
    that child is the run's root skill.
    """

    _attr_prefix = "_subskill_"

    def __init__(self, skill_class: "type[Skill]"):
        super().__init__(required=True)
        self.skill_class = skill_class


class TrainedSkill:
    """Base of the generated classes in the ``physical_skills`` package — a
    typed handle on one of this robot's physical skills (a trained policy or
    recorded demonstration: data, with no code class of its own).

    Never subclass this by hand; the skill catalog regenerates the package
    whenever the robot's physical skills change. Reference the generated
    class anywhere a skill id goes::

        from physical_skills import PickSocks

        class TidyAgent(Agent):
            def get_skills(self):
                return [NavigateToPosition, PickSocks]

        class TidyUp(Skill):
            pick: PickSocks  # same call shape as a code sub-skill
    """

    skill_id: str = ""

    def __call__(self, *, timeout: float | None = None, **inputs) -> "SkillOutput":
        """Call shape for a declared physical skill (``self.pick(timeout=60)``).

        At runtime a ``pick: PickSocks`` attribute is a ``_BoundPhysicalSkill``;
        this method exists so type checkers see the same contract as calling
        a code sub-skill. Instantiating a ref and calling it directly is not
        supported.
        """
        raise TypeError(
            f"{type(self).__name__} is a typed physical-skill ref — declare it "
            f"on a Skill (``attr: {type(self).__name__}``) and call that attribute"
        )


class _BoundPhysicalSkill:
    """A declared physical skill, bound to the run's invoker at wire time."""

    def __init__(self, skill_id: str, invoker):
        self._skill_id = skill_id
        self._invoker = invoker

    def __call__(self, *, timeout: float | None = None, **inputs) -> "SkillOutput":
        """Same contract as calling a code sub-skill: output on success,
        SkillFailed / SkillCancelled otherwise."""
        output = self._invoker.run(self._skill_id, timeout=timeout, **inputs)
        if output.status is SkillResult.CANCELLED:
            raise SkillCancelled(output.message)
        if not output.ok:
            raise SkillFailed(output.message)
        return output


class PhysicalSkill(_Injected):
    """Declaration for a physical skill — a trained or recorded policy
    (``pick_socks = PhysicalSkill("pick_socks")``).

    Physical skills are data (metadata.json + a checkpoint), so there is no
    class to import and the id is the only handle. Declaring one still puts it
    in the dependency block with everything else, gives the same call shape as
    a code sub-skill (``self.pick_socks(timeout=60)``), and moves an unknown-id
    error to run start instead of mid-routine.
    """

    _attr_prefix = "_physical_skill_"

    def __init__(self, skill_id: "str | type[TrainedSkill]"):
        super().__init__(required=True)
        if isinstance(skill_id, type):
            skill_id = skill_id.skill_id
        self.skill_id = skill_id


class _Resource(Generic[T]):
    """Descriptor behind ``@resource`` — see that decorator for the contract."""

    def __init__(self, factory: Callable[[Any], Iterator[T] | T]):
        self._factory = factory
        self._name = getattr(factory, "__name__", "resource")
        self.__doc__ = factory.__doc__

    def __set_name__(self, owner: type, name: str):
        self._name = name

    @property
    def _gen_key(self) -> str:
        return f"_resource_gen_{self._name}"

    @overload
    def __get__(self, obj: None, objtype: type | None = None) -> Self: ...
    @overload
    def __get__(self, obj: object, objtype: type | None = None) -> T: ...
    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        # non-data descriptor: after the first build the instance-dict entry
        # wins the lookup (cached_property's trick); release() pops to re-arm
        if self._name not in obj.__dict__:
            produced = self._factory(obj)
            if isinstance(produced, GeneratorType):
                value = next(produced)
                if value is None:
                    produced.close()
                    return None  # pyright: ignore[reportReturnType] — factories yielding T | None bind T to the optional
                obj.__dict__[self._gen_key] = produced
            else:
                value = produced
                if value is None:
                    return None  # pyright: ignore[reportReturnType] — factories returning T | None bind T to the optional
            obj.__dict__[self._name] = value
        return obj.__dict__[self._name]

    def release(self, obj, logger) -> None:
        """Resume the factory generator past its yield so its teardown runs."""
        value = obj.__dict__.pop(self._name, None)
        gen = obj.__dict__.pop(self._gen_key, None)
        if value is None or gen is None:
            return
        try:
            next(gen)
        except StopIteration:
            pass
        except (Exception, SkillCancelled) as e:
            # SkillCancelled (a BaseException) included: teardown running
            # after a cancelled run may trip a cancel check, and that must
            # not unwind past the run's finalization (the goal would never
            # terminate and the roster would show the skill running forever).
            if logger is not None:
                logger.error(f"releasing resource '{self._name}' failed: {e}")
        else:
            gen.close()
            if logger is not None:
                logger.error(f"resource '{self._name}': factory yields more than once")


@overload
def resource(factory: Callable[[Any], Iterator[_T_resource]]) -> _Resource[_T_resource]: ...
@overload
def resource(factory: Callable[[Any], _T_resource]) -> _Resource[_T_resource]: ...
def resource(factory: Callable[[Any], Any]) -> _Resource[Any]:
    """An expensive object a skill owns, built on first access and cached for
    the run. A generator factory tears down below its ``yield`` at run end:

        @resource
        def controller(self) -> Iterator[Nav2Controller]:
            c = Nav2Controller(self)
            yield c
            c.destroy()

    A plain ``return`` declares no teardown. Returning or yielding None means
    "unavailable": nothing is cached, and the next access retries.

    Implemented as a function (not a decorator class) so type checkers replace
    the factory method's type with ``_Resource[T]`` / ``T`` on access — a class
    decorator is easy to miss, leaving ``self.foo`` typed as MethodType.
    """
    return _Resource(factory)


@dataclass(frozen=True)
class _FeedSpec:
    """One declarable feed — THE per-feed table; everything below derives
    from these rows. Adding a feed is one row plus its RobotStateType member
    and its getter/subscription in RobotStateProvider."""

    skill_type: type  # the annotation type authors declare
    descriptor: type  # Interface / Camera / RobotState — minted for the annotation
    key: "RobotStateType | InterfaceType"
    author_name: str  # the type name as authors write it (the innate export)
    hints: tuple[str, ...]  # attribute names that suggest this feed (typo help)
    label: str = ""  # states: human-readable name in run-failure messages
    grace_s: float | None = None  # states: required-feed warmup bound; None = default


@cache
def _feed_specs() -> "tuple[_FeedSpec, ...]":
    # imported lazily: the interface classes pull ROS/Nav2 modules
    from brain_client.robot.head import Head
    from brain_client.robot.manipulation import Manipulation
    from brain_client.robot.mobility import Mobility
    from brain_client.robot.spatial_memory import SpatialMemory
    from brain_client.state.arm import Arm
    from brain_client.state.battery import Battery
    from brain_client.state.head import HeadState
    from brain_client.state.image import DepthMap, MainImage, WristImage
    from brain_client.state.joint_states import JointStates
    from brain_client.state.lidar import Lidar
    from brain_client.state.map import Map
    from brain_client.state.odometry import Odometry
    from brain_client.state.pose import Pose

    main = RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64
    wrist = RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64
    return (
        _FeedSpec(Manipulation, Interface, InterfaceType.MANIPULATION, "Manipulation", ("manipulation",)),
        _FeedSpec(Mobility, Interface, InterfaceType.MOBILITY, "Mobility", ("mobility",)),
        _FeedSpec(Head, Interface, InterfaceType.HEAD, "Head", ("head",)),
        _FeedSpec(SpatialMemory, Interface, InterfaceType.MEMORY, "SpatialMemory", ("memory",)),
        # cameras start per run (and sim renders on demand) — longer grace
        _FeedSpec(MainImage, Camera, main, "MainImage", ("image", "main_image"), "main camera", grace_s=3.0),
        _FeedSpec(WristImage, Camera, wrist, "WristImage", ("wrist_image",), "wrist camera", grace_s=3.0),
        _FeedSpec(
            DepthMap,
            Camera,
            RobotStateType.LAST_DEPTH_IMAGE,
            "DepthMap",
            ("depth", "depth_image"),
            "depth camera",
            grace_s=3.0,
        ),
        _FeedSpec(Odometry, RobotState, RobotStateType.LAST_ODOM, "Odometry", ("odom",), "odometry"),
        _FeedSpec(Pose, RobotState, RobotStateType.LAST_POSE, "Pose", ("pose",), "map pose"),
        # battery publishes at ~0.2 Hz — the default grace would always miss it
        _FeedSpec(Battery, RobotState, RobotStateType.LAST_BATTERY, "Battery", ("battery",), "battery", grace_s=6.0),
        _FeedSpec(Lidar, RobotState, RobotStateType.LAST_LIDAR, "Lidar", ("lidar",), "lidar"),
        _FeedSpec(Arm, RobotState, RobotStateType.LAST_ARM, "Arm", ("arm",), "arm pose"),
        _FeedSpec(Map, RobotState, RobotStateType.LAST_MAP, "Map", ("map",), "map"),
        _FeedSpec(
            JointStates, RobotState, RobotStateType.LAST_JOINT_STATES, "JointStates", ("joint_states",), "joint states"
        ),
        _FeedSpec(
            HeadState, RobotState, RobotStateType.LAST_HEAD_POSITION, "HeadState", ("head_position",), "head position"
        ),
    )


@cache
def _feed_types() -> dict:
    """Annotated type -> (descriptor class, feed enum)."""
    return {spec.skill_type: (spec.descriptor, spec.key) for spec in _feed_specs()}


@cache
def _feed_attr_hints() -> "dict[str, str]":
    """Attribute name -> annotation type name, for typo-help messages."""
    return {hint: spec.author_name for spec in _feed_specs() for hint in spec.hints}


@cache
def _state_labels() -> dict:
    return {spec.key: spec.label for spec in _feed_specs() if spec.label}


@cache
def _camera_feed_keys() -> frozenset:
    return frozenset(spec.key for spec in _feed_specs() if spec.descriptor is Camera)


# required-feed warmup bound when the spec row has no override
_DEFAULT_STATE_GRACE_S = 2.0


@cache
def _state_grace_s() -> dict:
    return {spec.key: spec.grace_s for spec in _feed_specs() if spec.grace_s is not None}


def _own_annotations(cls) -> dict:
    """The class's own annotations, string forms resolved (PEP 563).

    ``inspect.get_annotations`` evaluates all-or-nothing; retry per name so
    one typo costs only its own declaration. Entries that still fail stay
    strings and surface through the ``_declaration_issues`` warnings.
    """
    try:
        return inspect.get_annotations(cls, eval_str=True)
    except Exception:
        module = sys.modules.get(getattr(cls, "__module__", ""), None)
        cls_globals = getattr(module, "__dict__", {})
        cls_locals = dict(vars(cls))
        annotations = {}
        for name, annotation in cls.__dict__.get("__annotations__", {}).items():
            if isinstance(annotation, str):
                try:
                    annotation = eval(annotation, cls_globals, cls_locals)  # noqa: S307 — mirrors inspect
                except Exception:
                    pass
            annotations[name] = annotation
        return annotations


def _split_optional(annotation):
    """(base_type, is_optional) — unwraps ``T | None`` / ``Optional[T]``."""
    if get_origin(annotation) in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _materialize_feed_annotations(cls) -> None:
    """Mint the matching descriptor for each bare feed annotation
    (``mobility: Mobility``; ``| None`` = optional). A ``= None`` class
    default (``image: MainImage | None = None`` — a natural authoring idiom)
    counts as an optional declaration, not as opting out of injection.
    Suspicious annotations land on ``cls._declaration_issues`` and the
    loader logs them."""
    issues: list[str] = []
    cls._declaration_issues = issues  # always this class's own, never inherited
    # An annotation with a non-None class value (a legacy explicit descriptor,
    # a real default) is not a bare declaration; `= None` still declares.
    bare = {
        name: annotation
        for name, annotation in _own_annotations(cls).items()
        if name not in cls.__dict__ or cls.__dict__[name] is None
    }
    if not bare:
        return
    feed_types = _feed_types()
    for name, annotation in bare.items():
        if isinstance(annotation, str):
            issues.append(
                f"{cls.__name__}.{name}: annotation {annotation!r} does not resolve — if this is a "
                "feed declaration, import the type; nothing will be injected for it."
            )
            continue
        resolved, optional = _split_optional(annotation)
        # py3.10 quirk: a subscripted generic (`tuple[float, float]`) passes
        # isinstance(..., type) but crashes issubclass — and is never a feed.
        is_class = isinstance(resolved, type) and get_origin(resolved) is None
        if is_class and issubclass(resolved, Skill) and resolved is not Skill:
            if optional:
                # The feed rule (`| None` = best effort) does not extend to
                # composition: a declared sub-skill is always wired and its
                # requirements gate the run. Say so, or the author only finds
                # out when the parent fails up front on the child's feeds.
                issues.append(
                    f"{cls.__name__}.{name}: `| None` has no effect on a sub-skill declaration — "
                    f"{resolved.__name__} is always wired and required. For a best-effort dependency, "
                    "dispatch by id at run time (self.skills.run(...))."
                )
            descriptor = SubSkill(resolved)
            setattr(cls, name, descriptor)
            descriptor.__set_name__(cls, name)
            continue
        if is_class and issubclass(resolved, TrainedSkill) and resolved is not TrainedSkill:
            # a generated physical-skill ref: same declaration shape as a code
            # sub-skill, materialized as the PhysicalSkill descriptor
            if optional:
                issues.append(
                    f"{cls.__name__}.{name}: `| None` has no effect on a physical-skill declaration — "
                    f"{resolved.__name__} is always required. For a best-effort dependency, "
                    "dispatch by id at run time (self.skills.run(...))."
                )
            descriptor = PhysicalSkill(resolved.skill_id)
            setattr(cls, name, descriptor)
            descriptor.__set_name__(cls, name)
            continue
        if resolved is Skill:
            issues.append(
                f"{cls.__name__}.{name}: annotate with a concrete Skill subclass to compose it "
                "(`gripper_open: GripperOpen`); bare `Skill` declares nothing."
            )
            continue
        entry = feed_types.get(resolved) if is_class else None
        if entry is None:
            hint = _feed_attr_hints().get(name)
            if hint is not None:
                resolved_name = getattr(resolved, "__name__", repr(resolved))
                issues.append(
                    f"{cls.__name__}.{name}: annotated with {resolved_name}, which is not a feed "
                    f"type — did you mean `{name}: {hint}`? Nothing will be injected for it."
                )
            continue
        descriptor_cls, feed = entry
        # a `= None` default reads as "may be None" even without `| None`
        descriptor = descriptor_cls(feed, required=not optional and name not in cls.__dict__)
        setattr(cls, name, descriptor)
        # setattr after class creation skips the implicit __set_name__ hook
        descriptor.__set_name__(cls, name)


def _index_feed_declarations(cls) -> None:
    """Precompute the class's ``{name: descriptor}`` maps (first definition
    in MRO wins) — update_robot_state runs at 50 Hz, so the MRO walk happens
    once here and the hot path reads a dict."""
    states: dict[str, RobotState] = {}
    interfaces: dict[str, Interface] = {}
    subskills: dict[str, SubSkill] = {}
    physicals: dict[str, PhysicalSkill] = {}
    for klass in cls.__mro__:
        for name, attr in vars(klass).items():
            if isinstance(attr, RobotState) and name not in states:
                states[name] = attr
            elif isinstance(attr, Interface) and name not in interfaces:
                interfaces[name] = attr
            elif isinstance(attr, SubSkill) and name not in subskills:
                subskills[name] = attr
            elif isinstance(attr, PhysicalSkill) and name not in physicals:
                physicals[name] = attr
    cls._feed_states = states
    cls._feed_interfaces = interfaces
    cls._feed_subskills = subskills
    cls._feed_physical_skills = physicals


class Skill(ABC):
    # Stamped by the loader to "shipped" or "user" based on origin directory.
    source: Source = "user"

    # Every subclass registers itself here at definition time, PyTorch-style:
    # defining a Skill is what makes the robot know it — no file scanning.
    # Keyed by (module, qualname) so a re-import of the same module replaces
    # its own entries. Abstract/private classes are filtered at *collect*
    # time (registered_skills in workspace_import.py), not here —
    # __abstractmethods__ is not populated yet when __init_subclass__ runs.
    _registry: "dict[tuple[str, str], type[Skill]]" = {}

    # Precomputed by __init_subclass__ (see _index_feed_declarations); the
    # base class itself declares nothing.
    _feed_states: "dict[str, RobotState]" = {}
    _feed_interfaces: "dict[str, Interface]" = {}
    _feed_subskills: "dict[str, SubSkill]" = {}
    _feed_physical_skills: "dict[str, PhysicalSkill]" = {}

    def __init__(self, logger):
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self.node: Node | None = None
        self._feedback_callback = None
        self._cancel_latch()
        # injected by the server before each run (see invoker.py)
        self.skills: SkillInvoker | None = None
        self._say_publisher = None
        self._tts_status_sub = None
        self._tts_playing = None  # last /tts/is_playing value
        self._storage = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _materialize_feed_annotations(cls)
        _index_feed_declarations(cls)
        Skill._registry[(cls.__module__, cls.__qualname__)] = cls

    # hidden from type checkers on purpose: a visible __getattr__ makes every
    # attribute legal, so typos would only fail on the robot
    if not TYPE_CHECKING:

        def __getattr__(self, name: str):
            hint = _feed_attr_hints().get(name)
            if hint is not None:
                raise AttributeError(
                    f"'{type(self).__name__}' has no '{name}': declare it on the class with a type "
                    f"annotation — `{name}: {hint}` (append ` | None` to tolerate it being unavailable)."
                )
            raise AttributeError(f"'{type(self).__name__}' object has no attribute {name!r}")

    @property
    def name(self) -> str:
        """The snake_case class name, which must equal the filename stem.
        Override only when the two legitimately differ."""
        return class_name_to_snake_case(type(self).__name__)

    @abstractmethod
    def execute(self, *args, **kwargs) -> SkillReturn:
        pass

    def __call__(self, **inputs) -> SkillOutput:
        """Run this skill as a step of the caller: composition's call form.

        PyTorch's __call__/forward split: authors implement execute(); calling
        the instance runs it with the invoker's contract — success returns the
        SkillOutput, FAILURE raises SkillFailed, CANCELLED raises
        SkillCancelled. Only meaningful on a wired instance (a declared
        sub-skill, or the run root the server built).
        """
        # on_cancel hooks registered during this call expire with it: a wired
        # sub-skill instance is reused across calls, so without the truncation
        # every execute()'s `self.on_cancel(self._stop)` would stack up — N
        # calls, N firings on one cancel — and a hook capturing per-call state
        # (a goal handle) would fire long after its run ended.
        hooks = vars(self).setdefault("_cancel_hooks", [])
        registered_before_call = len(hooks)
        try:
            result = self.execute(**inputs)
        finally:
            del hooks[registered_before_call:]
        output = normalize_skill_result(result, self.name, logger=self.logger)
        if output.status is SkillResult.CANCELLED:
            raise SkillCancelled(output.message)
        if not output.ok:
            raise SkillFailed(output.message)
        return output

    def wire_subskills(self, wire_child: "Callable[[type[Skill]], Skill]", _seen: frozenset = frozenset()) -> None:
        """Construct and attach every declared sub-skill, recursively.

        ``wire_child`` builds a fully wired instance of a class (run node,
        interfaces, invoker, feedback) — the server supplies it. This method
        owns what composition adds: sharing the parent's cancel latch so one
        cancel unwinds the whole tree, recursion, and the cycle guard.
        Cycles can't normally be declared (an annotation needs the class
        object, which a circular import prevents), so the guard is defensive.
        """
        for name, physical in self._feed_physical_skills.items():
            if self.skills is None:
                raise RuntimeError(
                    f"{type(self).__name__}.{name} declares physical skill "
                    f"'{physical.skill_id}' but no invoker is available"
                )
            if self.skills.find(physical.skill_id) is None:
                raise RuntimeError(
                    f"{type(self).__name__}.{name}: no skill with id '{physical.skill_id}' — "
                    "check the name, or the policy may still be in training"
                )
            setattr(self, name, _BoundPhysicalSkill(physical.skill_id, self.skills))
        seen = _seen | {type(self)}
        for name, descriptor in self._feed_subskills.items():
            if descriptor.skill_class in seen:
                raise RuntimeError(
                    f"Skill composition cycle: {descriptor.skill_class.__name__} is declared by its own descendant"
                )
            child = wire_child(descriptor.skill_class)
            vars(child)["_cancel_event"] = self._cancel_latch()
            child.wire_subskills(wire_child, seen)
            setattr(self, name, child)

    def _wired_children(self) -> "list[Skill]":
        return [child for name in self._feed_subskills if (child := getattr(self, name, None)) is not None]

    def fail(self, message: str) -> NoReturn:
        """End the run as a FAILURE with ``message``."""
        raise SkillFailed(message)

    def _cancel_latch(self) -> threading.Event:
        # created lazily — some skills skip super().__init__()
        latch = vars(self).get("_cancel_event")
        if latch is None:
            latch = vars(self).setdefault("_cancel_event", threading.Event())
        return latch

    @property
    def cancelled(self) -> bool:
        """True once cancellation was requested for the run."""
        return self._cancel_latch().is_set()

    # Legacy write-shim only — read via `cancelled`. It latches True and
    # ignores False: fleet skills reset `self._cancelled = False` at execute()
    # entry, which would wipe a cancel that raced goal startup.
    @property
    def _cancelled(self) -> bool:
        return self._cancel_latch().is_set()

    @_cancelled.setter
    def _cancelled(self, value: bool):
        if value:
            self._cancel_latch().set()

    def sleep(self, seconds: float) -> None:
        """time.sleep for skill code: wakes and raises SkillCancelled the
        moment a cancel lands. Sleeping is the only cancel point a loop
        needs — write the loop as if cancel didn't exist."""
        if self._cancel_latch().wait(seconds):
            raise SkillCancelled(f"{self.name} cancelled")

    def check_cancelled(self) -> None:
        """Raise SkillCancelled if a cancel landed — for the rare checkpoint
        that has no sleep (e.g. right before an irreversible commit)."""
        if self._cancelled:
            raise SkillCancelled(f"{self.name} cancelled")

    def wait_for(self, read: Callable[[], T | None], timeout: float = 2.0, poll: float = 0.02) -> T | None:
        """Block until ``read()`` returns non-None (or ``timeout`` passes ->
        None). Raises SkillCancelled if the run is cancelled while waiting."""
        deadline = time.monotonic() + timeout
        while True:
            value = read()
            if value is not None:
                return value
            if time.monotonic() >= deadline:
                return None
            self.sleep(poll)

    def on_cancel(self, callback) -> None:
        """Register a zero-arg hook fired (on the cancelling thread) the
        moment a cancel lands. Rarely needed: braking the base is automatic
        (see ``_halt_interfaces``) — use this only to forward the cancel to
        an external goal (``self.on_cancel(handle.cancel_goal_async)``).
        A hook registered inside a composed call (``self.child(...)``) lives
        only for that call (see ``__call__``); on a run root it lives for
        the run."""
        vars(self).setdefault("_cancel_hooks", []).append(callback)

    def _begin_run(self, goal_handle=None):
        """Server hook: latch a cancel that landed before the run started."""
        try:
            if goal_handle is not None and goal_handle.is_cancel_requested:
                self._cancel_latch().set()
        except Exception:
            pass  # duck-typed handles without cancel status

    def cancel(self) -> None:
        """Latch self.cancelled, fire the on_cancel hooks, halt motion, and
        stop any running child. Override only when a hook can't express the
        teardown — and if you chain children, call self.skills.cancel() too."""
        self._cancel_latch().set()
        self._fire_cancel_hooks()
        self._halt_interfaces()
        skills = getattr(self, "skills", None)
        if skills is not None:
            skills.cancel()

    def _halt_interfaces(self) -> None:
        """Call halt() on every injected interface that has one, children
        included — the framework brake: the server fires this on cancel and
        again at run end, so commanded motion never outlives a run and
        skills don't write brake hooks or trailing stops."""
        for name in self._feed_interfaces:
            halt = getattr(getattr(self, name, None), "halt", None)
            if halt is None:
                continue
            try:
                halt()
            except Exception as e:
                self.logger.error(f"[{self.name}] halting {name} failed: {e}")
        for child in self._wired_children():
            child._halt_interfaces()

    def _fire_cancel_hooks(self) -> None:
        """Fire this instance's on_cancel hooks, then the wired children's —
        a composed child registers its brake hook (`self.on_cancel(self._stop)`)
        on ITSELF, and only the root's cancel() is ever invoked, so without
        the walk a child mid-`self.move(...)` would coast until its next
        cancelled poll instead of braking immediately."""
        for hook in list(vars(self).get("_cancel_hooks", [])):
            try:
                hook()
            except (Exception, SkillCancelled) as e:
                # SkillCancelled is a BaseException, so it must be named here:
                # a hook that calls a composed child during a cancel raises it
                # (the shared latch is already set), and it must not abort the
                # remaining hooks or unwind the server's cancel dispatch.
                self.logger.error(f"[{self.name}] on_cancel hook failed: {e}")
        for child in self._wired_children():
            child._fire_cancel_hooks()

    def shutdown(self):
        """Release every ``@resource`` this instance built, at run end.
        Entities on ``self.node`` need no cleanup here — the run's throwaway
        node is destroyed wholesale right after this returns (#497)."""
        for cls in type(self).__mro__:
            for attr in vars(cls).values():
                if isinstance(attr, _Resource):
                    attr.release(self, getattr(self, "logger", None))
        for child in self._wired_children():
            child.shutdown()

    @property
    def storage(self) -> SkillStorage:
        """Persistent per-skill key-value store (survives restarts)."""
        if self._storage is None:
            self._storage = SkillStorage(_storage_dir() / f"{self.name}.json")
        return self._storage

    def say(self, text: str, wait: bool = False) -> None:
        """Speak through the robot's voice; ``wait=True`` blocks until
        playback ends (best effort). No-op if speech isn't available."""
        if not text or self.node is None:
            return
        if self._say_publisher is None:
            self._say_publisher = self.node.create_publisher(String, TTS_TOPIC, 10)
            # fresh publisher every run — wait briefly for the TTS engine to
            # match, or the run's first utterance is dropped. A cancel skips
            # the wait: dropped speech beats a delayed Stop.
            deadline = time.time() + 1.0
            while self._say_publisher.get_subscription_count() == 0 and time.time() < deadline:
                if self.cancelled:
                    break
                time.sleep(0.02)
        if wait and self._tts_status_sub is None:
            self._tts_status_sub = self.node.create_subscription(String, TTS_STATUS_TOPIC, self._on_tts_status, 10)
        self._say_publisher.publish(String(data=text))
        if wait:
            self._wait_for_speech_end(text)

    def _on_tts_status(self, msg: String) -> None:
        self._tts_playing = msg.data

    def _wait_for_speech_end(self, text: str) -> None:
        # A cancel abandons the wait (best effort, like the rest of say():
        # no raise — teardown paths speak after the latch is set). Without
        # the check a Stop would ride out the full budget below.
        # if playback never starts (TTS off, muted), don't hang the skill
        deadline = time.monotonic() + 15.0
        while self._tts_playing != "true":
            if self.cancelled or time.monotonic() > deadline:
                return
            time.sleep(0.05)
        # finish budget scales with utterance length
        deadline = time.monotonic() + max(30.0, 0.1 * len(text))
        while self._tts_playing == "true" and time.monotonic() < deadline:
            if self.cancelled:
                return
            time.sleep(0.05)

    def update_robot_state(self, **kwargs):
        for name, descriptor in self._feed_states.items():
            state_key = descriptor.state_type.value
            if state_key in kwargs:
                setattr(self, name, kwargs[state_key])
        for child in self._wired_children():
            child.update_robot_state(**kwargs)

    def declared_robot_state_types(self) -> list[RobotStateType]:
        """Every declared state feed, required or optional, sub-skills included."""
        types = [desc.state_type for desc in self._feed_states.values()]
        for child in self._wired_children():
            types.extend(child.declared_robot_state_types())
        return list(dict.fromkeys(types))

    def declared_interface_types(self) -> list[InterfaceType]:
        """Every declared interface, required or optional."""
        return [desc.interface_type for desc in self._feed_interfaces.values()]

    def inject_interface(self, interface_type: InterfaceType, interface_instance):
        for name, descriptor in self._feed_interfaces.items():
            if descriptor.interface_type == interface_type:
                setattr(self, name, interface_instance)
                return True
        return False

    def missing_required_interfaces(self) -> list[str]:
        """Declared required interfaces still None — the server fails the run
        up front when non-empty."""
        missing = [
            descriptor.interface_type.value
            for name, descriptor in self._feed_interfaces.items()
            if descriptor.required and getattr(self, name) is None
        ]
        for child in self._wired_children():
            missing.extend(child.missing_required_interfaces())
        return list(dict.fromkeys(missing))

    def required_robot_state_types(self) -> "list[RobotStateType]":
        types = [d.state_type for d in self._feed_states.values() if d.required]
        for child in self._wired_children():
            types.extend(child.required_robot_state_types())
        return list(dict.fromkeys(types))

    def missing_required_robot_states(self) -> list[str]:
        """Labels of declared required states still None — the server fails
        the run up front when non-empty after the warmup wait."""
        missing = [
            _state_labels().get(descriptor.state_type, descriptor.state_type.value)
            for name, descriptor in self._feed_states.items()
            if descriptor.required and getattr(self, name) is None
        ]
        for child in self._wired_children():
            missing.extend(child.missing_required_robot_states())
        return list(dict.fromkeys(missing))

    def describe_feeds(self) -> str:
        """Every declared feed for load-time logs, ``?`` marking optional:
        ``mobility, image, battery?``."""
        parts = [
            name + ("" if descriptor.required else "?")
            for group in (self._feed_interfaces, self._feed_states)
            for name, descriptor in group.items()
        ]
        return ", ".join(parts)

    def guidelines(self) -> str | None:
        """What the agent reads to decide when to call this skill; defaults
        to the class docstring."""
        doc = type(self).__dict__.get("__doc__")
        return inspect.cleandoc(doc) if doc else None

    def guidelines_when_running(self) -> str | None:
        return None

    def feedback(self, message: str, image_b64: str | None = None) -> None:
        """Stream a progress update to whoever launched the skill."""
        self.logger.info(f"Skill feedback [{self.name}]: {message}")
        if self._feedback_callback:
            try:
                self._feedback_callback(message, image_b64)
            except Exception as e:
                self.logger.error(f"Error sending feedback for skill {self.name}: {e}")

    def _send_feedback(self, message: str, image_b64: str | None = None) -> None:
        """Deprecated pre-#542 name for feedback(), kept for existing workspace skills."""
        self.feedback(message, image_b64)

    def set_feedback_callback(self, callback: Callable[[str, str | None], None]) -> None:
        self._feedback_callback = callback
        self.logger.debug(f"Feedback callback set for skill {self.name}.")
