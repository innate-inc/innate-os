# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Static checks on a drafted skill file, before it touches the workspace.

A lint, not a sandbox: it catches the mistakes the coder is prone to and the obvious
escape hatches (reflection, dunders, module hopping, import aliases). An accepted file
runs with the same privileges as any other workspace skill."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from brain_client.common.dynamic_loader import class_name_to_snake_case

ALLOWED_IMPORTS = frozenset(
    {"innate", "innate_skills", "collections", "dataclasses", "enum", "json", "math", "random", "time", "typing"}
)
BANNED_NAMES = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "input",
        "sleep",  # `from time import sleep`: time.sleep ignores Stop
    }
)
# Attributes that turn an allowed module into a door (typing.sys, json.decoder, ...).
BANNED_ATTRS = frozenset(
    {"sys", "os", "subprocess", "builtins", "importlib", "socket", "shutil", "ctypes", "modules", "smtplib", "decoder"}
)


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
    own_privates = _declared_privates(tree)
    for node in ast.walk(tree):
        try:
            _check_node(node, own_privates)
        except DraftRejected as rejected:
            where = f"line {getattr(node, 'lineno', '?')}: {(ast.get_source_segment(source, node) or '').strip()}"
            raise DraftRejected(f"{rejected} ({where})") from None
    skills = [node for node in tree.body if isinstance(node, ast.ClassDef) and _subclasses_skill(node)]
    if len(skills) != 1:
        raise DraftRejected("the file must define exactly one class that subclasses Skill")
    _check_execute(skills[0])
    return Draft(skills[0].name, source)


def _check_node(node: ast.AST, own_privates: frozenset[str]) -> None:
    if isinstance(node, ast.Import | ast.ImportFrom):
        _check_import(node)
    elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
        raise DraftRejected(f"'{node.id}' is not allowed in a skill")
    elif isinstance(node, ast.Attribute) and _escapes(node, own_privates):
        raise DraftRejected(f"'.{node.attr}' is not allowed in a skill")
    elif isinstance(node, ast.Call) and _dotted(node.func) == "time.sleep":
        raise DraftRejected("time.sleep() is not allowed: use self.sleep(seconds), time.sleep ignores Stop")


def _check_import(node: ast.Import | ast.ImportFrom) -> None:
    names = node.names
    roots = [(node.module or "").split(".")[0]] if isinstance(node, ast.ImportFrom) else []
    roots += [alias.name.split(".")[0] for alias in names] if isinstance(node, ast.Import) else []
    for root in roots:
        if root not in ALLOWED_IMPORTS:
            raise DraftRejected(f"'{root}' may not be imported; allowed: {', '.join(sorted(ALLOWED_IMPORTS))}")
    for alias in names:
        if alias.asname is not None:
            raise DraftRejected("import aliases ('as') are not allowed")
        if alias.name in BANNED_NAMES or alias.name in BANNED_ATTRS:
            raise DraftRejected(f"'{alias.name}' may not be imported")


def _declared_privates(tree: ast.Module) -> frozenset[str]:
    """Private names the draft itself defines: its own methods and the attributes it assigns on self."""
    methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    fields = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and _is_self(node.value)
    }
    return frozenset(name for name in methods | fields if name.startswith("_") and not name.startswith("__"))


def _escapes(node: ast.Attribute, own_privates: frozenset[str]) -> bool:
    """Dunders anywhere, privates other than the draft's own on self, and the module doors."""
    own = _is_self(node.value) and node.attr in own_privates
    return (node.attr.startswith("_") and not own) or node.attr in BANNED_ATTRS


def _is_self(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Name) and expr.id == "self"


def _dotted(expr: ast.expr) -> str:
    """'time.sleep' for ``time.sleep(...)``, '' for anything that is not a plain dotted name."""
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
