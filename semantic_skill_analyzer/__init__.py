"""
Semantic Skill Analyzer
=======================
Standalone library — zero dependency on brain_client, ROS, or innate-os internals.

Given a natural-language prompt and a capabilities index, asks a local LLM (ollama)
to identify missing agents/skills and writes a structured JSON file.

Public API:
    analyze(prompt, ...) -> str          # run analysis, return output file path
    load_capabilities(path) -> dict      # load capabilities.json
"""

from semantic_skill_analyzer.analyzer import analyze, load_capabilities

__all__ = ["analyze", "load_capabilities"]
