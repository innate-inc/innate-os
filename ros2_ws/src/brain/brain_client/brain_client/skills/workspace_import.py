# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Import-based skill discovery: skills register by existing, not by being found.

The model: every directory under ``workspace/`` not claimed by other
machinery is a Python package. Discovery is a *real import* —
``workspace/`` goes on ``sys.path``, every module in every package is imported
with ``importlib.import_module``, and defining a ``Skill`` subclass registers
it (``Skill.__init_subclass__``), the way defining an ``nn.Module`` is all
PyTorch needs. No file execing, no filename-derived identity, no guessing
whether a file "meant" to be a skill:

- a module that imports cleanly and registers nothing **is** a helper;
- a module that raises **is** broken, keyed by its real module name with a
  real traceback;
- a module that registers classes contributes one skill per class —
  ``id = <namespace>/<snake_case(ClassName)>``.

Because these are ordinary imports, ordinary Python works: relative imports,
multi-skill files, helpers anywhere, packages importing each other by bare
name, and pip-installed packs registering on import (the torchvision pattern).

Hot reload = evict every cached module under ``workspace/`` (see
``evict_modules_under``), then import again.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import traceback
from pathlib import Path

from brain_client.common.dynamic_loader import class_name_to_snake_case
from brain_client.common.script_paths import (
    get_custom_skills_dir,
    get_innate_os_root,
    get_innate_skills_dir,
    get_workspace_dir,
    get_workspace_package_dirs,
)
from brain_client.skills.physical import has_physical_metadata
from brain_client.skills.types import Skill

# workspace package directory name -> skill-id namespace. The two standard
# dirs keep their historical prefixes so every persisted id keeps resolving;
# any other package namespaces by its own directory name.
_NAMESPACE_BY_PACKAGE = {"innate_skills": "innate-os", "custom_skills": "local"}


def ensure_import_roots() -> None:
    """Put the repo root and workspace/ on sys.path (idempotent).

    The root makes ``workspace.*`` imports resolve (compat with existing
    skills); workspace/ itself makes packages importable by bare name
    (``from innate_skills import navigate_to_position``, ``import custom_skills.geometry``).
    """
    for path in (str(get_innate_os_root()), str(get_workspace_dir())):
        if path not in sys.path:
            sys.path.insert(0, path)


def _iter_module_names(directory: Path, prefix: str):
    """Dotted module names for every importable module under ``directory``.

    Hidden and ``_``-prefixed names are skipped from *proactive* import (they
    still import fine transitively — same convention as pytest); a directory
    with a non-empty ``metadata.json`` is a physical skill's data, not a
    package (see ``has_physical_metadata`` for why empties don't count).
    """
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_file() and entry.suffix == ".py":
            yield f"{prefix}.{entry.stem}"
        elif entry.is_dir() and not has_physical_metadata(entry):
            yield f"{prefix}.{entry.name}"
            yield from _iter_module_names(entry, f"{prefix}.{entry.name}")


def import_workspace_packages(logger) -> dict[str, str]:
    """Import every module in every skill package.

    Returns ``{module_name: error}`` for modules that failed — the catalog
    rosters these as broken so nothing silently vanishes. Modules already in
    ``sys.modules`` are cheap no-ops; a reload evicts first.
    """
    return import_packages([get_innate_skills_dir(), get_custom_skills_dir(), *get_workspace_package_dirs()], logger)


def unique_key(taken: dict, base: str) -> str:
    """``base``, or ``base.2``, ``base.3``… — the first key not in ``taken``.

    Every broken-roster merge goes through this so no entry can silently
    shadow another (class over module, module over module, probe over import);
    a collision costs a numbered suffix, never a vanished error.
    """
    key, n = base, 2
    while key in taken:
        key, n = f"{base}.{n}", n + 1
    return key


def format_load_error(e: BaseException) -> str:
    """``<file>:<line>: ExcType: msg``, pointing at the deepest workspace frame
    so the roster error names the user's file, not just the exception.
    Falls back to plain ``ExcType: msg`` when no workspace frame is involved
    (e.g. instantiating an abstract class)."""
    workspace = get_workspace_dir()
    location = ""
    # lookup_lines=False: only filename/lineno are used, don't read sources.
    for frame in traceback.StackSummary.extract(traceback.walk_tb(e.__traceback__), lookup_lines=False):
        try:
            rel = Path(frame.filename).relative_to(workspace)
        except ValueError:
            continue  # not under workspace/ (a prefix sibling like workspace_old/ must not match)
        location = f"{rel}:{frame.lineno}: "
    return f"{location}{type(e).__name__}: {e}"


