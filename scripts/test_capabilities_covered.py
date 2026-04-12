#!/usr/bin/env python3
"""
Test script 2 — Verify existing capabilities coverage.

Runs the semantic skill analyzer with a prompt that the robot can ALREADY
handle with existing agents and skills. Expected result: existing agents
are listed and no new capabilities are needed.

Usage:
    conda run -n local_llm python3 scripts/test_capabilities_covered.py
    conda run -n local_llm python3 scripts/test_capabilities_covered.py "your prompt here"
"""

import json
import os
import sys
from pathlib import Path

_INNATE_ROOT = os.environ.get("INNATE_OS_ROOT", str(Path(__file__).parent.parent))
if _INNATE_ROOT not in sys.path:
    sys.path.insert(0, _INNATE_ROOT)

from semantic_skill_analyzer import analyze

# Prompt describes tasks the robot already has skills for:
#   navigate_to_position + wave  →  demo_agent
DEFAULT_PROMPT = (
    "Navigate the robot to coordinates x=1.5, y=2.0 facing north, "
    "then wave hello at the person standing there."
)

prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT

print("=" * 60)
print("Semantic Skill Analyzer — existing coverage check")
print("=" * 60)
print(f"\nPrompt:\n  {prompt}\n")
print("Calling ollama (qwen3:0.6b)… this may take a few seconds.\n")

output_path = analyze(prompt)
data = json.loads(Path(output_path).read_text(encoding="utf-8"))

print(f"Output written to:\n  {output_path}\n")

existing = data.get("existing_agents", [])
missing = data.get("missing_capabilities", {})

# ── Result ──────────────────────────────────────────────────────────────────
if existing and not missing:
    print("✓ Request is fully covered by existing agents:")
    for agent in existing:
        print(f"    {agent}")
elif existing and missing:
    print("~ Request is partially covered:")
    print("  Existing agents:")
    for agent in existing:
        print(f"    ✓ {agent}")
    print("  Still missing:")
    for agent_name in missing:
        print(f"    ✗ {agent_name}")
else:
    print("✗ No existing agents cover this request.")
    if missing:
        print("  New agents needed:")
        for agent_name in missing:
            print(f"    • {agent_name}")

print("\n" + "=" * 60)
