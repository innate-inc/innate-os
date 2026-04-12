"""
Prompt construction for agent_codegen.

Dynamically loads the Agent/Skill ABC interfaces and existing examples from the
caller's filesystem so generated code matches the live codebase.  Both inputs
have safe fallbacks when the caller does not supply paths or the files are missing.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent interface — embedded fallback (verbatim copy of agent_types.Agent)
# ---------------------------------------------------------------------------

_AGENT_INTERFACE_FALLBACK = '''\
#!/usr/bin/env python3
"""Agent Type Definitions — Base class for robot agents."""
from abc import ABC, abstractmethod
from typing import List, Optional


class Agent(ABC):
    """Base class for all agents.

    Provides personality and behavior guidelines for the robot,
    along with the list of skills active when this agent is running.
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """Snake-case identifier. Must be defined by every subclass."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable display name. Must be defined by every subclass."""
        pass

    @abstractmethod
    def get_skills(self) -> List[str]:
        """List of skill names available when this agent is active."""
        pass

    @abstractmethod
    def get_prompt(self) -> Optional[str]:
        """System prompt defining the robot\'s personality and behavior."""
        pass

    @property
    def display_icon(self) -> Optional[str]:
        """Optional path to a 32x32 pixel icon. Default: None."""
        return None

    def get_inputs(self) -> List[str]:
        """Input device names active while this agent runs. Default: []."""
        return []

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation. Default: False."""
        return False

    def get_routing_description(self) -> Optional[str]:
        """Short text for AgentOrchestrator routing. Default: None."""
        return None
'''

# ---------------------------------------------------------------------------
# Skill interface — embedded fallback (key parts of skill_types.py)
# ---------------------------------------------------------------------------

_SKILL_INTERFACE_FALLBACK = '''\
"""Skill Type Definitions — Base class for robot skills."""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional


class SkillResult(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class InterfaceType(Enum):
    MANIPULATION = "manipulation"
    MOBILITY = "mobility"
    HEAD = "head"


class RobotStateType(Enum):
    LAST_MAIN_CAMERA_IMAGE_B64 = "last_main_camera_image_b64"
    LAST_WRIST_CAMERA_IMAGE_B64 = "last_wrist_camera_image_b64"
    LAST_ODOM = "last_odom"
    LAST_MAP = "last_map"
    LAST_HEAD_POSITION = "last_head_position"


class Interface:
    """Descriptor for declaring hardware interface dependencies.

    Declare at class level; the runtime injects the real object before execute().
    Always check for None before use in case the interface is unavailable.

    Example:
        class MySkill(Skill):
            head = Interface(InterfaceType.HEAD)

            def execute(self, **kwargs):
                if self.head is None:
                    return "Head interface not available", SkillResult.FAILURE
                self.head.move(...)
    """
    def __init__(self, interface_type: InterfaceType): ...


class RobotState:
    """Descriptor for declaring robot state dependencies.

    Example:
        class MySkill(Skill):
            image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)
    """
    def __init__(self, state_type: RobotStateType): ...


class Skill(ABC):
    def __init__(self, logger):
        self.logger = logger      # Use self.logger.info/warning/error
        self.node = None          # ROS2 node — may be None; check before use

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique snake_case skill identifier. Must match the spec key exactly."""
        ...

    @abstractmethod
    def execute(self, *args, **kwargs) -> tuple[str, SkillResult]:
        """Execute the skill.

        Returns:
            (result_message: str, status: SkillResult)

        Always accept **kwargs so the LLM can call with arbitrary parameters.
        """
        ...

    @abstractmethod
    def cancel(self) -> str:
        """Cancel execution gracefully. Returns a cancellation message."""
        ...

    def guidelines(self) -> Optional[str]:
        """Usage guidelines shown to the LLM. Override to provide context."""
        return None

    def guidelines_when_running(self) -> Optional[str]:
        """Guidelines while the skill is running. Override if needed."""
        return None

    def _send_feedback(self, message: str, image_b64: str = None):
        """Send progress feedback during long execute() calls."""
        ...
