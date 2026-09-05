# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Centralized paths for agent, skill, and input scripts.

Layout:
    $INNATE_OS_ROOT/workspace/innate_agents/   # shipped agents (tracked)
    $INNATE_OS_ROOT/workspace/custom_agents/   # user agents   (gitignored)
    $INNATE_OS_ROOT/workspace/innate_skills/   # shipped skills (tracked)
    $INNATE_OS_ROOT/workspace/custom_skills/   # user skills   (gitignored)
    $INNATE_OS_ROOT/workspace/<any other dir>/ # a skill package, dropped in whole (see below)
    $INNATE_OS_ROOT/workspace/inputs/          # input devices

Provenance is determined by which directory a script came from:
"shipped" if the path is under innate_*/, "user" otherwise.

Skill packages: every other directory under workspace/ (not agents/inputs/lib
machinery, not hidden or ``_``-prefixed) is scanned as a skill package — a
folder of skills and their helpers that installs by being dropped in whole.
Skills in a package get ids namespaced by the package directory name
(``john_skills/chess``); ``innate_skills`` and ``custom_skills`` keep their
historical ``innate-os/`` and ``local/`` prefixes so nothing persisted breaks.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

Source = Literal["shipped", "user"]


def get_innate_os_root() -> Path:
    return Path(os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")))


def _workspace() -> Path:
    return get_innate_os_root() / "workspace"


def get_workspace_dir() -> Path:
    """workspace/ itself — the root new skill packages are dropped into."""
    return _workspace()


def get_innate_agents_dir() -> Path:
    return _workspace() / "innate_agents"


def get_custom_agents_dir() -> Path:
    return _workspace() / "custom_agents"


def get_innate_skills_dir() -> Path:
    return _workspace() / "innate_skills"


def get_custom_skills_dir() -> Path:
    return _workspace() / "custom_skills"


# workspace/ directories that are never skill packages: agent/input/lib
# machinery and per-skill storage. skill_lib/ and the pre-workspace agents//
# skills/ names stay listed so a stale checkout directory is never scanned.
NON_PACKAGE_DIR_NAMES = frozenset(
    {
        "innate_agents",
        "custom_agents",
        "inputs",
        "skill_lib",
        "skill_storage",
        "debug_runs",
        "agents",
        "skills",
        # generated TrainedSkill refs (see skills/physical_refs.py) — importable
        # like any workspace package, but never scanned for skills
        "physical_skills",
    }
)


def get_workspace_package_dirs() -> list[Path]:
    """Skill-package directories under workspace/ beyond the two standard ones.

    Any directory not claimed by other machinery is a package: a folder of
    skills and helpers that installs by being dropped in whole. Hidden and
    ``_``-prefixed names are skipped. Sorted for a deterministic scan order.
    """
    workspace = _workspace()
    if not workspace.is_dir():
        return []
    packages = []
    for child in sorted(workspace.iterdir()):
        name = child.name
        if not child.is_dir() or name.startswith((".", "_")):
            continue
        if name in NON_PACKAGE_DIR_NAMES or name in ("innate_skills", "custom_skills"):
            continue
        packages.append(child)
    return packages


def skill_id_prefix_for(path: str | os.PathLike) -> str:
    """The skill-id namespace for a script at ``path``.

    ``innate_skills`` and ``custom_skills`` keep their historical prefixes —
    every persisted id, the webapp, and cloud registration already speak them.
    Any other workspace package namespaces by its directory name, which is what
    makes a dropped-in pack collision-proof. Anything outside workspace/
    stays ``local``.
    """
    resolved = Path(path).resolve()
    for root, prefix in ((get_innate_skills_dir(), "innate-os"), (get_custom_skills_dir(), "local")):
        try:
            resolved.relative_to(root.resolve())
            return prefix
        except ValueError:
            continue
    try:
        rel = resolved.relative_to(_workspace().resolve())
    except ValueError:
        return "local"
    if rel.parts and rel.parts[0] not in NON_PACKAGE_DIR_NAMES:
        return rel.parts[0]
    return "local"


def get_innate_inputs_dir() -> Path:
    return _workspace() / "inputs"


def _dedupe(dirs: list[Path]) -> list[Path]:
    """Ordered, de-duplicated scan list (loaders tolerate missing dirs)."""
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        if str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def get_agent_directories() -> list[Path]:
    """Agent scan dirs under workspace/."""
    return _dedupe([get_innate_agents_dir(), get_custom_agents_dir()])


def get_skill_directories() -> list[Path]:
    """Skill scan dirs: the two standard packages plus any dropped-in package."""
    return _dedupe([get_innate_skills_dir(), get_custom_skills_dir(), *get_workspace_package_dirs()])


def get_input_directories() -> list[Path]:
    """Input-device scan dirs under workspace/."""
    return _dedupe([get_innate_inputs_dir()])


def classify_source(path: str | os.PathLike) -> Source:
    """Return "shipped" if path lives under an innate_* dir, else "user"."""
    resolved = Path(path).resolve()
    innate_roots = [get_innate_agents_dir().resolve(), get_innate_skills_dir().resolve()]
    for root in innate_roots:
        try:
            resolved.relative_to(root)
            return "shipped"
        except ValueError:
            continue
    return "user"


def ensure_user_directories() -> None:
    """Create custom_* directories if they don't exist yet."""
    get_custom_agents_dir().mkdir(parents=True, exist_ok=True)
    get_custom_skills_dir().mkdir(parents=True, exist_ok=True)
