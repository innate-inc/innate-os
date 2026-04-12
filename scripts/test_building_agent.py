#!/usr/bin/env python3
"""
Test script — Build an Agent from a missing-skills spec.

Reads scripts/sample_missing_skills.json (produced by semantic_skill_analyzer),
calls agent_codegen.generate_agent via MiniMax M2.7, and writes the generated
Python file to the agents/ directory.

Usage:
    conda run -n local_llm python3 scripts/test_building_agent.py
    conda run -n local_llm python3 scripts/test_building_agent.py --dry-run

Options:
    --dry-run   Print the generated code without writing it to agents/

Requires:
    MINIMAX_API_KEY environment variable (or add it to .env)
"""

import json
import os
import sys
from pathlib import Path

# ── Repo root on sys.path so agent_codegen is importable without install ─────
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env if present (picks up MINIMAX_API_KEY set there)
_env_file = _ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass  # dotenv not installed; rely on shell env

from agent_codegen import AgentCodegenError, generate_agent

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPTS_DIR    = Path(__file__).parent
_SPEC_FILE      = _SCRIPTS_DIR / "sample_missing_skills.json"
_AGENTS_DIR     = _ROOT / "agents"
_AGENT_TYPES    = _ROOT / "ros2_ws" / "src" / "brain" / "brain_client" / "brain_client" / "agent_types.py"

# ── Parse flags ───────────────────────────────────────────────────────────────
dry_run = "--dry-run" in sys.argv

# ── Load spec ─────────────────────────────────────────────────────────────────
data = json.loads(_SPEC_FILE.read_text(encoding="utf-8"))
missing = data.get("missing_capabilities", {})

print("=" * 62)
print("agent_codegen — build agent from missing-skills spec")
print("=" * 62)
print(f"\nSpec file : {_SPEC_FILE.relative_to(_ROOT)}")
print(f"Agents dir: {_AGENTS_DIR.relative_to(_ROOT)}")
print(f"Dry run   : {dry_run}\n")

if not missing:
    print("missing_capabilities is empty — nothing to generate.")
    sys.exit(0)

agent_id = next(iter(missing))
agent_spec = missing[agent_id]
output_path = _AGENTS_DIR / f"{agent_id}.py"

print("Spec contents:")
print("-" * 40)
print(f"  Agent ID : {agent_id}")
print(f"  Prompt   : {agent_spec.get('prompt', '')}")
new_skills = agent_spec.get("new_skills", [])
if new_skills:
    print("  New skills:")
    for skill in new_skills:
        if isinstance(skill, dict):
            for k, v in skill.items():
                print(f"    • {k}: {v}")
print("-" * 40)

if dry_run:
    print(f"\nOutput would be written to: agents/{agent_id}.py")
else:
    print(f"\nOutput will be written to: agents/{agent_id}.py")

# ── API key ───────────────────────────────────────────────────────────────────
api_key = os.environ.get("MINIMAX_API_KEY", "")
if not api_key:
    print(
        "\nError: MINIMAX_API_KEY is not set.\n"
        "  Export it in your shell:  export MINIMAX_API_KEY=your_key\n"
        "  Or add it to .env:        MINIMAX_API_KEY=your_key",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Generate ──────────────────────────────────────────────────────────────────
print("\nCalling MiniMax M2.7 … (this may take 10-30 seconds)\n")

try:
    result = generate_agent(
        missing_capabilities=missing,
        api_key=api_key,
        output_path=None if dry_run else str(output_path),
        agents_dir=str(_AGENTS_DIR),
        agent_types_path=str(_AGENT_TYPES) if _AGENT_TYPES.exists() else None,
    )
except AgentCodegenError as exc:
    print(f"\nGeneration failed: {exc}", file=sys.stderr)
    sys.exit(1)

# ── Results ───────────────────────────────────────────────────────────────────
print("=" * 62)
print("Result")
print("=" * 62)
print(f"  Agent ID   : {result.agent_id}")
print(f"  Via tool   : {result.via_tool}")
print(f"  File path  : {result.file_path or '(not written — dry run)'}")
print(f"  Code length: {len(result.code)} chars\n")

print("Generated code:")
print("-" * 62)
print(result.code)
print("-" * 62)

if result.file_path:
    print(f"\nAgent written to: {result.file_path}")
    print("Restart brain_client to load the new agent automatically.")
