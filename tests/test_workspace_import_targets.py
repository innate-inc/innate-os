# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Catch dangling shipped agent/skill imports without requiring ROS hardware."""

import ast
import importlib.util
import symtable
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
SHIPPED_PACKAGES = {"innate_agents", "innate_skills"}


def missing_import_targets(workspace):
    missing = []
    for source in sorted(workspace.rglob("*.py")):
        package = ".".join(source.parent.relative_to(workspace).parts)
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    try:
                        module = importlib.util.resolve_name("." * node.level + module, package)
                    except (ImportError, ValueError):
                        missing.append(f"{source.relative_to(workspace)}:{node.lineno}: invalid relative import")
                        continue
                modules = [module]
                if node.module is None:
                    # `from . import helper` can import a submodule or a name
                    # bound by __init__.py. Inspect bindings without running it.
                    init = workspace.joinpath(*module.split("."), "__init__.py")
                    exports = set()
                    if init.is_file():
                        init_tree = ast.parse(init.read_text())
                        if init == source:
                            # The import under test cannot prove its own target exists.
                            init_tree.body = [stmt for stmt in init_tree.body if stmt.lineno != node.lineno]
                        symbols = symtable.symtable(ast.unparse(init_tree), str(init), "exec").get_symbols()
                        exports = {s.get_name() for s in symbols if s.is_assigned() or s.is_imported()}
                    modules += [f"{module}.{a.name}" for a in node.names if a.name != "*" and a.name not in exports]
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
                target = workspace.joinpath(*parts)
                if not target.is_dir() and not target.with_suffix(".py").is_file():
                    missing.append(f"{source.relative_to(workspace)}:{node.lineno}: {module}")
    return missing


def test_shipped_workspace_import_targets_exist():
    assert list(WORKSPACE.rglob("*.py")), "No workspace sources found"
    missing = missing_import_targets(WORKSPACE)
    assert not missing, "Missing shipped modules:\n" + "\n".join(missing)


@pytest.mark.parametrize("source_name", ["consumer.py", "__init__.py"])
@pytest.mark.parametrize("statement", ["from . import helper", "from .helper import Skill", "from .. import helper"])
def test_relative_import_detects_deleted_module(tmp_path, source_name, statement):
    package = tmp_path / "innate_skills" / "arm"
    package.mkdir(parents=True)
    source = package / source_name
    source.write_text(statement)
    helper = (package.parent if statement.startswith("from .. ") else package) / "helper.py"
    helper.write_text("class Skill: pass\n")
    assert missing_import_targets(tmp_path) == []
    helper.unlink()
    assert len(missing_import_targets(tmp_path)) == 1
    assert "helper" in missing_import_targets(tmp_path)[0]


def test_relative_import_accepts_package_exports(tmp_path):
    package = tmp_path / "innate_skills"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n")
    (package / "consumer.py").write_text("from . import VALUE\n")
    assert missing_import_targets(tmp_path) == []
