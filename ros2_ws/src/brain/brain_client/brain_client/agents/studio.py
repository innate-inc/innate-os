# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The agent detail form <-> file boundary for agents authored in the webapp.

An agent the form may rewrite is one whose file holds nothing beyond the form:
from-imports, one ``Agent`` subclass, and literal-returning ``id`` /
``display_name`` / ``get_skills`` / ``get_inputs`` / ``get_prompt`` /
``uses_gaze`` (:func:`conforms`). Such a file is re-rendered in the shipped
agents' style (:func:`render_agent`), so a round trip through the form changes
only what the form changed. Anything else is edited in code, not here.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from brain_client.common.script_paths import get_custom_agents_dir
from brain_client.skills.physical_refs import class_name_for

if TYPE_CHECKING:
    from brain_client.agents.types import Agent
    from brain_client.core.state import BrainState

SkillImport = tuple[str, str]  # (module, class name)

MICRO_NAME = "micro"
_MICRO_MODULE, _MICRO_CLASS = "inputs.micro_input", "MicroInput"
_FORM_METHODS = {"id", "display_name", "get_skills", "get_inputs", "get_prompt", "uses_gaze"}
_REQUIRED_METHODS = {"id", "display_name", "get_skills", "get_prompt"}
_PROPERTIES = {"id", "display_name"}
_STRING_METHODS = {"id", "display_name", "get_prompt"}
_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
_LINE_LENGTH = 120


class StudioError(Exception):
    """A refused save or delete; the message is shown to the user as-is."""


@dataclass(frozen=True)
class AgentSpec:
    id: str
    display_name: str
    prompt: str
    skill_ids: tuple[str, ...]
    listen: bool
    gaze: bool


def custom_agent_path(agent_id: str) -> Path:
    return get_custom_agents_dir() / f"{agent_id}.py"


def agent_file(agent: Agent) -> Path | None:
    try:
        return Path(inspect.getfile(type(agent)))
    except (TypeError, OSError):
        return None


def form_class(source: str) -> ast.ClassDef | None:
    """The one Agent subclass of a file that holds nothing the form cannot
    express, else None."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(classes) != 1 or not all(isinstance(node, (ast.ImportFrom, ast.ClassDef)) for node in tree.body):
        return None
    cls = classes[0]
    if not _is_agent_class(cls):
        return None
    body = cls.body[1:] if _is_docstring(cls.body[0]) else cls.body
    seen: set[str] = set()
    for item in body:
        if not isinstance(item, ast.FunctionDef) or item.name in seen or not _is_form_method(item):
            return None
        seen.add(item.name)
    return cls if _REQUIRED_METHODS <= seen else None


def conforms(source: str) -> bool:
    return form_class(source) is not None


def _is_agent_class(cls: ast.ClassDef) -> bool:
    if len(cls.bases) != 1 or cls.keywords or cls.decorator_list:
        return False
    base = cls.bases[0]
    return isinstance(base, ast.Name) and base.id == "Agent"


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _is_form_method(fn: ast.FunctionDef) -> bool:
    if fn.name not in _FORM_METHODS:
        return False
    decorators = [d.id for d in fn.decorator_list if isinstance(d, ast.Name)]
    expected = ["property"] if fn.name in _PROPERTIES else []
    if len(decorators) != len(fn.decorator_list) or decorators != expected:
        return False
    args = fn.args
    if [a.arg for a in args.args] != ["self"] or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg:
        return False
    body = fn.body[1:] if len(fn.body) > 1 and _is_docstring(fn.body[0]) else fn.body
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return False
    return _is_form_value(fn.name, body[0].value)


def _is_form_value(name: str, value: ast.expr) -> bool:
    if name in _STRING_METHODS:
        return _is_str(value)
    if name == "uses_gaze":
        return isinstance(value, ast.Constant) and isinstance(value.value, bool)
    if not isinstance(value, ast.List):
        return False
    if name == "get_inputs":
        return all(_is_micro_ref(item) for item in value.elts)
    return all(isinstance(item, ast.Name) or _is_str(item) for item in value.elts)


def _is_str(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and isinstance(value.value, str)


def _is_micro_ref(value: ast.expr) -> bool:
    if isinstance(value, ast.Name):
        return value.id == _MICRO_CLASS
    return isinstance(value, ast.Constant) and value.value == MICRO_NAME


def render_agent(spec: AgentSpec, imports: Mapping[str, SkillImport], docstring: str | None = None) -> str:
    """The shipped agents' shape, from a form. A skill with no importable class
    on the roster (or whose class name is already taken) stays an id string,
    which ``get_skills()`` accepts."""
    class_name = f"{class_name_for(spec.id)}Agent"
    imported: dict[str, str] = {}  # class name -> module
    refs: list[str] = []
    for skill_id in spec.skill_ids:
        module, name = imports.get(skill_id, ("", ""))
        clash = imported.get(name, module) != module or name == class_name
        if not module or not name or clash:
            refs.append(_str_literal(skill_id))
            continue
        imported[name] = module
        refs.append(name)
    if spec.listen:
        imported[_MICRO_CLASS] = _MICRO_MODULE
    types = "Agent, InputRef, SkillRef" if spec.listen else "Agent, SkillRef"

    lines = sorted(f"from {module} import {name}" for name, module in imported.items())
    if lines:
        lines.append("")
    lines += [
        f"from brain_client.agents.types import {types}",
        "",
        "",
        f"class {class_name}(Agent):",
        _docstring_block(docstring or f"{spec.display_name} - made in the webapp."),
        "",
        "    @property",
        "    def id(self) -> str:",
        f"        return {_str_literal(spec.id)}",
        "",
        "    @property",
        "    def display_name(self) -> str:",
        f"        return {_str_literal(spec.display_name)}",
        "",
        "    def get_skills(self) -> list[SkillRef]:",
        *_list_return(refs),
    ]
    if spec.listen:
        lines += ["", "    def get_inputs(self) -> list[InputRef]:", f"        return [{_MICRO_CLASS}]"]
    lines += ["", "    def get_prompt(self) -> str:", f"        return {_prompt_literal(spec.prompt)}"]
    if spec.gaze:
        lines += ["", "    def uses_gaze(self) -> bool:", "        return True"]
    return "\n".join(lines) + "\n"


def _list_return(refs: list[str]) -> list[str]:
    one_line = f"        return [{', '.join(refs)}]"
    if len(one_line) <= _LINE_LENGTH:
        return [one_line]
    return ["        return [", *(f"            {ref}," for ref in refs), "        ]"]


def _str_literal(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _escape_triple(text: str) -> str:
    """Escape for the inside of a triple-quoted literal. A trailing quote would
    merge with the closing delimiter; escape it unless it already is (an odd
    run of backslashes before it)."""
    escaped = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    trailing = re.search(r'(\\*)"$', escaped)
    if trailing and len(trailing.group(1)) % 2 == 0:
        escaped = escaped[:-1] + '\\"'
    return escaped


def _prompt_literal(prompt: str) -> str:
    if not prompt:
        return '""'
    return f'"""{_escape_triple(prompt)}"""'


def _docstring_block(text: str) -> str:
    body = "\n".join(f"    {line}".rstrip() for line in _escape_triple(text.strip()).splitlines())
    return f'    """\n{body}\n    """'


def studio_fields(agent: Agent) -> dict[str, str | bool]:
    """The roster fields the agent detail reads: the form values it cannot get
    from the agent list, the file to edit in code, and whether the form may
    rewrite that file."""
    path = agent_file(agent)
    code = _read(path)
    return {
        "listen": MICRO_NAME in agent.input_names(),
        "gaze": agent.uses_gaze(),
        "path": str(path) if path else "",
        "editable": agent.source == "user" and code is not None and conforms(code),
    }


def broken_agent_fields(name: str) -> dict[str, str]:
    """A broken row's file, when its name is a custom_agents module."""
    path = custom_agent_path(name)
    return {"path": str(path)} if path.exists() else {}


