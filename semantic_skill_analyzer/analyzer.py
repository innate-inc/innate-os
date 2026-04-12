"""
Semantic Skill Analyzer — core implementation.

Takes a text prompt and a capabilities index, asks a local ollama LLM to:
  - identify which existing agents already cover the request
  - identify missing agents/skills not yet in the system

Output is written to ~/.wildrobot/<uuid>-missing-skills.json with the format:
  {
    "existing_agents": ["agent_id", ...],
    "missing_capabilities": {
      "new_agent_name": {
        "prompt": "...",
        "new_skills": [{"skill_name": "description"}, ...]
      }
    }
  }

No dependency on brain_client, ROS, or innate-os internals.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import ollama

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "qwen3:0.6b"
DEFAULT_CAPABILITIES_PATH = "~/.wildrobot/capabilities.json"
DEFAULT_OUTPUT_DIR = "~/.wildrobot"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

_SYSTEM_PROMPT = """\
You are a robot capability analyst. Given a user scenario and a list of existing agents \
and skills, identify:
1. Which existing agents can already handle the request (list them by name).
2. What genuinely new agents and skills are needed for parts not covered by existing ones.

Rules:
- Avoid creating agents that sound similar to existing ones (e.g. scout vs patrol).
- Avoid duplicate skills — agents and skills have a many-to-many relationship.
- Only propose new agents/skills for capabilities that truly do not exist yet.
- Agent and skill prompts must be detailed and functional. Do not describe implementation. \
Focus on what they do. These prompts will be fed to the next LLM for code generation.\
"""

# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "save_capability_analysis",
        "description": (
            "Save the full capability analysis: which existing agents cover the request "
            "and what new agents/skills need to be created for the rest."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "existing_agents": {
                    "type": "array",
                    "description": (
                        "Names of existing agents (from the catalog) that can already "
                        "handle this request, fully or partially."
                    ),
                    "items": {"type": "string"},
                },
                "missing_capabilities": {
                    "type": "object",
                    "description": (
                        "New agents that must be created. Keys are agent names. "
                        "Empty object if everything is already covered."
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "description": (
                                    "Detailed functional description of what this agent "
                                    "does and which skills it orchestrates."
                                ),
                            },
                            "new_skills": {
                                "type": "array",
                                "description": "New skills this agent needs that don't exist yet.",
                                "items": {
                                    "type": "object",
                                    "description": "{skill_name: detailed_description}",
                                },
                            },
                        },
                        "required": ["prompt", "new_skills"],
                    },
                },
            },
            "required": ["existing_agents", "missing_capabilities"],
        },
    },
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_user_message(prompt: str, capabilities: Dict[str, Any]) -> str:
    """Combine user scenario and existing capabilities into a single message."""
    caps_json = json.dumps(capabilities, indent=2)
    return (
        f"USER TASK / SCENARIO:\n{prompt}\n\n"
        f"EXISTING CAPABILITIES:\n{caps_json}"
    )


def _strip_think_blocks(text: str) -> str:
    """Remove <think>…</think> blocks emitted by qwen3 in thinking mode."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _known_skill_basenames(capabilities: Dict[str, Any]) -> set:
    """
    Collect all skill basenames from the capabilities index.

    Skill strings may be plain basenames ("wave") or enriched
    ("wave: Make the robot wave its arm."). Strip the description part.
    """
    names: set = set()
    for agent_data in capabilities.values():
        for skill in agent_data.get("skills", []):
            if isinstance(skill, str):
                names.add(skill.split(":")[0].strip())
    return names


