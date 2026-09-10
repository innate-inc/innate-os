"""Loading sidecar modules: one loop for challenges, props and rooms.

A sidecar is a Python file exporting one object under a fixed name
(``CHALLENGE = Challenge(...)``, ``PROP = Prop(...)``, ``ROOM = Room(...)``),
found under any of a list of roots. Later roots override earlier ones by key,
so an asset bundle can ship its own pack next to the tracked defaults. A file
whose import fails, or that lacks the export, is skipped with a warning: one
bad sidecar loses its own object and nothing else, and must never take out
the world server.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def load_sidecars(
    roots: list[Path], module_prefix: str, export: str, tag: str, key: Callable[[T], str], set_root: bool = False
) -> dict[str, T]:
    """{key(obj): obj} for every ``<root>/*.py`` not starting with an
    underscore, in root order then filename order (filenames sort the roster
    the user sees). `module_prefix` names the imported module; `export` is the
    attribute read off it; `tag` prefixes the skip warning; with `set_root`
    the sidecar's directory is stored on ``obj.root`` so its relative asset
    paths can be resolved."""
    found: dict[str, T] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"{module_prefix}_{path.stem}", path)
                assert spec and spec.loader
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                obj: T = getattr(module, export)
                if set_root:
                    obj.root = path.parent
                found[key(obj)] = obj
            except Exception as exc:  # noqa: BLE001 -- a bad sidecar loses its object, nothing else
                print(f"[{tag}] skipping {path.name}: {exc!r}", flush=True)
    return found
