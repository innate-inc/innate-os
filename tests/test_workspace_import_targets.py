# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Catch dangling shipped agent/skill imports without requiring ROS hardware."""

import ast
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
SHIPPED_PACKAGES = {"innate_agents", "innate_skills"}


def test_shipped_workspace_import_targets_exist():
    sources = sorted(WORKSPACE.rglob("*.py"))
    assert sources, "No workspace sources found"
    missing = []
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            for module in modules:
                parts = module.split(".")
                if parts[0] == "workspace":
                    parts = parts[1:]
                if not parts or parts[0] not in SHIPPED_PACKAGES:
                    continue
                target = WORKSPACE.joinpath(*parts)
                if not target.is_dir() and not target.with_suffix(".py").is_file():
                    missing.append(f"{source.relative_to(WORKSPACE)}:{node.lineno}: {module}")
    assert not missing, "Missing shipped modules:\n" + "\n".join(missing)