def _filter_existing_from_missing(
    missing: Dict[str, Any],
    capabilities: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Remove from missing_capabilities any key that already exists in the catalog —
    either as an agent ID or as a known skill basename.

    Small models hallucinate in two ways:
    1. They list an existing agent name as "missing".
    2. They list a skill name as if it were a new agent name.
    Both are caught here using the ground-truth capabilities index.
    """
    known_agents = set(capabilities.keys())
    known_skills = _known_skill_basenames(capabilities)
    discard = known_agents | known_skills
    return {k: v for k, v in missing.items() if k not in discard}


def _filter_valid_existing_agents(
    agents: List[str],
    capabilities: Dict[str, Any],
) -> List[str]:
    """
    Keep only items in existing_agents that are actual agent IDs in the catalog.

    Small models sometimes put skill description strings or arbitrary text into
    this list instead of agent IDs. Ground-truth filtering removes them.
    """
    return [a for a in agents if a in capabilities]


def _normalize_agent_list(agents: Any) -> List[str]:
    """
    Coerce the existing_agents value to a plain list of strings.

    Models occasionally return items as objects (e.g. {"name": "demo_agent"})
    instead of bare strings. Extract the first string value in that case.
    """
    if not isinstance(agents, list):
        return []
    result = []
    for item in agents:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            # Accept {"name": "..."} or any single-value dict
            for v in item.values():
                if isinstance(v, str):
                    result.append(v)
                    break
    return result


def _extract_tool_result(response: Any) -> Optional[Dict[str, Any]]:
    """
    Extract the analysis dict from an ollama tool-call response.

    Returns a dict with keys ``existing_agents`` and ``missing_capabilities``,
    or None if the model did not call the expected tool.
    """
    if not response.message.tool_calls:
        return None
    for tc in response.message.tool_calls:
        if tc.function.name == "save_capability_analysis":
            args = tc.function.arguments
            if isinstance(args, str):
                args = json.loads(args)
            return {
                "existing_agents": _normalize_agent_list(args.get("existing_agents", [])),
                "missing_capabilities": args.get("missing_capabilities", {}),
            }
    return None


def _extract_content_result(response: Any) -> Optional[Dict[str, Any]]:
    """
    Fallback: attempt to parse JSON from the plain-text response content.

    Handles:
    - <think>…</think> blocks (qwen3 thinking mode) — stripped before parsing
    - Markdown code fences
    - Bare dict:    {"existing_agents": [...], "missing_capabilities": {...}}
    - Legacy bare:  {"agent_name": {"prompt": ..., "new_skills": [...]}}
    """
    content = (response.message.content or "").strip()
    if not content:
        return None

    # Strip qwen3 thinking blocks
    content = _strip_think_blocks(content)
    if not content:
        return None

    # Strip markdown code fences
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content.strip())

    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        # Already in the expected envelope format
        if "existing_agents" in data or "missing_capabilities" in data:
            return {
                "existing_agents": _normalize_agent_list(data.get("existing_agents", [])),
                "missing_capabilities": data.get("missing_capabilities", {}),
            }
        # Legacy flat format: {agent_name: {prompt, new_skills}}
        return {"existing_agents": [], "missing_capabilities": data}
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_capabilities(
    path: Union[str, Path] = DEFAULT_CAPABILITIES_PATH,
) -> Dict[str, Any]:
    """
    Load and return the capabilities index from a JSON file.

    Args:
        path: Path to capabilities.json (default: ~/.wildrobot/capabilities.json).

    Returns:
        Dict mapping agent ids to their skill lists.
    """
    return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))


def analyze(
    prompt: str,
    capabilities_path: Union[str, Path] = DEFAULT_CAPABILITIES_PATH,
    output_dir: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
) -> str:
    """
    Analyze a text prompt against existing capabilities and write an analysis file.

    The LLM identifies:
    - ``existing_agents``: existing agents that already cover the request
    - ``missing_capabilities``: new agents/skills that need to be created

    The result is written to::

        <output_dir>/<uuid4>-missing-skills.json

    Args:
        prompt: Natural-language description of a desired scenario or feature.
        capabilities_path: Path to capabilities.json produced by the capabilities exporter.
        output_dir: Directory where the output file is written.
        ollama_host: Ollama API base URL (default: http://localhost:11434).

    Returns:
        Absolute path string of the written output file.
    """
    capabilities = load_capabilities(capabilities_path)

    client = ollama.Client(host=ollama_host)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(prompt, capabilities)},
    ]

    response = client.chat(
        model=MODEL,
        messages=messages,
        tools=[_TOOL],
        think=False,  # disable qwen3 thinking mode for reliable tool calling
    )

    result = _extract_tool_result(response)
    if result is None:
        result = _extract_content_result(response)
    if result is None:
        result = {"existing_agents": [], "missing_capabilities": {}}

    # Keep only real agent IDs in existing_agents (model sometimes puts skill
    # descriptions or arbitrary text there instead of agent names)
    result["existing_agents"] = _filter_valid_existing_agents(
        result["existing_agents"], capabilities
    )
    # Remove any existing agent or skill name the model mistakenly placed in
    # missing_capabilities (model confuses agents with skills, or duplicates
    # existing agents as "missing")
    result["missing_capabilities"] = _filter_existing_from_missing(
        result["missing_capabilities"], capabilities
    )

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{uuid.uuid4()}-missing-skills.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

    return str(out_file)
