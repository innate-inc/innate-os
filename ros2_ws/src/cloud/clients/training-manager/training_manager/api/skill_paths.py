"""Shared helpers for locating skill directories."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Request


def skills_dir(request: Request) -> Path:
    return Path(request.app.state.skills_dir)


def is_skill_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and not path.name.startswith(".")
        and path.name != "__pycache__"
        and (path / "metadata.json").is_file()
    )


def iter_skill_dirs(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return

    for child in sorted(root.iterdir()):
        if is_skill_dir(child):
            yield child