def import_packages(package_dirs: list[Path], logger) -> dict[str, str]:
    """Import every module in every given workspace package; the shared core
    of skill discovery, also used for agent packages (agents/loader.py).

    Returns ``{module_name: error}`` for modules that failed to import.
    """
    ensure_import_roots()
    errors: dict[str, str] = {}
    for package_dir in package_dirs:
        if not package_dir.is_dir():
            continue
        for module_name in (package_dir.name, *_iter_module_names(package_dir, package_dir.name)):
            try:
                importlib.import_module(module_name)
            except Exception as e:  # noqa: BLE001 — a broken user module must not stop discovery
                errors[module_name] = format_load_error(e)
                logger.warning(f"Module {module_name} failed to import: {errors[module_name]}")
    return errors


def skill_id_for_class(cls: type) -> str:
    """``<namespace>/<snake_case(ClassName)>`` — identity from the class, not the file."""
    top = cls.__module__.split(".", 1)[0]
    namespace = _NAMESPACE_BY_PACKAGE.get(top, top)
    return f"{namespace}/{class_name_to_snake_case(cls.__name__)}"


def module_skill_id(module_name: str) -> str:
    """A roster id for a *module* (used for broken modules, which have no class).

    ``custom_skills.pick_socks`` -> ``local/pick_socks``;
    ``john_skills.boards.chess`` -> ``john_skills/boards.chess``.
    """
    top, _, rest = module_name.partition(".")
    namespace = _NAMESPACE_BY_PACKAGE.get(top, top)
    return f"{namespace}/{rest or top}"


def _live_class(module, qualname: str):
    """The object ``qualname`` currently denotes in ``module``, or None.

    Walks the full dotted path so a Skill nested in a class namespace resolves
    to itself, not to its enclosing class (which would silently fail the
    identity check below and prune a live skill). Function-local classes
    (``<locals>`` in the qualname) are unreachable from the module by design.
    """
    obj = module
    for part in qualname.split("."):
        if part == "<locals>":
            return None
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def live_registered_classes(registry: dict, kind: str, logger, *, include_abstract: bool = False) -> tuple[list, list]:
    """Live ``(cls, source_path)`` pairs from a ``__init_subclass__`` registry
    (``Skill._registry``, ``Agent._registry``), pruning stale entries as it
    goes: an entry whose module is no longer in ``sys.modules`` (evicted for
    reload) or no longer binds this exact class object (module re-imported,
    file edited to remove the class) is dead. ``_``-prefixed classes are
    helper bases. Abstract classes warn and are kept only with
    ``include_abstract`` (agents roster them broken; skills skip them).

    Returns ``(classes, rejected)``: ``rejected`` is ``(cls, error)`` for
    function-local classes in live modules — unreachable from their module by
    design, so unloadable; callers roster them broken rather than let them
    vanish with only a log line.
    """
    out: list[tuple[type, Path]] = []
    rejected: list[tuple[type, str]] = []
    for (module_name, qualname), cls in list(registry.items()):
        module = sys.modules.get(module_name)
        bound = _live_class(module, qualname) if module is not None else None
        if bound is not cls:
            # A live module with a function-local class is an authoring
            # mistake, not staleness — it must surface, not silently vanish.
            if module is not None and "<locals>" in qualname:
                error = (
                    f"{qualname} in {module_name} is defined inside a function and cannot be "
                    "loaded; define it at module level."
                )
                logger.warning(f"{kind} {error}")
                rejected.append((cls, error))
            del registry[(module_name, qualname)]
            continue
        if module_name.startswith("workspace."):
            # The same file imported via the compat `workspace.<pkg>` path is a
            # *second* module object; the bare-name import already owns the
            # registration, so skip the double to avoid duplicate ids.
            continue
        if cls.__name__.startswith("_"):
            continue  # helper base by convention
        if inspect.isabstract(cls):
            # Usually a misspelled abstract method — silently vanishing from
            # the roster was undiagnosable for the author.
            missing = ", ".join(sorted(getattr(cls, "__abstractmethods__", ())))
            logger.warning(
                f"{kind} {cls.__name__} in {module_name} is abstract (unimplemented: {missing}); "
                "implement the missing members, or prefix the class with '_' if it is a helper base."
            )
            if not include_abstract:
                continue
        source_file = getattr(module, "__file__", None)
        if source_file is None:
            continue
        out.append((cls, Path(source_file)))
    return out, rejected


def registered_workspace_skills(logger) -> tuple[dict[str, tuple[str, type[Skill], Path]], dict[str, str]]:
    """Live skills from the registry, plus function-local rejects as broken:
    ``({skill_id: (class_name, cls, source_path)}, {skill_id: error})``."""
    classes, rejected = live_registered_classes(Skill._registry, "Skill", logger)
    skills: dict[str, tuple[str, type[Skill], Path]] = {}
    for cls, source_file in classes:
        skill_id = skill_id_for_class(cls)
        if skill_id in skills:
            logger.warning(
                f"Skill id conflict: '{skill_id}' defined by both "
                f"{skills[skill_id][2]} and {source_file}. Using the latter."
            )
        skills[skill_id] = (cls.__name__, cls, source_file)
    return skills, {skill_id_for_class(cls): error for cls, error in rejected}
