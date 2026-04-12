"""
Agent code generation orchestrator.

Ties together prompt building, the MiniMax client, and file I/O.
The public entry point is :func:`generate_agent`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from agent_codegen.client import DEFAULT_MODEL, MiniMaxClient
from agent_codegen.models import AgentCodegenError, GenerationResult
from agent_codegen.prompt import build_prompt, load_agent_examples, load_agent_interface

_LOG = logging.getLogger(__name__)

# Tokens that must appear in generated code for it to pass structural validation.
_VALIDATION_TOKENS = ("class", "Agent", "brain_client")


def _validate_code(code: str) -> bool:
    """Return ``True`` when *code* contains all required structural markers."""
    return all(tok in code for tok in _VALIDATION_TOKENS)


def generate_agent(
    missing_capabilities: dict,
    *,
    api_key: str,
    output_path: Optional[str] = None,
    agents_dir: Optional[str] = None,
    agent_types_path: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
) -> GenerationResult:
    """Generate a Python Agent class from a missing-capabilities spec.

    Args:
        missing_capabilities:
            The ``"missing_capabilities"`` sub-dict produced by
            ``semantic_skill_analyzer.analyze()``.  Must contain exactly one
            key (the new agent's snake-case id) whose value is a dict with
            ``"prompt"`` and ``"new_skills"`` fields.
        api_key:
            MiniMax API key.
        output_path:
            If provided, the generated source is written to this absolute path.
            Any existing file at the path is renamed to ``<path>.bak`` first.
            Parent directories are created automatically.
        agents_dir:
            Path to a directory of existing agent ``.py`` files.  When given,
            up to two of the shortest agents are included in the prompt as
            style examples so the model produces idiomatic code.
        agent_types_path:
            Path to ``agent_types.py``.  When given, the live Agent ABC source
            is embedded in the prompt; otherwise a built-in fallback is used.
        model:
            MiniMax model identifier (default: ``"MiniMax-M2.7"``).
        max_tokens:
            Maximum tokens in the model response (default: 4096).

    Returns:
        :class:`~agent_codegen.models.GenerationResult` with the generated
        agent id, code, optional file path, and source flag.

    Raises:
        AgentCodegenError:
            If *missing_capabilities* is empty, the model produces invalid
            code, or any API error occurs.
    """
    if not missing_capabilities:
        raise AgentCodegenError(
            "missing_capabilities is empty — nothing to generate. "
            "Run semantic_skill_analyzer first to identify a gap."
        )

    # analyzer.py enforces at most one key; take the first.
    agent_id = next(iter(missing_capabilities))
    agent_spec = missing_capabilities[agent_id]

    _LOG.info("Generating agent %r from spec: %s", agent_id, list(agent_spec.keys()))

    # --- Load live context from the caller's codebase ---
    agent_interface = load_agent_interface(agent_types_path)
    agent_examples = load_agent_examples(agents_dir)

    # --- Build prompt ---
    prompt = build_prompt(
        agent_id,
        agent_spec,
        agent_interface=agent_interface,
        agent_examples=agent_examples,
    )

    # --- Call MiniMax ---
    client = MiniMaxClient(api_key=api_key, model=model, max_tokens=max_tokens)
    code, via_tool = client.generate(prompt)

    # --- Validate structural correctness ---
    if not _validate_code(code):
        raise AgentCodegenError(
            f"Generated code for {agent_id!r} failed structural validation: "
            f"must contain {_VALIDATION_TOKENS!r}. "
            "The model may have produced incomplete output — try again."
        )

    # --- Write to disk (optional) ---
    file_path: Optional[str] = None
    if output_path is not None:
        dest = Path(output_path)
        if dest.exists():
            bak = dest.with_suffix(".py.bak")
            _LOG.warning("Output file already exists — renaming to %s", bak)
            dest.rename(bak)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(code, encoding="utf-8")
        file_path = str(dest)
        _LOG.info("Agent written to %s", file_path)

    return GenerationResult(
        agent_id=agent_id,
        code=code,
        file_path=file_path,
        via_tool=via_tool,
    )