'''

# ---------------------------------------------------------------------------
# Interface API reference — compact signatures for the three hardware interfaces.
# Used as fallback when the live interface files cannot be read.
# ---------------------------------------------------------------------------

_INTERFACE_API_FALLBACK = '''\
# ManipulationInterface  (self.manipulation)
#   move_to_cartesian_pose(x, y, z, roll=0.0, pitch=0.0, yaw=0.0, duration=3, blocking=False) -> bool
#     Move end-effector to Cartesian pose (metres, radians). Returns True on success.
#     pitch=math.pi/2 → end-effector pointing straight down.
#   move_cartesian_trajectory(poses, segment_duration=1.0, segment_durations=None) -> bool
#     Smooth trajectory through a list of pose dicts [{x,y,z,roll,pitch,yaw}, ...].
#   get_current_end_effector_pose() -> dict | None
#     Returns {"position": {x,y,z}, "orientation": {x,y,z,w}} or None.
#   open_gripper(percent=100.0, duration=0.5, blocking=False) -> bool
#   close_gripper(strength=0.0, duration=0.5, blocking=False) -> bool
#   GRIPPER_OPEN = 0.85  (radians)   GRIPPER_CLOSED = 0.0  (radians)

# MobilityInterface  (self.mobility)
#   send_cmd_vel(linear_x=0.0, angular_z=0.0, duration=None) -> None
#     Publish velocity command. If duration>0 a stop is auto-sent after that many seconds.
#   rotate(angle_radians) -> bool
#     Precise blocking rotation via Nav2. Positive = counter-clockwise.
#   rotate_in_place(angular_speed, duration) -> None
#     Non-blocking rotation for a fixed duration then auto-stops.

