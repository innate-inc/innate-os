# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import ast
import contextlib
import os
from pathlib import Path

CORE = Path(__file__).resolve().parents[3] / "ros2_ws/src/mars_bot/mars_sim_driver/mars_sim_driver/core.py"


def _load_cache_policy():
    """Load the stdlib-only cache policy without importing MuJoCo in launcher CI."""
    tree = ast.parse(CORE.read_text(encoding="utf-8"), filename=str(CORE))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MODEL_CACHE_LIMIT" for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_prune_model_cache":
            selected.append(node)
    namespace = {"Path": Path, "contextlib": contextlib}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(CORE), "exec"), namespace)
    return namespace["MODEL_CACHE_LIMIT"], namespace["_prune_model_cache"]


def test_compiled_world_cache_keeps_current_plus_recent_environments(tmp_path):
    limit, prune = _load_cache_policy()
    assert limit == 3
    caches = []
    for index in range(5):
        path = tmp_path / f"world-{index}.mjb"
        path.write_bytes(b"mjb")
        stamp = 1_000_000_000 + index
        os.utime(path, ns=(stamp, stamp))
        caches.append(path)

    current = caches[0]  # protected even though it is older than every peer
    prune(tmp_path, current)

    assert {path.name for path in tmp_path.glob("world-*.mjb")} == {
        current.name,
        caches[3].name,
        caches[4].name,
    }
