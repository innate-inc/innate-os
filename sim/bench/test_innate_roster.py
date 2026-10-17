"""The probe's robot has the tools innate's sim gives the robot on main.

Its menu is read from main's sources (innate_roster.py); these pin the
reading, so a change on main shows up here rather than as a probe that
quietly certifies a different robot.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import ast

import innate_roster as R
from brain_agent import IMPLEMENTED, TOOLS


def _intro_skills() -> list[str]:
    tree = ast.parse(R.DEFAULT_AGENT.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "get_skills")
    ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
    return [R._snake(e.id) for e in ret.value.elts]


def test_the_default_agent_is_the_one_the_brain_picks():
    init = (R.REPO / "ros2_ws/src/brain/brain_client/brain_client/agents/initializer.py").read_text()
    assert 'agents["intro_agent"]' in init
    assert 'return "intro_agent"' in R.DEFAULT_AGENT.read_text()


def test_the_roster_is_intro_agents_skills_plus_the_brains_built_ins():
    expected = [s for s in _intro_skills() if s not in R.WITHHELD] + [R.GO_TO_POINT_IN_VIEW, R.WAIT]
    assert [t.name for t in TOOLS] == expected


def test_every_offered_skill_runs_here():
    offered = {t.name for t in TOOLS} - {R.GO_TO_POINT_IN_VIEW, R.WAIT}
    assert offered <= IMPLEMENTED, f"offered with no in-process implementation: {sorted(offered - IMPLEMENTED)}"


def test_a_withheld_skill_says_why():
    for name, why in R.WITHHELD.items():
        assert name in _intro_skills(), f"{name} is withheld but not in the roster"
        assert why


def test_descriptions_are_mains():
    by = {t.name: t for t in TOOLS}
    assert by["pick_any_object"].description.startswith("Pick up an object lying on the floor")
    assert by["navigate_to_position"].description.startswith("Move the robot to a position given x, y")
    assert "'very_happy'" in by["head_emotion"].description
    assert by["wave"].description.startswith("Greet someone with a friendly arm wave")
    assert by[R.WAIT].description.startswith("Do nothing until the next update")


def test_the_stop_tool_text_is_the_brains():
    tree = ast.parse((R.BRAIN / "tools.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_tools")
    choice = next(
        n for n in ast.walk(fn) if isinstance(n, ast.IfExp) and isinstance(getattr(n.body, "value", None), str)
    )
    for user_spoke, branch in ((True, choice.body), (False, choice.orelse)):
        ours = R.stop_description("x", user_spoke).removeprefix("Abort the currently running skill (x). ")
        assert ours == ast.literal_eval(branch)


def test_the_system_prompt_is_mains():
    prompt = R.system_prompt()
    assert prompt.startswith("You are the brain of a MARS, the Innate home robot.")
    assert "Anything you write as plain text is spoken aloud" in prompt
    assert R.DEFAULT_DIRECTIVE in prompt


def _offered(text: str) -> list[str]:
    return [ln for ln in text.split("Your tools this turn:")[1].splitlines() if ln.startswith("- ")]


def test_a_running_skill_offers_only_stop():
    assert _offered(R.menu(TOOLS, running="navigate_to_position", user_spoke=True)) == ["- stop_current_skill()"]
    assert _offered(R.menu(TOOLS, running="navigate_to_position", user_spoke=False)) == [
        "- stop_current_skill()",
        "- wait()",
    ]


def test_arguments_are_checked_like_the_skills_signature():
    nav = next(t for t in TOOLS if t.name == "navigate_to_position")
    assert R.coerce_args(nav, {"x": "1", "y": 0, "local_frame": "true"}) == {
        "x": 1.0,
        "y": 0.0,
        "theta_degrees": 0.0,
        "local_frame": True,
    }
    try:
        R.coerce_args(nav, {"x": 1})
    except ValueError as exc:
        assert "'y'" in str(exc)
    else:
        raise AssertionError("a missing required argument was accepted")
