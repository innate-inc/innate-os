# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Static checks on a drafted skill file, before it touches the workspace."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from brain_client.common.dynamic_loader import class_name_to_snake_case

ALLOWED_IMPORTS = frozenset(
    {"innate", "innate_skills", "collections", "dataclasses", "enum", "json", "math", "random", "time", "typing"}
)
BANNED_CALLS = {
    "time.sleep": "use self.sleep(seconds); time.sleep ignores Stop",
    "open": "skills never touch files; keep state in self.storage",
    "exec": "no dynamic code",
    "eval": "no dynamic code",
    "__import__": "no dynamic imports",
}


class DraftRejected(Exception):
    """The draft cannot be installed; the message tells the coder why."""


@dataclass(frozen=True)
class Draft:
    class_name: str
    source: str

    @property
    def module(self) -> str:
        return class_name_to_snake_case(self.class_name)

    @property
    def skill_id(self) -> str:
        return f"local/{self.module}"

    @property
    def display_name(self) -> str:
        return self.module.replace("_", " ")


def check(source: str) -> Draft:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise DraftRejected(f"syntax error on line {error.lineno}: {error.msg}") from None
    for node in ast.walk(tree):
        _check_node(node)
    skills = [node for node in tree.body if isinstance(node, ast.ClassDef) and _subclasses_skill(node)]
    if len(skills) != 1:
        raise DraftRejected("the file must define exactly one class that subclasses Skill")
    _check_execute(skills[0])
    return Draft(skills[0].name, source)


def _check_node(node: ast.AST) -> None:
    if isinstance(node, ast.Import | ast.ImportFrom):
        for root in _import_roots(node):
            if root not in ALLOWED_IMPORTS:
                raise DraftRejected(f"'{root}' may not be imported; allowed: {', '.join(sorted(ALLOWED_IMPORTS))}")
    if isinstance(node, ast.Call):
        called = _dotted(node.func)
        if called in BANNED_CALLS:
            raise DraftRejected(f"{called}() is not allowed: {BANNED_CALLS[called]}")


def _import_roots(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [(node.module or "").split(".")[0]]
    return [alias.name.split(".")[0] for alias in node.names]


def _dotted(expr: ast.expr) -> str:
    """'time.sleep' for ``time.sleep(...)``, 'open' for ``open(...)``, '' for anything else."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return ""
    parts.append(expr.id)
    return ".".join(reversed(parts))


def _subclasses_skill(cls: ast.ClassDef) -> bool:
    return any(_dotted(base).rpartition(".")[2] == "Skill" for base in cls.bases)


def _check_execute(cls: ast.ClassDef) -> None:
    execute = next((node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "execute"), None)
    if execute is None:
        raise DraftRejected("the skill class needs an execute() method")
    parameters = len(execute.args.args) - 1 + len(execute.args.kwonlyargs)
    defaults = len(execute.args.defaults) + sum(default is not None for default in execute.args.kw_defaults)
    if defaults < parameters:
        raise DraftRejected("every execute() parameter needs a default, so the skill can be tried without inputs")
