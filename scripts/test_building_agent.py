#!/usr/bin/env python3
"""
Test script — Build an Agent and its missing Skills from a spec.

Reads a missing-skills JSON produced by semantic_skill_analyzer, calls
agent_codegen.generate_agent via MiniMax M2.7, and writes the generated
Python agent to agents/ and any new skills to skills/.

Spec file resolution order:
  1. Explicit path passed as first non-flag argument
  2. Most recent *-missing-skills.json in ~/.wildrobot/
  3. scripts/sample_missing_skills.json  (static fallback)

Usage:
    conda run -n local_llm python3 scripts/test_building_agent.py
    conda run -n local_llm python3 scripts/test_building_agent.py --dry-run
    conda run -n local_llm python3 scripts/test_building_agent.py path/to/missing.json

Options:
    --dry-run   Print generated code without writing files

Requires:
    MINIMAX_API_KEY environment variable (or add it to .env)
"""

import json
import os
import sys
from pathlib import Path

# ── Repo root on sys.path ────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load .env if present
_env_file = _ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass

from agent_codegen import AgentCodegenError, generate_agent

# ── Paths ─────────────────────────────────────────────────────────────────────
_SCRIPTS_DIR    = Path(__file__).parent
_SAMPLE_SPEC    = _SCRIPTS_DIR / "sample_missing_skills.json"
_WILDROBOT_DIR  = Path.home() / ".wildrobot"
_AGENTS_DIR     = _ROOT / "agents"
_SKILLS_DIR     = _ROOT / "skills"
_AGENT_TYPES    = _ROOT / "ros2_ws" / "src" / "brain" / "brain_client" / "brain_client" / "agent_types.py"
_SKILL_TYPES    = _ROOT / "ros2_ws" / "src" / "brain" / "brain_client" / "brain_client" / "skill_types.py"
_CAPABILITIES   = Path.home() / ".wildrobot" / "capabilities.json"

# ── Parse flags ───────────────────────────────────────────────────────────────
dry_run = "--dry-run" in sys.argv
_positional = [a for a in sys.argv[1:] if not a.startswith("--")]

# ── Resolve spec file ─────────────────────────────────────────────────────────
def _latest_wildrobot_spec() -> Path | None:
    """Return the most recently modified *-missing-skills.json in ~/.wildrobot/."""
    candidates = sorted(
        _WILDROBOT_DIR.glob("*-missing-skills.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None

if _positional:
    _SPEC_FILE = Path(_positional[0]).expanduser().resolve()
else:
    _latest = _latest_wildrobot_spec()
    _SPEC_FILE = _latest if _latest is not None else _SAMPLE_SPEC

# ── Load spec ─────────────────────────────────────────────────────────────────
data = json.loads(_SPEC_FILE.read_text(encoding="utf-8"))
missing = data.get("missing_capabilities", {})

print("=" * 62)
print("agent_codegen — build agent + skills from missing-skills spec")
print("=" * 62)
try:
    _spec_display = _SPEC_FILE.relative_to(_ROOT)
except ValueError:
    _spec_display = _SPEC_FILE
print(f"\nSpec file : {_spec_display}")
print(f"Agents dir: {_AGENTS_DIR.relative_to(_ROOT)}")
print(f"Skills dir: {_SKILLS_DIR.relative_to(_ROOT)}")
print(f"Dry run   : {dry_run}\n")

if not missing:
    print("missing_capabilities is empty — nothing to generate.")
    sys.exit(0)

agent_id = next(iter(missing))
agent_spec = missing[agent_id]
new_skills = agent_spec.get("new_skills", [])
agent_output = _AGENTS_DIR / f"{agent_id}.py"

# ── Print spec summary ────────────────────────────────────────────────────────
print("Spec contents:")
print("-" * 40)
print(f"  Agent ID : {agent_id}")
print(f"  Prompt   : {agent_spec.get('prompt', '')}")
if new_skills:
    print("  New skills:")
    for skill in new_skills:
        if isinstance(skill, dict):
            if "skill_name" in skill and "description" in skill:
                print(f"    • {skill['skill_name']}: {skill['description']}")
            else:
                for k, v in skill.items():
                    print(f"    • {k}: {v}")
else:
    print("  New skills: (none)")
print("-" * 40)

if dry_run:
    print(f"\nAgent would be written to: agents/{agent_id}.py")
    for skill in new_skills:
        if isinstance(skill, dict):
            name = skill.get("skill_name") or next(iter(skill), "?")
            print(f"Skill would be written to: skills/{name}.py")
else:
    print(f"\nAgent will be written to: agents/{agent_id}.py")
    for skill in new_skills:
        if isinstance(skill, dict):
            name = skill.get("skill_name") or next(iter(skill), "?")
            print(f"Skill will be written to: skills/{name}.py")

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
print("\nCalling MiniMax M2.7 … (this may take 20-60 seconds)\n")

try:
    result = generate_agent(
        missing_capabilities=missing,
        api_key=api_key,
        output_path=None if dry_run else str(agent_output),
        agents_dir=str(_AGENTS_DIR),
        agent_types_path=str(_AGENT_TYPES) if _AGENT_TYPES.exists() else None,
        skills_dir=None if dry_run else str(_SKILLS_DIR),
        skill_types_path=str(_SKILL_TYPES) if _SKILL_TYPES.exists() else None,
        capabilities_path=str(_CAPABILITIES) if _CAPABILITIES.exists() else None,
    )
except AgentCodegenError as exc:
    print(f"\nGeneration failed: {exc}", file=sys.stderr)
    sys.exit(1)

# ── Agent result ──────────────────────────────────────────────────────────────
print("=" * 62)
print("Agent result")
print("=" * 62)
print(f"  Agent ID   : {result.agent_id}")
print(f"  Via tool   : {result.via_tool}")
print(f"  File path  : {result.file_path or '(not written — dry run)'}")
print(f"  Code length: {len(result.code)} chars\n")
print("Generated agent code:")
print("-" * 62)
print(result.code)
print("-" * 62)

# ── Skill results ─────────────────────────────────────────────────────────────
if result.skills:
    print(f"\n{'=' * 62}")
    print(f"Skills ({len(result.skills)} generated)")
    print("=" * 62)
    for skill in result.skills:
        print(f"\n  Skill      : {skill.skill_name}")
        print(f"  Via tool   : {skill.via_tool}")
        print(f"  File path  : {skill.file_path or '(not written — dry run or failed)'}")
        print(f"  Code length: {len(skill.code)} chars")
        if skill.code:
            print(f"\nGenerated skill code ({skill.skill_name}):")
            print("-" * 62)
            print(skill.code)
            print("-" * 62)
        else:
            print("  (generation failed — see logs above)")
elif new_skills:
    print("\n(Skills not generated — pass --write or remove --dry-run to generate)")

# ── Summary ───────────────────────────────────────────────────────────────────
if result.file_path or any(s.file_path for s in result.skills):
    print("\nFiles written:")
    if result.file_path:
        print(f"  Agent : {result.file_path}")
    for skill in result.skills:
        if skill.file_path:
            print(f"  Skill : {skill.file_path}")
    print("\nRestart brain_client to load the new agent and skills automatically.")
