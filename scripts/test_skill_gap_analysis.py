#!/usr/bin/env python3
"""
Test script 1 — Skill gap analysis.

Calls the semantic skill analyzer with a scenario the robot likely cannot
fully handle yet, producing ~/.wildrobot/<uuid>-missing-skills.json.
Shows both existing agents that partially cover the request and new
agents/skills that need to be created.

Usage:
    conda run -n local_llm python3 scripts/test_skill_gap_analysis.py
    conda run -n local_llm python3 scripts/test_skill_gap_analysis.py "your prompt here"
"""

import json
import os
import sys
from pathlib import Path

_INNATE_ROOT = os.environ.get("INNATE_OS_ROOT", str(Path(__file__).parent.parent))
if _INNATE_ROOT not in sys.path:
    sys.path.insert(0, _INNATE_ROOT)

from semantic_skill_analyzer import analyze

DEFAULT_PROMPT = (
    "I want the robot to autonomously monitor a room for human presence, "
    "recognise known faces and greet them by name, detect unknown visitors and "
    "send a Telegram alert with a photo, keep a timestamped log of all entries "
    "and exits, and play a welcome or warning sound depending on whether the "
    "person is recognised."
)

prompt = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROMPT

print("=" * 60)
print("Semantic Skill Analyzer — gap analysis")
print("=" * 60)
print(f"\nPrompt:\n  {prompt}\n")
print("Calling ollama (qwen3:0.6b)… this may take a few seconds.\n")

output_path = analyze(prompt)
data = json.loads(Path(output_path).read_text(encoding="utf-8"))

print(f"Output written to:\n  {output_path}\n")

# ── Existing coverage ───────────────────────────────────────────────────────
existing = data.get("existing_agents", [])
print("Covered by existing agents:")
if existing:
    for agent in existing:
        print(f"  ✓ {agent}")
else:
    print("  (none)")

# ── Missing capabilities ────────────────────────────────────────────────────
missing = data.get("missing_capabilities", {})
print("\nMissing capabilities:")
if not missing:
    print("  (none — all required capabilities already exist)")
else:
    for agent_name, details in missing.items():
        print(f"\n  [New agent] {agent_name}")
        print(f"  Prompt: {details.get('prompt', '')}")
        new_skills = details.get("new_skills", [])
        if new_skills:
            print("  New skills:")
            for skill in new_skills:
                for skill_name, description in skill.items():
                    print(f"    • {skill_name}: {description}")
        else:
            print("  New skills: (none — uses existing skills only)")

print("\n" + "=" * 60)
