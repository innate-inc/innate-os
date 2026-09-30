# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Static checks on a drafted skill file, before it touches the workspace.

A lint, not a sandbox: it catches the mistakes the coder is prone to and the obvious
escape hatches (reflection, dunders, module hopping, import aliases). An accepted file
runs with the same privileges as any other workspace skill."""

from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import dataclass

from brain_client.common.dynamic_loader import class_name_to_snake_case

ALLOWED_IMPORTS = frozenset(
    {
        "innate",
        "innate_skills",
        "collections",
        "dataclasses",
        "enum",
        "json",
        "math",
        "pydantic",
        "random",
        "time",
        "typing",
    }
)
# What may run at import time: the skills server imports the file synchronously during its reload.
INERT_STATEMENTS = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.Assign, ast.AnnAssign, ast.Pass)
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
# Attributes that turn an allowed module into a door (typing.sys, json.decoder, dataclasses.inspect,
# json.codecs.open ...): the escape modules, and the banned builtins reached as attributes.
BANNED_ATTRS = frozenset(
    {
        "sys",
        "os",
        "subprocess",
        "builtins",
        "importlib",
        "socket",
        "shutil",
        "ctypes",
        "modules",
        "smtplib",
        "decoder",
        "inspect",
        "codecs",
        "io",
        "pathlib",
        "types",
        "gc",
    }
) | (BANNED_NAMES - {"sleep"})


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
    _check_import_time(tree.body)
    own_privates = _declared_privates(tree)
    modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    dotted = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        try:
            _check_node(node, own_privates, modules, dotted)
        except DraftRejected as rejected:
            where = f"line {getattr(node, 'lineno', '?')}: {(ast.get_source_segment(source, node) or '').strip()}"
            raise DraftRejected(f"{rejected} ({where})") from None
    skills = [node for node in tree.body if isinstance(node, ast.ClassDef) and _subclasses_skill(node)]
    if len(skills) != 1:
        raise DraftRejected("the file must define exactly one class that subclasses Skill")
    _check_execute(skills[0])
    return Draft(skills[0].name, source)


def _check_node(node: ast.AST, own_privates: frozenset[str], modules: set[str], dotted: set[int]) -> None:
    """``modules`` are the names ``import x`` bound; ``dotted`` the ids of every ``x`` in an ``x.y``:
    a module used any other way (``t = time``) is an alias that hides what the checks below look for."""
    if isinstance(node, ast.Import | ast.ImportFrom):
        _check_import(node)
    elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
        raise DraftRejected(f"'{node.id}' is not allowed in a skill")
    elif isinstance(node, ast.Name) and node.id in modules and id(node) not in dotted:
        raise DraftRejected(f"'{node.id}' may only be used as {node.id}.<name>, never aliased or passed around")
    elif isinstance(node, ast.Attribute) and _dotted(node) == "time.sleep":
        raise DraftRejected("time.sleep is not allowed: use self.sleep(seconds), time.sleep ignores Stop")
    elif isinstance(node, ast.Attribute) and _escapes(node, own_privates):
        raise DraftRejected(f"'.{node.attr}' is not allowed in a skill")


def _check_import_time(body: list[ast.stmt]) -> None:
    """Module and class bodies execute inside the skills server's reload: declarations only, no calls."""
    for statement in body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue  # a docstring
        if not isinstance(statement, INERT_STATEMENTS):
            raise DraftRejected(
                f"only imports, assignments, and definitions may run at import time (line {statement.lineno})"
            )
        if isinstance(statement, ast.ClassDef):
            _check_import_time(statement.body)
            evaluated = [
                *statement.bases,
                *(keyword.value for keyword in statement.keywords),
                *statement.decorator_list,
            ]
        elif isinstance(statement, ast.FunctionDef):
            defaults = [default for default in statement.args.kw_defaults if default is not None]
            evaluated = [*statement.decorator_list, *statement.args.defaults, *defaults]
        else:
            value = getattr(statement, "value", None)
            evaluated = [value] if value is not None else []
        if any(isinstance(node, ast.Call) for expression in evaluated for node in ast.walk(expression)):
            raise DraftRejected(f"no calls at import time (line {statement.lineno}); move it into a method")


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
    package = node.module if isinstance(node, ast.ImportFrom) else None
    imported = [f"{package}.{alias.name}" if package else alias.name for alias in names]
    for name in (package, *imported):
        if name and name.startswith("innate_skills.") and not _resolvable(name):
            raise DraftRejected(f"there is no {name}; only the skills and helpers listed in the prompt exist")


def _resolvable(name: str) -> bool:
    """A module find_spec locates, or what an imported module defines (``from innate_skills.x import Cls``)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        pass  # the parent is missing, or a plain module the name must then be an attribute of
    parent, _, attribute = name.rpartition(".")
    try:
        module = sys.modules.get(parent) or importlib.import_module(parent)
    except Exception:  # noqa: BLE001 — a parent that cannot import is as good as missing
        return False
    return hasattr(module, attribute)


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
