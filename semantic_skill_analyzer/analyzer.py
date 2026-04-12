"""
Semantic Skill Analyzer — core implementation.

Takes a text prompt and a capabilities index, asks a local ollama LLM (``analyze``)
or Google GenAI / Gemini (``analyze_gemma``) to:
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
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import ollama

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "qwen3:1.7b"
GEMINI_ANALYZER_MODEL = "gemini-3-flash-preview"
DEFAULT_CAPABILITIES_PATH = "~/.wildrobot/capabilities.json"
DEFAULT_OUTPUT_DIR = "~/.wildrobot"
# Read from env so the same code works on both laptop (localhost) and the
# remote GPU machine (set OLLAMA_HOST=http://172.17.30.138:11434 there).
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MAX_RETRIES = 1

_SYSTEM_PROMPT = """\
You are a robot capability analyst. Output ONLY a JSON object — no explanation, no markdown.

Format:
{
  "existing_agents": [],
  "missing_capabilities": {}
}

- "existing_agents": agent names from the catalog that already handle parts of this task
- "missing_capabilities": AT MOST ONE new agent; keys are agent names, values have:
    "prompt": detailed description of what the agent does
    "new_skills": list of {"skill_name": "description"} for skills not in the catalog
- Reuse existing skills wherever possible — only add to new_skills what truly does not exist
- Leave missing_capabilities as {} if everything is already covered\
"""

_RETRY_PROMPT = (
    "Your previous response was empty. Try again. "
    "Output ONLY the JSON object, starting with { and ending with }. "
    "No explanation, no markdown."
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_user_message(prompt: str, capabilities: Dict[str, Any]) -> str:
    """Combine user scenario and existing capabilities into a single message."""
    caps_json = json.dumps(capabilities, indent=2)
    return (
        f"TASK:\n{prompt}\n\n"
        f"EXISTING CAPABILITIES:\n{caps_json}\n\n"
        f"JSON:"
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


def _best_matching_agent(
    prompt: str,
    candidates: List[str],
    capabilities: Dict[str, Any],
) -> Optional[str]:
    """Return the single agent from candidates that best matches the prompt.

    Scoring: token overlap between prompt words and the agent's ID + skill strings.
    Returns None if no candidate has any token overlap with the prompt — this
    indicates the model hallucinated coverage (e.g. chess_agent for a WhatsApp task).
    """
    prompt_tokens = set(re.findall(r"[a-z0-9]+", prompt.lower()))
    best_agent: Optional[str] = None
    best_score = 0  # require at least one overlapping token
    for agent_id in candidates:
        skills = capabilities.get(agent_id, {}).get("skills", [])
        # Use skill basenames only (strip ": description") — enriched descriptions
        # contain generic words ("confirm", "detect", "use") that cause false matches
        # against unrelated prompts.
        basenames = [s.split(":")[0].strip() for s in skills if isinstance(s, str)]
        blob = agent_id.replace("_", " ") + " " + " ".join(basenames)
        blob_tokens = set(re.findall(r"[a-z0-9]+", blob.lower()))
        score = len(prompt_tokens & blob_tokens)
        if score > best_score:
            best_score = score
            best_agent = agent_id
    return best_agent


def _filter_existing_from_new_skills(
    missing: Dict[str, Any],
    capabilities: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Remove from each agent's new_skills list any skill that already exists in
    the catalog.

    Small models sometimes list an existing skill (e.g. ``arm_utils``) as if it
    needs to be created.  Ground-truth filtering catches both name formats the
    model emits:
    - ``{"draw_triangle": "description"}`` — key is the skill name
    - ``{"skill_name": "draw_triangle", "description": "..."}`` — structured dict
    """
    known_skills = _known_skill_basenames(capabilities)
    result: Dict[str, Any] = {}
    for agent_name, agent_spec in missing.items():
        filtered: List[Any] = []
        for skill in agent_spec.get("new_skills", []):
            if not isinstance(skill, dict):
                continue
            if "skill_name" in skill:
                name = skill["skill_name"]
            else:
                name = next(iter(skill), "")
            name = name.split(":")[0].strip()
            if name and name not in known_skills:
                filtered.append(skill)
        result[agent_name] = {**agent_spec, "new_skills": filtered}
    return result


