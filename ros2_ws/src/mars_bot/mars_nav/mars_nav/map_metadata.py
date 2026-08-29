#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

"""Compatibility fixes for saved Nav2 map metadata."""

import os
import re
from pathlib import Path

TRINARY_FREE_THRESHOLD = 0.196
_LEGACY_TRINARY_FREE_THRESHOLD = 0.25
_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_MODE_RE = re.compile(r"^(\s*mode\s*:\s*)([^\s#]+)(.*)$", re.MULTILINE)
_FREE_THRESH_RE = re.compile(rf"^(\s*free_thresh\s*:\s*)({_NUMBER})(.*)$", re.MULTILINE)


def normalize_legacy_trinary_metadata(text: str) -> tuple[str, bool]:
    """Repair the unsafe threshold emitted by Nav2 Humble's trinary map saver.

    Humble writes unknown occupancy cells as gray 205 but pairs them with its
    default ``free_thresh: 0.25``. On reload, 205 becomes free because its
    occupancy is about 0.196. Restrict the migration to that exact legacy
    signature so imported maps and non-trinary modes retain their semantics.
    """
    mode_match = _MODE_RE.search(text)
    threshold_match = _FREE_THRESH_RE.search(text)
    if mode_match is None or threshold_match is None:
        return text, False
    if mode_match.group(2).lower() != "trinary":
        return text, False
    if abs(float(threshold_match.group(2)) - _LEGACY_TRINARY_FREE_THRESHOLD) > 1e-9:
        return text, False

    replacement = (
        f"{threshold_match.group(1)}{TRINARY_FREE_THRESHOLD:.3f}{threshold_match.group(3)}"
    )
    start, end = threshold_match.span()
    return text[:start] + replacement + text[end:], True


def repair_legacy_trinary_map(yaml_path: str | Path) -> bool:
    """Atomically repair one legacy map YAML; return whether it changed."""
    path = Path(yaml_path)
    original = path.read_text()
    repaired, changed = normalize_legacy_trinary_metadata(original)
    if not changed:
        return False

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(repaired)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
