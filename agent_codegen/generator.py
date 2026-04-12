"""
Agent and skill code generation orchestrator.

Ties together prompt building, the MiniMax client, and file I/O.
Public entry points: :func:`generate_agent`, :func:`generate_skill`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from agent_codegen.client import DEFAULT_MODEL, MiniMaxClient
from agent_codegen.models import AgentCodegenError, GenerationResult, SkillGenerationResult
from agent_codegen.prompt import (
    build_prompt,
    build_skill_prompt,
    load_agent_examples,
    load_agent_interface,
    load_skill_examples,
    load_skill_interface,
)

_LOG = logging.getLogger(__name__)

_AGENT_VALIDATION_TOKENS = ("class", "Agent", "brain_client")
_SKILL_VALIDATION_TOKENS = ("class", "Skill", "brain_client")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_capabilities(path: Optional[str]) -> dict:
    """Load a capabilities.json file; returns ``{}`` on any error."""
    if path is None:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("Cannot read capabilities %r: %s — existing skill descriptions unavailable", path, exc)
        return {}


def _build_skill_desc_map(capabilities: dict) -> dict[str, str]:
    """Flatten a capabilities dict into ``{skill_name: description}``.

    Entries are ``"skill_name: description"`` or just ``"skill_name"``.  All
    agents in the file are scanned so any installed skill can be found.
    Later entries overwrite earlier ones (all descriptions for a given skill
    should be identical in practice).
    """
    skill_map: dict[str, str] = {}
    for _agent_id, agent_info in capabilities.items():
        for entry in agent_info.get("skills", []):
            if ": " in entry:
                name, _, desc = entry.partition(": ")
                skill_map[name.strip()] = desc.strip()
            else:
                skill_map[entry.strip()] = ""
    return skill_map


def _validate_agent_code(code: str) -> bool:
    return all(tok in code for tok in _AGENT_VALIDATION_TOKENS)


def _validate_skill_code(code: str) -> bool:
    return all(tok in code for tok in _SKILL_VALIDATION_TOKENS)


def _write_file(code: str, output_path: str) -> str:
    """Write *code* to *output_path*, backing up any existing file. Returns absolute path."""
    dest = Path(output_path)
    if dest.exists():
        bak = dest.with_suffix(".py.bak")
        _LOG.warning("Output file already exists — renaming to %s", bak)
        dest.rename(bak)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(code, encoding="utf-8")
    return str(dest)


def _parse_skill_entries(new_skills: list) -> list[tuple[str, str]]:
    """Extract ``(skill_name, description)`` pairs from a ``new_skills`` list.

    Handles two formats produced by ``semantic_skill_analyzer``:

    - ``{"skill_name": "name", "description": "desc"}`` — two-key dict
    - ``{"skill_name": "description"}``                  — single-key dict
    """
    entries: list[tuple[str, str]] = []
    for item in new_skills:
        if not isinstance(item, dict):
            continue
        if "skill_name" in item and "description" in item:
            entries.append((str(item["skill_name"]), str(item["description"])))
        elif len(item) == 1:
            name, desc = next(iter(item.items()))
            entries.append((str(name), str(desc)))
        else:
            # Fallback: use skill_name key; stringify remainder as description.
            name = item.get("skill_name", "")
            desc = str({k: v for k, v in item.items() if k != "skill_name"})
            if name:
                entries.append((str(name), desc))
    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_skill(
    skill_name: str,
    description: str,
    *,
    api_key: str,
    output_path: Optional[str] = None,
    skills_dir: Optional[str] = None,
    skill_types_path: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
) -> SkillGenerationResult:
    """Generate a Python Skill class from a name and description.

    Args:
        skill_name:       Snake-case skill identifier (must match ``name`` property).
        description:      Human-readable description of what the skill does.
        api_key:          MiniMax API key.
        output_path:      If provided, write the generated source here.  Any
                          existing file is renamed to ``<path>.bak`` first.
        skills_dir:       Directory of existing skill ``.py`` files used as style
                          examples in the prompt.
        skill_types_path: Path to ``skill_types.py`` for the live Skill ABC source.
        model:            MiniMax model identifier.
        max_tokens:       Maximum tokens in the model response.

    Returns:
        :class:`~agent_codegen.models.SkillGenerationResult`

    Raises:
        AgentCodegenError: On empty input, invalid generated code, or API errors.
    """
    if not skill_name:
        raise AgentCodegenError("skill_name must not be empty")
    if not description:
        raise AgentCodegenError("description must not be empty")

    _LOG.info("Generating skill %r", skill_name)

    skill_interface = load_skill_interface(skill_types_path)
    skill_examples = load_skill_examples(skills_dir)

    prompt = build_skill_prompt(
        skill_name,
        description,
        skill_interface=skill_interface,
        skill_examples=skill_examples,
    )

    client = MiniMaxClient(api_key=api_key, model=model, max_tokens=max_tokens)
    code, via_tool = client.generate_skill_code(prompt)

    if not _validate_skill_code(code):
        raise AgentCodegenError(
            f"Generated code for skill {skill_name!r} failed structural validation: "
            f"must contain {_SKILL_VALIDATION_TOKENS!r}. "
            "The model may have produced incomplete output — try again."
        )

    file_path: Optional[str] = None
    if output_path is not None:
        file_path = _write_file(code, output_path)
        _LOG.info("Skill written to %s", file_path)

    return SkillGenerationResult(
        skill_name=skill_name,
        code=code,
        file_path=file_path,
        via_tool=via_tool,
    )


def generate_agent(
    missing_capabilities: dict,
    *,
    api_key: str,
    output_path: Optional[str] = None,
    agents_dir: Optional[str] = None,
    agent_types_path: Optional[str] = None,
    skills_dir: Optional[str] = None,
    skill_types_path: Optional[str] = None,
    capabilities_path: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
) -> GenerationResult:
    """Generate a Python Agent class and any missing skills from a spec.

    Reads the ``"missing_capabilities"`` sub-dict produced by
    ``semantic_skill_analyzer.analyze()``.  When the spec lists ``new_skills``
    and *skills_dir* is provided, each skill is also generated and written to
    ``<skills_dir>/<skill_name>.py``.

    Args:
        missing_capabilities:
            Dict with one key (the agent's snake-case id) whose value contains
            ``"prompt"`` and ``"new_skills"`` fields.
        api_key:
            MiniMax API key.
        output_path:
            Write the agent source here.  Existing file is backed up as ``.bak``.
        agents_dir:
            Directory of existing agent files used as style examples.
        agent_types_path:
            Path to ``agent_types.py`` for the live Agent ABC source.
        skills_dir:
            Directory of existing skills.  When provided:
            - used as style examples for both agent and skill prompts
            - new skills from ``new_skills`` are written here as
              ``<skills_dir>/<skill_name>.py``
        skill_types_path:
            Path to ``skill_types.py`` for the live Skill ABC source.
        capabilities_path:
            Path to ``capabilities.json`` (the full skill-description index).
            When provided, descriptions for ``existing_skills`` listed in the
            spec are injected into the agent prompt so the model knows exactly
            how to instruct the LLM to use each skill.
        model:
            MiniMax model identifier.
        max_tokens:
            Maximum tokens per model response.

    Returns:
        :class:`~agent_codegen.models.GenerationResult` with ``skills`` populated
        for each skill that was generated.

    Raises:
        AgentCodegenError: On empty input, invalid code, or API errors.
    """
    if not missing_capabilities:
        raise AgentCodegenError(
            "missing_capabilities is empty — nothing to generate. "
            "Run semantic_skill_analyzer first to identify a gap."
        )

    agent_id = next(iter(missing_capabilities))
    agent_spec = missing_capabilities[agent_id]

    _LOG.info("Generating agent %r from spec: %s", agent_id, list(agent_spec.keys()))

    # --- Agent generation ---
    agent_interface = load_agent_interface(agent_types_path)
    agent_examples = load_agent_examples(agents_dir)

    # Build existing-skill descriptions from the capabilities index.
    capabilities = _load_capabilities(capabilities_path)
    skill_desc_map = _build_skill_desc_map(capabilities)
    raw_existing = agent_spec.get("existing_skills", [])
    existing_skills: list[tuple[str, str]] = [
        (name, skill_desc_map.get(name, ""))
        for name in raw_existing
        if isinstance(name, str)
    ]

    prompt = build_prompt(
        agent_id,
        agent_spec,
        agent_interface=agent_interface,
        agent_examples=agent_examples,
        existing_skills=existing_skills,
    )

    client = MiniMaxClient(api_key=api_key, model=model, max_tokens=max_tokens)
    code, via_tool = client.generate(prompt)

    if not _validate_agent_code(code):
        raise AgentCodegenError(
            f"Generated code for {agent_id!r} failed structural validation: "
            f"must contain {_AGENT_VALIDATION_TOKENS!r}. "
            "The model may have produced incomplete output — try again."
        )

    file_path: Optional[str] = None
    if output_path is not None:
        file_path = _write_file(code, output_path)
        _LOG.info("Agent written to %s", file_path)

    # --- Skill generation ---
    skill_results: list[SkillGenerationResult] = []

    if skills_dir is not None:
        raw_skills = agent_spec.get("new_skills", [])
        skill_entries = _parse_skill_entries(raw_skills)

        for skill_name, description in skill_entries:
            skill_output = str(Path(skills_dir) / f"{skill_name}.py")
            _LOG.info("Generating skill %r → %s", skill_name, skill_output)
            try:
                skill_result = generate_skill(
                    skill_name,
                    description,
                    api_key=api_key,
                    output_path=skill_output,
                    skills_dir=skills_dir,
                    skill_types_path=skill_types_path,
                    model=model,
                    max_tokens=max_tokens,
                )
                skill_results.append(skill_result)
            except AgentCodegenError as exc:
                _LOG.error("Skill %r generation failed: %s", skill_name, exc)
                # Continue generating remaining skills rather than aborting.
                skill_results.append(
                    SkillGenerationResult(
                        skill_name=skill_name,
                        code="",
                        file_path=None,
                        via_tool=False,
                    )
                )

    return GenerationResult(
        agent_id=agent_id,
        code=code,
        file_path=file_path,
        via_tool=via_tool,
        skills=skill_results,
    )