def _read(path: Path | None) -> str | None:
    """The file's text, or None when there is no readable file."""
    if path is None:
        return None
    try:
        return path.read_text()
    except OSError:
        return None


def save_agent(state: BrainState, spec: AgentSpec) -> tuple[Path, str]:
    """Write ``spec`` as its agent file and return the path and content. Refuses
    innate agents, taken ids, and rewriting a file edited in code from the form."""
    _valid_id(spec.id)
    existing = state.directives.get(spec.id)
    if existing is not None and existing.source == "shipped":
        raise StudioError(f"'{spec.id}' is an innate agent; create your own agent or edit it in code")
    path = agent_file(existing) if existing is not None else custom_agent_path(spec.id)
    if path is None:
        raise StudioError(f"cannot locate the file of '{spec.id}'")
    content = _render_over(state, spec, path, existing is None)
    # custom_agents is gitignored, so a fresh checkout (and the demo image built from one) has no such directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path, content


def _render_over(state: BrainState, spec: AgentSpec, path: Path, new: bool) -> str:
    current = _read(path)
    if new and (current is not None or spec.id in state.broken_agents):
        raise StudioError(f"'{spec.id}' is already taken; choose another name")
    cls = form_class(current) if current is not None else None
    if current is not None and cls is None:
        raise StudioError(f"{path.name} was edited in code; keep editing it there")
    imports = {
        skill_id: (meta.get("module", ""), meta.get("class_name", ""))
        for skill_id, meta in state.registry.primitives.items()
    }
    return render_agent(spec, imports, ast.get_docstring(cls) if cls is not None else None)


def _valid_id(agent_id: str) -> None:
    """The id is half a file path, so anything but a bare snake_case name is refused
    before it can reach outside custom_agents."""
    if _ID_RE.fullmatch(agent_id) is None:
        raise StudioError(f"'{agent_id}' is not a valid id: lowercase letters, digits and underscores")


def delete_agent(state: BrainState, agent_id: str) -> Path:
    _valid_id(agent_id)
    existing = state.directives.get(agent_id)
    if existing is None:
        path = custom_agent_path(agent_id)
        if not path.exists():
            raise StudioError(f"no agent '{agent_id}'")
        path.unlink()
        return path
    if existing.source == "shipped":
        raise StudioError(f"'{agent_id}' is an innate agent and cannot be deleted")
    path = agent_file(existing)
    if path is None:
        raise StudioError(f"cannot locate the file of '{agent_id}'")
    module = type(existing).__module__
    siblings = [a.id for a in state.directives.values() if type(a).__module__ == module and a.id != agent_id]
    if siblings:
        raise StudioError(f"{path.name} also defines {', '.join(siblings)}; delete it by hand")
    path.unlink()
    return path
