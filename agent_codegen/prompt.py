"""
Prompt construction for agent_codegen.

Dynamically loads the Agent ABC interface and existing agent examples from the
caller's filesystem so that generated code matches the live codebase.  Both
inputs have safe fallbacks: the embedded ``_AGENT_INTERFACE_FALLBACK`` constant
is used when ``agent_types_path`` is unavailable, and an empty list of examples
is used when ``agents_dir`` is unavailable.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedded fallback — verbatim copy of brain_client.agent_types.Agent.
# Used when the caller does not supply agent_types_path or the file is missing.
# ---------------------------------------------------------------------------

_AGENT_INTERFACE_FALLBACK = '''\
#!/usr/bin/env python3
"""
Agent Type Definitions

Base class and types for robot agents.
"""
from abc import ABC, abstractmethod
from typing import List, Optional


class Agent(ABC):
    """
    Base class for all agents.

    An agent provides personality and behavior guidelines for the robot,
    along with the list of skills that should be available when this
    agent is active.
    """

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
    def get_skills(self) -> List[str]:
        """
        Returns a list of skill names that should be available
        when this agent is active.

        Subclasses must implement this method.
        """
        pass

    @abstractmethod
    def get_prompt(self) -> Optional[str]:
        """
        Returns the prompt/description for this directive.
        This defines the robot\'s personality and behavior guidelines.

        Subclasses must implement this method.
        """
        pass

    @property
    def display_icon(self) -> Optional[str]:
        """Optional path to a 32x32 pixel icon asset. Default: None."""
        return None

    def get_inputs(self) -> List[str]:
        """
        Returns a list of input device names active when this directive runs.
        Default: [] (no input devices required).
        Example: return ["micro", "camera"]
        """
        return []

    def uses_gaze(self) -> bool:
        """
        Whether this agent uses person-tracking gaze.
        When True, the robot looks at detected people during conversation.
        Default: False.
        """
        return False

    def get_routing_description(self) -> Optional[str]:
        """
        Optional short text used by AgentOrchestrator to route tasks to this agent.
        When None, the orchestrator derives context from display_name, id, skills,
        and a truncated system prompt.
        """
        return None
'''

# Files excluded from style examples (meta-agents or non-representative patterns).
_SKIP_FILENAMES = {"__init__.py", "orchestrator_agent.py"}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def load_agent_interface(agent_types_path: Optional[str] = None) -> str:
    """Return the Agent ABC source to embed in the prompt.

    If *agent_types_path* is given and the file is readable its full text is
    returned — this ensures generated code matches the live codebase exactly.
    Falls back to :data:`_AGENT_INTERFACE_FALLBACK` on any error.

    Args:
        agent_types_path: Absolute path to ``agent_types.py``, or ``None``.

    Returns:
        The Agent ABC source as a string.
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

    Selects the shortest files first so the prompt stays concise and the model
    sees clean, minimal patterns rather than long, complex agents.

    Skips:
    - ``__init__.py`` and files whose names start with ``_``
    - ``orchestrator_agent.py`` (meta-agent — not a useful implementation example)

    Args:
        agents_dir: Path to the directory containing agent ``.py`` files, or ``None``.
        max_examples: Maximum number of examples to include (default: 2).

    Returns:
        List of ``(filename, source)`` pairs, shortest file first.
        Returns ``[]`` if *agents_dir* is ``None`` or cannot be read.
    """
    if agents_dir is None:
        return []

    try:
        candidates = [
            p for p in Path(agents_dir).iterdir()
            if p.suffix == ".py"
            and p.name not in _SKIP_FILENAMES
            and not p.name.startswith("_")
        ]
    except OSError as exc:
        _LOG.warning("Cannot read agents_dir %r: %s — using no examples", agents_dir, exc)
        return []

    results: list[tuple[str, str]] = []
    for path in candidates:
        try:
            src = path.read_text(encoding="utf-8")
            results.append((path.name, src))
        except OSError as exc:
            _LOG.warning("Skipping %s: %s", path.name, exc)

    # Shortest source first so the model sees concise examples.
    results.sort(key=lambda pair: len(pair[1]))
    selected = results[:max_examples]
    _LOG.debug(
        "Loaded %d agent example(s) from %s: %s",
        len(selected),
        agents_dir,
        [name for name, _ in selected],
    )
    return selected


def build_prompt(
    agent_id: str,
    agent_spec: dict,
    *,
    agent_interface: str,
    agent_examples: list[tuple[str, str]],
) -> str:
    """Assemble the user prompt sent to MiniMax.

    Args:
        agent_id:        Snake-case identifier for the agent to generate.
        agent_spec:      The value dict from ``missing_capabilities[agent_id]``.
                         Contains ``"prompt"`` and ``"new_skills"`` keys.
        agent_interface: Source of the Agent ABC (from :func:`load_agent_interface`).
        agent_examples:  List of ``(filename, source)`` pairs from
                         :func:`load_agent_examples`.

    Returns:
        The complete prompt string to send to the model.
    """
    spec_json = json.dumps(agent_spec, indent=2)

    examples_section = ""
    if agent_examples:
        parts = []
        for filename, src in agent_examples:
            parts.append(f"# {filename}\n{src}")
        examples_section = (
            "EXISTING STYLE EXAMPLES\n"
            "────────────────────────\n"
            + "\n\n".join(parts)
            + "\n\n"
        )

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
        "\n"
        "AGENT INTERFACE CONTRACT\n"
        "────────────────────────\n"
        f"{agent_interface}\n"
        "\n"
        f"{examples_section}"
        "RULES\n"
        "─────\n"
        f"1. Class name: CamelCase of agent_id  ({agent_id} → {''.join(w.capitalize() for w in agent_id.split('_'))})\n"
        f'2. id property must return exactly: "{agent_id}"\n'
        '3. Prefix every skill with "innate-os/" (e.g. "innate-os/skill_name")\n'
        "4. get_prompt() must return detailed multi-sentence behavior instructions, not None\n"
        '5. Include get_inputs() returning ["micro"] unless the spec explicitly says otherwise\n'
        "6. File must be importable standalone — no ROS imports, no side effects at module level\n"
        "7. Submit via write_agent_file only — no markdown, no explanations outside the code\n"
    )
