"""One place that names an agent architecture.

`--agents brain:<spec>` takes either a built-in key or an import path,
`module:Class`. The import path is the point: a new architecture is a new
module and a command-line argument, never an edit to this harness. That is
what "swappable without forking" has to mean to be worth claiming.

Built-ins are strings, not imported classes, so naming one costs nothing at
startup and an optional backend whose dependencies are absent cannot break
the harness for everyone else.
"""

from __future__ import annotations

import importlib
from typing import Any

BUILTIN: dict[str, str] = {
    "echo": "backends:EchoBackend",  # offline test double
    "codex": "backends:CodexBackend",  # sees, by being handed a file path
    "codex-blind": "backends:CodexBlindBackend",  # the control: same model, no camera
    "gemini": "backends:GeminiBackend",  # sees, inline; needs GEMINI_API_KEY
    "nemotron_stack": "backends_v2:NemotronStackBackend",  # AGENT_SPEC.md's agent
}


def names() -> list[str]:
    return sorted(BUILTIN)


def resolve(spec: str) -> Any:
    """Return the backend class for a built-in name or a `module:Class` path."""
    target = BUILTIN.get(spec, spec)
    if ":" not in target:
        raise ValueError(
            f"unknown backend {spec!r}. Built-ins: {', '.join(names())}. "
            f"Any other architecture: --agents brain:your_module:YourBackend"
        )
    mod_name, _, cls_name = target.rpartition(":")
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ValueError(f"backend {spec!r}: cannot import {mod_name!r} ({exc})") from exc
    try:
        return getattr(mod, cls_name)
    except AttributeError as exc:
        raise ValueError(f"backend {spec!r}: {mod_name!r} has no {cls_name!r}") from exc
