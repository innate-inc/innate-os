"""Tests for the capability gate, one per way I got it wrong.

Every case here is a mistake that was live at some point this session. The gate
decides which challenges get scored, so a wrong answer either hides a real
agent failure or invents twenty-two fake ones -- and each of these was invisible
until something downstream looked wrong.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

from pathlib import Path

from capabilities import missing_capabilities, needs_manipulation, needs_move, runtime_env
from mars_sim_driver.challenges import (
    Challenge,
    Drop,
    Goal,
    Hold,
    InCircle,
    InRect,
    Near,
    SkillDone,
)


def challenge(*goals, setup=()) -> Challenge:
    return Challenge(id="t", title="t", category=1, brief="t", setup=list(setup), goals=list(goals))


def test_placement_away_from_the_drop_is_a_carry() -> None:
    # The cup is dropped at the counter and must end up at a seat.
    assert (
        needs_manipulation(
            challenge(
                Goal("deliver", InCircle("cup", 0.0, 0.2, 0.3)),
                setup=[Drop("cup", -0.62, 1.32)],
            )
        )
        is True
    )


def test_placement_the_setup_already_satisfies_is_not_a_carry() -> None:
    # STAY-PUT. counter_out_of_reach's third goal: the teapot is dropped at
    # exactly the position the goal names, so the goal says "do not move it".
    # Read as a carry, it blocks a challenge whose correct outcome is that
    # nothing moved -- and whose whole point is declining to try.
    assert (
        needs_manipulation(
            challenge(
                Goal("leave it", InCircle("teapot", -2.10, -0.30, 0.30)),
                setup=[Drop("teapot", -2.10, -0.30)],
            )
        )
        is False
    )


def test_hold_around_a_robot_position_is_not_a_grasp() -> None:
    # Hold is a DURATION wrapper, not a grasp. Hold(inner=InRect(robot)) is
    # "stand in this doorway for a second". Reading the name as a grasp
    # blocked five category-1 challenges.
    assert (
        needs_manipulation(
            challenge(
                Goal("stand there", Hold(InRect("robot", 4.0, -0.7, 5.9, 0.7), 1.0)),
            )
        )
        is False
    )


def test_inrect_on_the_robot_is_navigation_not_manipulation() -> None:
    # The one the per-prop diff could not catch: comparing old and new rules
    # over (challenge, prop) pairs only ever passes prop=<a dropped object>,
    # and "robot" is never a Drop. So a regression that made robot-position
    # goals look like carries slipped through a clean 101-pair diff, and only
    # showed up as rounds_all_doors -- four InRect(robot) goals, no props at
    # all -- being reported blocked.
    assert (
        needs_manipulation(
            challenge(
                Goal("red room", InRect("robot", -5.8, -3.6, -3.2, -1.0)),
                Goal("blue room", InRect("robot", -2.8, -3.6, -0.2, -1.0)),
            )
        )
        is False
    )


def test_incircle_on_the_robot_is_navigation_not_manipulation() -> None:
    assert (
        needs_manipulation(
            challenge(
                Goal("go there", InCircle("robot", -1.65, 0.1, 0.85)),
            )
        )
        is False
    )


def test_near_robot_to_prop_is_an_approach() -> None:
    assert needs_manipulation(challenge(Goal("approach", Near("robot", "mug", 0.45)))) is False


def test_near_prop_to_prop_is_a_delivery() -> None:
    assert (
        needs_manipulation(
            challenge(
                Goal("deliver", Near("mug", "plate", 0.3)),
                setup=[Drop("mug", 2.0, 2.0), Drop("plate", -2.0, -2.0)],
            )
        )
        is True
    )


def test_near_prop_to_prop_already_satisfied_is_not_a_delivery() -> None:
    assert (
        needs_manipulation(
            challenge(
                Goal("keep together", Near("mug", "plate", 0.5)),
                setup=[Drop("mug", 1.0, 1.0), Drop("plate", 1.1, 1.0)],
            )
        )
        is False
    )


# Per-prop, which is what the reach check asks: only the object that must move.
MOVED = challenge(
    Goal("deliver", InCircle("cup", 0.0, 0.2, 0.3)),
    setup=[Drop("cup", -0.62, 1.32), Drop("decoy", 0.66, 1.32)],
)


def test_per_prop_the_carried_object() -> None:
    assert needs_move(MOVED, "cup") is True


def test_per_prop_an_untouched_decoy() -> None:
    assert needs_move(MOVED, "decoy") is False


# Manipulation named rather than implied. A challenge whose ONLY goal is
# SkillDone("pick_any_object") reads, by geometry alone, as needing no pick,
# so the gate would let it run and score the missing capability as an agent
# failure -- the exact case this module exists to prevent, for the one
# skill it names by constant.
NAMED = challenge(Goal("it says it picked up", SkillDone("pick_any_object")))


def test_skilldone_naming_the_blocked_skill_implies_manipulation() -> None:
    assert needs_manipulation(NAMED) is True


def test_per_prop_question_is_unaffected_by_skilldone() -> None:
    # The reach check's path.
    assert needs_move(NAMED, "sock") is False


def test_namespaced_skill_ids_match_too() -> None:
    assert needs_manipulation(challenge(Goal("x", SkillDone("innate-os/pick_any_object")))) is True


def test_an_unrelated_skilldone_does_not_imply_manipulation() -> None:
    assert needs_manipulation(challenge(Goal("x", SkillDone("move_straight")))) is False


# The gate must describe the ROBOT's environment. The stack reads the repo
# .env; the harness runs on the host, where those variables are normally
# absent. Reading os.environ alone blocked all 19 runnable challenges on a
# deployment that could grasp perfectly well.


def test_runtime_env_sees_every_grasp_credential_env_declares() -> None:
    resolved = runtime_env()
    env_path = Path(__file__).resolve().parents[2] / ".env"
    declared = (
        [
            line.split("=", 1)[0]
            for line in env_path.read_text().splitlines()
            if line.split("=", 1)[0] in ("GEMINI_BASE_URL", "INNATE_SERVICE_KEY") and line.split("=", 1)[1].strip()
        ]
        if env_path.exists()
        else []
    )
    assert sorted(k for k in ("GEMINI_BASE_URL", "INNATE_SERVICE_KEY") if resolved.get(k)) == sorted(declared)


def test_an_explicit_empty_env_still_reports_the_capability_missing() -> None:
    assert missing_capabilities({}) == {"pick_any_object"}


def test_a_configured_backend_reports_nothing_missing() -> None:
    assert missing_capabilities({"GEMINI_BASE_URL": "http://x"}) == set()
