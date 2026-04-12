"""
agent_codegen.pipeline — End-to-end agent + skill code generation pipeline.

Chains:
    1. semantic_skill_analyzer.analyze()       → missing-skills JSON  (ollama, default)
    2. semantic_skill_analyzer.analyze_gemma() → missing-skills JSON  (Gemini, fallback)
    3. agent_codegen.generate_agent()          → .py files in agents/ and skills/

Public API::

    from agent_codegen.pipeline import run_pipeline, PipelineResult

    result = run_pipeline("make the robot wave when it hears a name")
    if result.success and result.agent_file:
        print("Generated:", result.agent_file)

CLI usage::

    python -m agent_codegen.pipeline "make the robot dance"
    python -m agent_codegen.pipeline "patrol and alert on intruders" --dry-run
    python -m agent_codegen.pipeline "..." --use-gemma   # force Gemini analyzer
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path constants — this file lives at innate-os/agent_codegen/pipeline.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent   # innate-os/
_DEFAULT_AGENTS_DIR = _REPO_ROOT / "agents"
_DEFAULT_SKILLS_DIR = _REPO_ROOT / "skills"
_DEFAULT_AGENT_TYPES = (
    _REPO_ROOT
    / "ros2_ws"
    / "src"
    / "brain"
    / "brain_client"
    / "brain_client"
    / "agent_types.py"
)
_DEFAULT_SKILL_TYPES = (
    _REPO_ROOT
    / "ros2_ws"
    / "src"
    / "brain"
    / "brain_client"
    / "brain_client"
    / "skill_types.py"
)
_DEFAULT_CAPABILITIES = Path.home() / ".wildrobot" / "capabilities.json"
_DEFAULT_WILDROBOT_DIR = Path.home() / ".wildrobot"

# Reference examples for code generation style guidance
_AGENT_EXAMPLES = [
    _REPO_ROOT / "agents" / "draw_triangle.py",
    _REPO_ROOT / "agents" / "draw_circle_agent.py",
]
_SKILL_EXAMPLES = [
    _REPO_ROOT / "skills" / "draw_triangle.py",
    _REPO_ROOT / "skills" / "draw_circle.py",
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """Outcome of a full analyze → generate run.

    Attributes:
        success:              True when the pipeline completed without error.
        agent_id:             Snake-case ID of the generated agent, or None.
        agent_file:           Absolute path to the written agent .py, or None.
        skill_files:          Absolute paths of written skill .py files.
        missing_capabilities: Raw dict from the analyzer (always set on success).
        error:                Exception message if the pipeline raised, else None.
    """

    success: bool
    agent_id: Optional[str] = None
    agent_file: Optional[str] = None
    skill_files: list[str] = field(default_factory=list)
    missing_capabilities: dict = field(default_factory=dict)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_api_key(name: str, value: Optional[str], *env_vars: str) -> str:
    """Return *value* if truthy, else check env_vars in order, else raise."""
    if value:
        return value
    for var in env_vars:
        v = os.environ.get(var)
        if v:
            return v
    raise ValueError(
        f"{name} not provided and none of {env_vars!r} found in environment"
    )


def _existing_paths(paths: list[Path]) -> list[str]:
    """Return string paths for files that actually exist on disk."""
    return [str(p) for p in paths if p.exists()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_pipeline(
    prompt: str,
    *,
    minimax_api_key: Optional[str] = None,
    ollama_host: Optional[str] = None,
    ollama_model: Optional[str] = None,
    gemini_api_key: Optional[str] = None,
    use_gemma: bool = False,
    agents_dir: Optional[str] = None,
    skills_dir: Optional[str] = None,
    capabilities_path: Optional[str] = None,
    agent_types_path: Optional[str] = None,
    skill_types_path: Optional[str] = None,
    wildrobot_dir: Optional[str] = None,
    dry_run: bool = False,
) -> PipelineResult:
    """Run the full analyze → generate pipeline.

    Step 1: Try ``analyze()`` (ollama/qwen3, local, no API key needed).
            On failure, fall back to ``analyze_gemma()`` (Google GenAI).
    Step 2: Read the written missing-skills JSON.
    Step 3: If ``missing_capabilities`` is empty → return success with no files
            (task already covered by existing agents).
    Step 4: If ``dry_run`` → return early with ``agent_id`` only, no .py files.
    Step 5: Call ``generate_agent()`` → write agent + skill .py files.
    Step 6: Return :class:`PipelineResult`.

    API keys are resolved from explicit arguments first, then env vars:
      - ``MINIMAX_API_KEY``
      - ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``  (only needed as fallback)

    Args:
        prompt:           Natural-language user request.
        minimax_api_key:  MiniMax API key for code generation.
        ollama_host:      Ollama base URL (default: from analyzer module constant).
        ollama_model:     Ollama model name (default: OLLAMA_MODEL env var or qwen3:1.7b).
        gemini_api_key:   Gemini API key; only used if ollama fails or use_gemma=True.
        use_gemma:        Skip ollama and go straight to Gemini.
        agents_dir:       Directory to write agent .py files.
        skills_dir:       Directory to write skill .py files.
        capabilities_path: Path to capabilities.json.
        agent_types_path: Path to agent_types.py ABC source.
        skill_types_path: Path to skill_types.py ABC source.
        wildrobot_dir:    Directory where missing-skills JSON files are written.
        dry_run:          Analyze only — do not generate or write .py files.

    Returns:
        :class:`PipelineResult`
    """
    # Ensure sibling packages are importable (repo root on sys.path)
    _repo = str(_REPO_ROOT)
    if _repo not in sys.path:
        sys.path.insert(0, _repo)

    # Resolve paths
    _agents_dir = Path(agents_dir) if agents_dir else _DEFAULT_AGENTS_DIR
    _skills_dir = Path(skills_dir) if skills_dir else _DEFAULT_SKILLS_DIR
    _caps_path = Path(capabilities_path) if capabilities_path else _DEFAULT_CAPABILITIES
    _agent_types = Path(agent_types_path) if agent_types_path else _DEFAULT_AGENT_TYPES
    _skill_types = Path(skill_types_path) if skill_types_path else _DEFAULT_SKILL_TYPES
    _wildrobot = Path(wildrobot_dir) if wildrobot_dir else _DEFAULT_WILDROBOT_DIR

    # Guard: capabilities.json must exist for the analyzer
    caps_resolved = _caps_path.expanduser()
    if not caps_resolved.exists():
        return PipelineResult(
            success=False,
            error=(
                f"capabilities.json not found at {_caps_path}. "
                "Start the brain once (innate start) so initialize_agents() creates it."
            ),
        )
    _LOG.debug("capabilities.json: %s", caps_resolved)

    # Step 1 — Analyze
    from semantic_skill_analyzer.analyzer import (  # noqa: PLC0415
        analyze,
        analyze_gemma,
        DEFAULT_OLLAMA_HOST,
        MODEL as _DEFAULT_MODEL,
    )

    _ollama_host = ollama_host or DEFAULT_OLLAMA_HOST
    _ollama_model = ollama_model or os.environ.get("OLLAMA_MODEL") or _DEFAULT_MODEL
    out_path: Optional[str] = None

    if not use_gemma:
        _LOG.info("─ step 1/3  analyze (ollama @ %s  model=%s)", _ollama_host, _ollama_model)
        t0 = time.monotonic()
        try:
            out_path = analyze(
                prompt,
                capabilities_path=str(caps_resolved),
                output_dir=str(_wildrobot),
                ollama_host=_ollama_host,
                model=_ollama_model,
            )
            _LOG.info("  ✓ ollama  %.1fs  → %s", time.monotonic() - t0, out_path)
        except Exception as ollama_err:
            _LOG.warning(
                "  ✗ ollama failed (%.1fs): %s — falling back to Gemini",
                time.monotonic() - t0,
                ollama_err,
            )

    if out_path is None:
        try:
            _gemini_key = _resolve_api_key(
                "gemini_api_key", gemini_api_key, "GEMINI_API_KEY", "GOOGLE_API_KEY"
            )
        except ValueError as exc:
            return PipelineResult(success=False, error=str(exc))
        _LOG.info("─ step 1/3  analyze (Gemini)")
        t0 = time.monotonic()
        try:
            out_path = analyze_gemma(
                prompt,
                capabilities_path=str(caps_resolved),
                output_dir=str(_wildrobot),
                api_key=_gemini_key,
            )
            _LOG.info("  ✓ Gemini  %.1fs  → %s", time.monotonic() - t0, out_path)
        except Exception as exc:
            return PipelineResult(
                success=False, error=f"Analysis failed: {exc}"
            )

    # Step 2 — Read analysis output
    try:
        data = json.loads(Path(out_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return PipelineResult(
            success=False, error=f"Cannot read analysis output: {exc}"
        )

    missing = data.get("missing_capabilities", {})
    existing = data.get("existing_agents", [])

    if existing:
        _LOG.info("─ step 2/3  existing agents cover this: %s", existing)
    if missing:
        for agent_id, spec in missing.items():
            new_skills = spec.get("new_skills", [])
            reuse = spec.get("existing_skills", [])
            _LOG.info(
                "─ step 2/3  missing agent: %r  new_skills=%d  reuse=%d",
                agent_id, len(new_skills), len(reuse),
            )
            for s in new_skills:
                name = s.get("skill_name") or next(iter(s), "") if isinstance(s, dict) else s
                _LOG.info("              + new skill: %s", name)
            for s in reuse:
                _LOG.info("              ✓ reuse:     %s", s)
    if not existing and not missing:
        _LOG.info("─ step 2/3  model returned empty result (no existing, no missing)")

    # Step 3 — Nothing to generate
    if not missing:
        _LOG.info("  ✓ task already covered — nothing to generate")
        return PipelineResult(success=True, missing_capabilities=missing)

    # Step 4 — Dry run: return agent_id only, no files
    if dry_run:
        agent_id = next(iter(missing))
        _LOG.info("  dry_run=True — skipping codegen for %r", agent_id)
        return PipelineResult(
            success=True, agent_id=agent_id, missing_capabilities=missing
        )

    # Step 5 — Resolve MiniMax key and generate
    try:
        _minimax_key = _resolve_api_key(
            "minimax_api_key", minimax_api_key, "MINIMAX_API_KEY"
        )
    except ValueError as exc:
        return PipelineResult(success=False, error=str(exc))

    agent_id = next(iter(missing))
    agent_output = str(_agents_dir / f"{agent_id}.py")
    _LOG.info("─ step 3/3  codegen  agent=%r  model=%s", agent_id, "MiniMax")
    t0 = time.monotonic()

    try:
        from agent_codegen.generator import generate_agent  # noqa: PLC0415

        result = generate_agent(
            missing_capabilities=missing,
            api_key=_minimax_key,
            output_path=agent_output,
            agents_dir=str(_agents_dir),
            agent_types_path=str(_agent_types) if _agent_types.exists() else None,
            skills_dir=str(_skills_dir),
            skill_types_path=str(_skill_types) if _skill_types.exists() else None,
            capabilities_path=str(caps_resolved) if caps_resolved.exists() else None,
            agent_example_paths=_existing_paths(_AGENT_EXAMPLES) or None,
            skill_example_paths=_existing_paths(_SKILL_EXAMPLES) or None,
        )
    except Exception as exc:
        return PipelineResult(
            success=False,
            missing_capabilities=missing,
            error=f"Code generation failed: {exc}",
        )

    skill_files = [s.file_path for s in result.skills if s.file_path]
    _LOG.info(
        "  ✓ codegen done  %.1fs  agent=%s  skills=%s",
        time.monotonic() - t0,
        result.file_path,
        skill_files,
    )

    return PipelineResult(
        success=True,
        agent_id=result.agent_id,
        agent_file=result.file_path,
        skill_files=skill_files,
        missing_capabilities=missing,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cli_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m agent_codegen.pipeline",
        description="Run the full analyze → generate pipeline.",
    )
    parser.add_argument("prompt", help="Natural-language user request.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze only — do not generate or write code.",
    )
    parser.add_argument(
        "--use-gemma",
        action="store_true",
        help="Force Gemini analyzer instead of ollama.",
    )
    parser.add_argument("--agents-dir", default=None)
    parser.add_argument("--skills-dir", default=None)
    parser.add_argument("--capabilities-path", default=None)
    parser.add_argument(
        "--ollama-host",
        default=None,
        help="Ollama base URL (default: value from analyzer, e.g. http://localhost:11434).",
    )
    parser.add_argument(
        "--ollama-model",
        default=None,
        help="Ollama model name (default: OLLAMA_MODEL env var or qwen3:1.7b).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level output (full prompt text, generated code preview).",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    result = run_pipeline(
        args.prompt,
        dry_run=args.dry_run,
        use_gemma=args.use_gemma,
        agents_dir=args.agents_dir,
        skills_dir=args.skills_dir,
        capabilities_path=args.capabilities_path,
        ollama_host=args.ollama_host,
        ollama_model=args.ollama_model,
    )

    if not result.success:
        print(f"Pipeline failed: {result.error}", file=sys.stderr)
        sys.exit(1)

    if not result.missing_capabilities:
        print("Task already covered by existing agents — nothing to generate.")
        sys.exit(0)

    if args.dry_run:
        print(f"[dry-run] Would generate agent: {result.agent_id}")
        sys.exit(0)

    print(f"Agent generated: {result.agent_id}")
    if result.agent_file:
        print(f"  File:  {result.agent_file}")
    for sf in result.skill_files:
        print(f"  Skill: {sf}")


if __name__ == "__main__":
    _cli_main()