# HeadInterface  (self.head)
#   set_position(angle_degrees: int) -> None
#     Tilt head. Typical range: -25 (looking down) to +15 (looking up).
'''

# Files excluded from style examples (meta-files, utilities, internal helpers).
_AGENT_SKIP_FILENAMES = {"__init__.py", "orchestrator_agent.py"}
_SKILL_SKIP_FILENAMES = {"__init__.py", "arm_utils.py"}


# ---------------------------------------------------------------------------
# Agent prompt helpers
# ---------------------------------------------------------------------------


def load_agent_interface(agent_types_path: Optional[str] = None) -> str:
    """Return the Agent ABC source to embed in the prompt.

    If *agent_types_path* is given and readable its full text is returned.
    Falls back to :data:`_AGENT_INTERFACE_FALLBACK` on any error.
    """
    if agent_types_path is not None:
        try:
            text = Path(agent_types_path).read_text(encoding="utf-8")
            _LOG.debug("Loaded agent interface from %s (%d chars)", agent_types_path, len(text))
            return text
        except OSError as exc:
            _LOG.warning("Cannot read agent_types_path %r: %s — using fallback", agent_types_path, exc)
    return _AGENT_INTERFACE_FALLBACK


def load_agent_examples(
    agents_dir: Optional[str],
    max_examples: int = 2,
) -> list[tuple[str, str]]:
    """Read up to *max_examples* Python agent files from *agents_dir*.

    Selects the shortest files first.  Skips ``__init__.py``, ``_*`` files,
    and ``orchestrator_agent.py``.  Returns ``[]`` when *agents_dir* is ``None``
    or cannot be read.
    """
    return _load_examples(agents_dir, _AGENT_SKIP_FILENAMES, max_examples)


def build_prompt(
    agent_id: str,
    agent_spec: dict,
    *,
    agent_interface: str,
    agent_examples: list[tuple[str, str]],
    existing_skills: list[tuple[str, str]] = (),
) -> str:
    """Assemble the user prompt for agent generation."""
    spec_json = json.dumps(agent_spec, indent=2)
    examples_section = _format_examples(agent_examples)
    existing_skills_section = _format_existing_skills(existing_skills)
    class_name = "".join(w.capitalize() for w in agent_id.split("_"))

    return (
        "TASK\n"
        "────\n"
        "Generate a Python Agent class for a ROS2 robot AI system.\n"
        "Call write_agent_file with your complete implementation.\n"
        "Think through the agent's behavior before writing code.\n"
        "\n"
        "SPECIFICATION\n"
        "─────────────\n"
        f"Agent ID : {agent_id}\n"
        f"{spec_json}\n"
        "\n"
        'The "prompt" field is the robot\'s LLM system prompt — expand it into complete\n'
        "behavior guidelines (persona, decision rules, how to use skills).\n"
        'The "new_skills" entries become the return value of get_skills().\n'
        'The "existing_skills" entries are already implemented — include them in get_skills() too.\n'
        "\n"
        f"{existing_skills_section}"
        "AGENT INTERFACE CONTRACT\n"
        "────────────────────────\n"
        f"{agent_interface}\n"
        "\n"
        f"{examples_section}"
        "RULES\n"
        "─────\n"
        f"1. Class name: CamelCase of agent_id  ({agent_id} → {class_name})\n"
        f'2. id property must return exactly: "{agent_id}"\n'
        '3. Prefix every skill with "innate-os/" (e.g. "innate-os/skill_name")\n'
        "4. get_skills() must include BOTH existing_skills AND new_skills (all prefixed with innate-os/)\n"
        "5. get_prompt() must return detailed multi-sentence behavior instructions, not None\n"
        '6. Include get_inputs() returning ["micro"] unless the spec explicitly says otherwise\n'
        "7. File must be importable standalone — no ROS imports, no side effects at module level\n"
        "8. Submit via write_agent_file only — no markdown, no explanations outside the code\n"
    )


# ---------------------------------------------------------------------------
# Skill prompt helpers
# ---------------------------------------------------------------------------


def load_skill_interface(skill_types_path: Optional[str] = None) -> str:
    """Return the Skill ABC contract to embed in the prompt.

    Always returns the compact :data:`_SKILL_INTERFACE_FALLBACK` — it contains
    exactly the abstract methods and descriptors that generated skills need.
    The full ``skill_types.py`` is ~380 lines and includes ``PhysicalSkill``
    (ROS action boilerplate never used in generated code), so loading it would
    bloat the prompt without helping generation quality.
    """
    return _SKILL_INTERFACE_FALLBACK


def load_hardware_interfaces(interface_paths: Optional[list[str]] = None) -> str:
    """Return the hardware interface API reference to embed in the skill prompt.

    Tries to read the three live interface files from *interface_paths* (in order:
    manipulation, mobility, head).  Strips heavy docstrings/implementation from each
    file so only the method signatures and short descriptions remain.  Falls back to
    :data:`_INTERFACE_API_FALLBACK` when files are missing or unreadable.
    """
    if not interface_paths:
        return _INTERFACE_API_FALLBACK

    sections: list[str] = []
    for p in interface_paths:
        try:
            src = Path(p).read_text(encoding="utf-8")
            # Keep only lines that are class/def signatures or short comments — strip
            # implementation bodies to save tokens while preserving the API surface.
            lines = src.splitlines()
            kept: list[str] = []
            in_body = False
            for line in lines:
                stripped = line.strip()
                # Always keep class defs, def signatures, and short docstring lines.
                if stripped.startswith(("class ", "def ", "    def ", "        def ")):
                    kept.append(line)
                    in_body = False
                elif stripped.startswith('"""') and len(stripped) < 120:
                    kept.append(line)
                elif stripped.startswith("#") and len(stripped) < 120:
                    kept.append(line)
                elif stripped.startswith("GRIPPER_"):
                    kept.append(line)
            if kept:
                fname = Path(p).name
                sections.append(f"# {fname}\n" + "\n".join(kept))
                _LOG.debug("Loaded hardware interface from %s (%d kept lines)", p, len(kept))
        except OSError as exc:
            _LOG.warning("Cannot read interface file %r: %s — skipping", p, exc)

    if sections:
        return "\n\n".join(sections)
    return _INTERFACE_API_FALLBACK


def load_skill_examples(
    skills_dir: Optional[str],
    max_examples: int = 2,
) -> list[tuple[str, str]]:
    """Read up to *max_examples* Python skill files from *skills_dir*.

    Selects the shortest files first.  Skips ``__init__.py``, ``_*`` files,
    and utility helpers like ``arm_utils.py``.  Returns ``[]`` when *skills_dir*
    is ``None`` or cannot be read.  Only reads top-level ``.py`` files (not
    subdirectories, which are learned/replay skills with different structure).
    """
    return _load_examples(skills_dir, _SKILL_SKIP_FILENAMES, max_examples)


def build_skill_prompt(
    skill_name: str,
    description: str,
    *,
    skill_interface: str,
    skill_examples: list[tuple[str, str]],
    hardware_interface_api: str = "",
) -> str:
    """Assemble the user prompt for skill generation."""
    examples_section = _format_examples(skill_examples)
    class_name = "".join(w.capitalize() for w in skill_name.split("_"))
    hw_section = (
        "HARDWARE INTERFACE API\n"
        "──────────────────────\n"
        "Declare interfaces at class level with Interface(InterfaceType.*) descriptors.\n"
        "The runtime injects the real object before execute(). Always check for None.\n\n"
        f"{hardware_interface_api}\n\n"
    ) if hardware_interface_api else ""

    return (
        "TASK\n"
        "────\n"
        "Generate a Python Skill class for a ROS2 robot AI system.\n"
        "Call write_skill_file with your complete implementation.\n"
        "Think through what the skill needs before writing code.\n"
        "\n"
        "SPECIFICATION\n"
        "─────────────\n"
        f"Skill name  : {skill_name}\n"
        f"Description : {description}\n"
        "\n"
        "SKILL INTERFACE CONTRACT\n"
        "────────────────────────\n"
        f"{skill_interface}\n"
        "\n"
        f"{hw_section}"
        f"{examples_section}"
        "RULES\n"
        "─────\n"
        f"1. Class name: CamelCase of skill_name  ({skill_name} → {class_name})\n"
        f'2. name property must return exactly: "{skill_name}"\n'
        "3. execute() must return tuple[str, SkillResult] — always accept **kwargs\n"
        "4. cancel() must return a str cancellation message\n"
        "5. Override guidelines() with a clear description of when/how to use this skill\n"
        "6. For hardware access use Interface descriptors; always check for None before use\n"
        "7. Only call methods shown in HARDWARE INTERFACE API — do not invent method names\n"
        "8. File must be importable standalone — no top-level ROS calls or side effects\n"
        "9. Submit via write_skill_file only — no markdown, no explanations outside the code\n"
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load_examples(
    directory: Optional[str],
    skip_filenames: set[str],
    max_examples: int,
) -> list[tuple[str, str]]:
    """Read up to *max_examples* ``.py`` files from *directory*.

    Skips files whose names are in *skip_filenames* or start with ``_``.
    Sorts by source length ascending (shortest = most concise example first).
    Returns ``[]`` when *directory* is ``None`` or unreadable.
    """
    if directory is None:
        return []

    try:
        candidates = [
            p for p in Path(directory).iterdir()
            if p.suffix == ".py"
            and p.name not in skip_filenames
            and not p.name.startswith("_")
        ]
    except OSError as exc:
        _LOG.warning("Cannot read directory %r: %s — using no examples", directory, exc)
        return []

    results: list[tuple[str, str]] = []
    for path in candidates:
        try:
            src = path.read_text(encoding="utf-8")
            results.append((path.name, src))
        except OSError as exc:
            _LOG.warning("Skipping %s: %s", path.name, exc)

    results.sort(key=lambda pair: len(pair[1]))
    selected = results[:max_examples]
    _LOG.debug(
        "Loaded %d example(s) from %s: %s",
        len(selected),
        directory,
        [name for name, _ in selected],
    )
    return selected


def load_examples_from_paths(paths: list[str]) -> list[tuple[str, str]]:
    """Load (filename, source) pairs from explicit file paths.

    Silently skips paths that cannot be read.  Returns pairs in the same
    order as *paths* so callers control priority.
    """
    results: list[tuple[str, str]] = []
    for p in paths:
        try:
            src = Path(p).read_text(encoding="utf-8")
            results.append((Path(p).name, src))
            _LOG.debug("Loaded pinned example: %s", p)
        except OSError as exc:
            _LOG.warning("Cannot read pinned example %r: %s — skipping", p, exc)
    return results


def _format_existing_skills(skills: list[tuple[str, str]]) -> str:
    """Format ``(skill_name, description)`` pairs into a prompt section string.

    Returns an empty string when *skills* is empty.
    """
    if not skills:
        return ""
    lines = []
    for name, desc in skills:
        if desc:
            lines.append(f"  - {name}: {desc}")
        else:
            lines.append(f"  - {name}")
    body = "\n".join(lines)
    return (
        "EXISTING SKILLS AVAILABLE TO THIS AGENT\n"
        "────────────────────────────────────────\n"
        "These skills are already implemented and must be included in get_skills():\n"
        f"{body}\n"
        "\n"
    )


def _format_examples(examples: list[tuple[str, str]]) -> str:
    """Format (filename, source) pairs into a prompt section string."""
    if not examples:
        return ""
    parts = [f"# {filename}\n{src}" for filename, src in examples]
    return (
        "EXISTING STYLE EXAMPLES\n"
        "────────────────────────\n"
        + "\n\n".join(parts)
        + "\n\n"
    )
