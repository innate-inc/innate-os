# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""A skill's client resource must be closed, not left to the collector.

`brain_client/skills/types.py` tears down a resource only when its factory is a
GENERATOR: a plain `return` declares no teardown and the framework drops the
value. Both vision skills returned their client that way, so every skill run
leaked one httpx connection pool -- and `close()`, added to the client for
exactly this, had no call site at all.

Checked against the source rather than by importing: these modules pull in
rclpy, which is not available off the robot, and the property being pinned is
structural -- the factory yields and closes, or it does not.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = REPO_ROOT / "workspace/innate_skills"
FACTORIES = [(SKILLS / "pick_any_object.py", "_proxy"), (SKILLS / "drop_in_box.py", "_proxy")]


def _func(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            if any(getattr(d, "id", getattr(d, "attr", "")) == "resource" for d in node.decorator_list):
                return node
    raise AssertionError(f"no @resource {name} in {path.name}")


@pytest.mark.parametrize("path,name", FACTORIES, ids=lambda v: getattr(v, "name", v))
def test_the_factory_yields_so_it_gets_a_teardown(path, name):
    fn = _func(path, name)
    assert any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn)), (
        f"{path.name}:{name} returns its client instead of yielding it, "
        "so the framework runs no teardown and the connection pool leaks"
    )


@pytest.mark.parametrize("path,name", FACTORIES, ids=lambda v: getattr(v, "name", v))
def test_the_teardown_closes_the_client(path, name):
    fn = _func(path, name)
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert tries, f"{path.name}:{name} has no finally, so a raising body skips the close"
    closes = [
        n
        for t in tries
        for n in ast.walk(ast.Module(body=t.finalbody, type_ignores=[]))
        if isinstance(n, ast.Call) and (getattr(n.func, "attr", "") == "close" or getattr(n.func, "id", "") == "close")
    ]
    assert closes, f"{path.name}:{name} never calls close() in its teardown"


def test_the_client_actually_has_a_close_to_call():
    """The teardown above is only real if the object it closes has close()."""
    src = (REPO_ROOT / "ros2_ws/src/brain/brain_client/innate/gemini.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    direct = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "_DirectClient")
    methods = {n.name for n in direct.body if isinstance(n, ast.FunctionDef)}
    assert "close" in methods, "_DirectClient has no close(); the skill teardown closes nothing"


def test_the_client_is_reused_rather_than_made_per_call():
    """One pool per client, not one per vision call."""
    src = (REPO_ROOT / "ros2_ws/src/brain/brain_client/innate/gemini.py").read_text(encoding="utf-8")
    assert src.count("httpx.Client(") == 1, "more than one place constructs a client"
    tree = ast.parse(src)
    direct = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "_DirectClient")
    stream = next(n for n in direct.body if isinstance(n, ast.FunctionDef) and n.name == "request_stream")
    # Constructing it here is fine -- lazily, once -- as long as the result is
    # kept on the instance. A bare httpx.Client(...) that is used and dropped
    # is the leak: one connection pool per vision call.
    for node in ast.walk(stream):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Client":
            parent = next((n for n in ast.walk(stream) if isinstance(n, ast.Assign) and n.value is node), None)
            assert parent is not None, "a client is built and not stored"
            target = parent.targets[0]
            assert isinstance(target, ast.Attribute) and target.attr == "_client", (
                "the client is not cached on the instance, so each call makes its own pool"
            )