def _merge_partial_coverage(
    result: Dict[str, Any],
    capabilities: Dict[str, Any],
) -> Dict[str, Any]:
    """
    When existing agents only partially cover the request (some skills present,
    some still missing), collapse everything into a single new-agent spec.

    The new agent spec gains an ``existing_skills`` list — skill basenames the
    agent can reuse without codegen — alongside the usual ``new_skills`` list of
    skills that must be created.  ``existing_agents`` is cleared so the output
    does not falsely claim the task is already handled.

    When there is no partial overlap (fully covered or fully missing) the result
    is returned unchanged.
    """
    existing = result.get("existing_agents", [])
    missing = result.get("missing_capabilities", {})

    if not existing or not missing:
        return result  # fully covered or fully missing — nothing to merge

    # Collect reusable skill basenames from the partial-match agents
    reusable: List[str] = []
    for agent_id in existing:
        for skill in capabilities.get(agent_id, {}).get("skills", []):
            if isinstance(skill, str):
                basename = skill.split(":")[0].strip()
                if basename not in reusable:
                    reusable.append(basename)

    merged_missing: Dict[str, Any] = {
        agent_name: {**agent_spec, "existing_skills": reusable}
        for agent_name, agent_spec in missing.items()
    }
    return {"existing_agents": [], "missing_capabilities": merged_missing}


def _keep_first_missing_agent(missing: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the first entry in missing_capabilities.

    Small models tend to enumerate many missing agents; we enforce at most one
    so the downstream code generator receives a single, focused target.
    """
    if len(missing) <= 1:
        return missing
    first_key = next(iter(missing))
    return {first_key: missing[first_key]}


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


def _parse_analysis_json_from_text(content: str) -> Optional[Dict[str, Any]]:
    """
    Parse the capability analysis envelope from model plain-text JSON.

    Handles:
    - <think>…</think> blocks (qwen3 thinking mode) — stripped before parsing
    - Markdown code fences
    - Bare dict:    {"existing_agents": [...], "missing_capabilities": {...}}
    - Legacy bare:  {"agent_name": {"prompt": ..., "new_skills": [...]}}
    """
    content = (content or "").strip()
    if not content:
        return None

    content = _strip_think_blocks(content)
    if not content:
        return None

    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content.strip())

    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        if "existing_agents" in data or "missing_capabilities" in data:
            return {
                "existing_agents": _normalize_agent_list(data.get("existing_agents", [])),
                "missing_capabilities": data.get("missing_capabilities", {}),
            }
        return {"existing_agents": [], "missing_capabilities": data}
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_content_result(response: Any) -> Optional[Dict[str, Any]]:
    """Fallback: attempt to parse JSON from the plain-text ollama response content."""
    return _parse_analysis_json_from_text(response.message.content or "")


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


_LOG = logging.getLogger(__name__)


def _extract_result(response: Any) -> Dict[str, Any]:
    """Extract a result dict from an ollama response, trying tool call then content."""
    raw = (response.message.content or "").strip()
    thinking_only = _strip_think_blocks(raw).strip() == ""
    _LOG.debug(
        "raw model response (%d chars)%s:\n%s",
        len(raw),
        " [thinking only — no JSON after </think>]" if thinking_only else "",
        raw[:1200],
    )

    result = _extract_tool_result(response)
    if result is None:
        result = _extract_content_result(response)
    if result is None:
        _LOG.warning(
            "Could not extract JSON from model response "
            "(thinking_only=%s, content_len=%d) — returning empty result",
            thinking_only,
            len(raw),
        )
        result = {"existing_agents": [], "missing_capabilities": {}}
    return result


def _postprocess_capability_result(
    result: Dict[str, Any],
    prompt: str,
    capabilities: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Apply catalog ground-truth filters, single-agent narrowing, and partial-coverage merge.

    Shared by ``analyze`` (ollama) and ``analyze_gemma`` (Google GenAI).
    """
    result = {
        "existing_agents": list(result.get("existing_agents", [])),
        "missing_capabilities": dict(result.get("missing_capabilities", {})),
    }
    _LOG.debug("raw parsed:  existing=%s  missing_keys=%s",
               result["existing_agents"], list(result["missing_capabilities"]))

    result["existing_agents"] = _filter_valid_existing_agents(
        result["existing_agents"], capabilities
    )
    before_filter = set(result["missing_capabilities"])
    result["missing_capabilities"] = _filter_existing_from_missing(
        result["missing_capabilities"], capabilities
    )
    dropped = before_filter - set(result["missing_capabilities"])
    if dropped:
        _LOG.debug("filtered out (already in catalog): %s", dropped)

    result["missing_capabilities"] = _keep_first_missing_agent(
        result["missing_capabilities"]
    )
    result["missing_capabilities"] = _filter_existing_from_new_skills(
        result["missing_capabilities"], capabilities
    )
    if result["existing_agents"]:
        best = _best_matching_agent(prompt, result["existing_agents"], capabilities)
        result["existing_agents"] = [best] if best is not None else []

    _LOG.debug("after postprocess: existing=%s  missing_keys=%s",
               result["existing_agents"], list(result["missing_capabilities"]))
    return _merge_partial_coverage(result, capabilities)


def _resolve_gemini_api_key(api_key: Optional[str]) -> str:
    if api_key:
        return api_key
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ValueError(
            "analyze_gemma requires api_key=... or GEMINI_API_KEY / GOOGLE_API_KEY in the environment"
        )
    return key


def analyze(
    prompt: str,
    capabilities_path: Union[str, Path] = DEFAULT_CAPABILITIES_PATH,
    output_dir: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
    max_retries: int = MAX_RETRIES,
) -> str:
    """
    Analyze a text prompt against existing capabilities and write an analysis file.

    The LLM identifies:
    - ``existing_agents``: existing agents that already cover the request
    - ``missing_capabilities``: new agents/skills that need to be created

    When the model returns an empty response (both fields blank), the call is
    retried up to ``max_retries`` times with a nudge message appended to the
    conversation so the model has context on why it is being asked again.

    The result is written to::

        <output_dir>/<uuid4>-missing-skills.json

    Args:
        prompt: Natural-language description of a desired scenario or feature.
        capabilities_path: Path to capabilities.json produced by the capabilities exporter.
        output_dir: Directory where the output file is written.
        ollama_host: Ollama API base URL (default: http://localhost:11434).
        max_retries: How many times to retry when the model returns a blank result
            (default: 1).

    Returns:
        Absolute path string of the written output file.
    """
    capabilities = load_capabilities(capabilities_path)

    client = ollama.Client(host=ollama_host)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(prompt, capabilities)},
    ]

    # No tool schema — ask the model to write JSON directly in its response.
    # Small models (0.6b) consistently ignore tool schemas and return nothing;
    # plain text JSON output is the only approach that works at this param count.
    # think=True lets the model reason in <think> blocks before writing JSON;
    # _strip_think_blocks in the content extractor removes them transparently.
    # temperature=0 forces greedy decoding for deterministic output across runs.
    _chat_opts: Dict[str, Any] = {"temperature": 0}
    response = client.chat(model=MODEL, messages=messages, think=True, options=_chat_opts)
    result = _extract_result(response)

    # Retry when the model returns both fields empty.
    for _ in range(max_retries):
        if result["existing_agents"] or result["missing_capabilities"]:
            break
        retry_messages = messages + [
            {"role": "assistant", "content": response.message.content or ""},
            {"role": "user", "content": _RETRY_PROMPT},
        ]
        response = client.chat(model=MODEL, messages=retry_messages, think=True, options=_chat_opts)
        result = _extract_result(response)

    result = _postprocess_capability_result(result, prompt, capabilities)

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{uuid.uuid4()}-missing-skills.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

    return str(out_file)


def analyze_gemma(
    prompt: str,
    capabilities_path: Union[str, Path] = DEFAULT_CAPABILITIES_PATH,
    output_dir: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    api_key: Optional[str] = None,
    model: str = GEMINI_ANALYZER_MODEL,
    max_retries: int = MAX_RETRIES,
) -> str:
    """
    Same pipeline as :func:`analyze`, but calls Google GenAI (Gemini) instead of ollama.

    The system and user prompts match the ollama path; the model is asked for the
    same JSON envelope. Responses are parsed with the same JSON extraction rules
    as plain-text ollama output (including markdown fences when present).

    Args:
        prompt: Natural-language description of a desired scenario or feature.
        capabilities_path: Path to capabilities.json.
        output_dir: Directory where the output file is written.
        api_key: Google AI API key. If omitted, uses ``GEMINI_API_KEY`` or
            ``GOOGLE_API_KEY`` from the environment.
        model: GenAI model id (default: ``GEMINI_ANALYZER_MODEL`` —
            ``gemini-3-flash-preview``).
        max_retries: Extra attempts when both ``existing_agents`` and
            ``missing_capabilities`` parse as empty (same semantics as
            :func:`analyze`).

    Returns:
        Absolute path string of the written output file.

    Raises:
        ValueError: If no API key is available from arguments or environment.
    """
    from google import genai as google_genai

    capabilities = load_capabilities(capabilities_path)
    key = _resolve_gemini_api_key(api_key)
    client = google_genai.Client(api_key=key)

    combined_prompt = f"{_SYSTEM_PROMPT}\n\n{_build_user_message(prompt, capabilities)}"
    response_text = ""
    result: Dict[str, Any] = {"existing_agents": [], "missing_capabilities": {}}
    contents: str = combined_prompt

    for call_idx in range(max_retries + 1):
        response = client.models.generate_content(model=model, contents=contents)
        response_text = (getattr(response, "text", None) or "").strip()
        parsed = _parse_analysis_json_from_text(response_text)
        result = (
            parsed
            if parsed is not None
            else {"existing_agents": [], "missing_capabilities": {}}
        )
        if result["existing_agents"] or result["missing_capabilities"]:
            break
        if call_idx == max_retries:
            break
        contents = (
            f"{combined_prompt}\n\n"
            f"Assistant:\n{response_text or '(empty)'}\n\n"
            f"User:\n{_RETRY_PROMPT}"
        )

    result = _postprocess_capability_result(result, prompt, capabilities)

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{uuid.uuid4()}-missing-skills.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

    return str(out_file)
