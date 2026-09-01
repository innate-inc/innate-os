"""VALID must not imply manipulation the oracle never performed.

`put` and `put_near` move a prop without driving the arm. A plan that reaches
its goal that way proves the goal logic and the route -- which is what these
plans are for -- but not that the robot can pick the thing up, so the verdict
has to carry the difference.

The flag has to come from the plan that RUNS. Deriving it from the
hand-written ORACLES table alone found three challenges and missed eighteen,
because autoplan emits `put`/`put_near` for carry goals and most of these
challenges have no hand plan at all.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

import pytest
from bench_common import gate_verdict
from mars_sim_driver.challenges import load_challenges
from oracles import ORACLES, plan_for, teleport_assisted
from runner import sources

ORACLE_PASS = {"passed": True, "goals_done": 2, "goals_total": 2}
RANDOM_FAIL = [{"passed": False}]


def test_teleport_assisted_valid_says_so():
    verdict, why = gate_verdict("nav", ORACLE_PASS, RANDOM_FAIL, True)
    assert verdict == "VALID"
    assert "teleport" in why and "arm not" in why, why


def test_plain_valid_is_unqualified():
    assert gate_verdict("nav", ORACLE_PASS, RANDOM_FAIL, False) == ("VALID", "")


def test_the_caveat_does_not_hide_the_guessing_floor():
    _, why = gate_verdict("nav", ORACLE_PASS, [{"passed": True}, {"passed": False}, {"passed": False}], True)
    assert "random passed 1/3" in why and "teleport" in why, why


def test_teleport_flag_never_rescues_a_failing_oracle():
    bad = {"passed": False, "goals_done": 0, "goals_total": 2, "reason": "time limit"}
    assert gate_verdict("nav", bad, RANDOM_FAIL, True)[0] == "INVALID"


@pytest.fixture(scope="module")
def challenges():
    out = {}
    for _name, (_assets, root) in sources().items():
        out.update(load_challenges([root]))
    return out


def test_an_auto_planned_carry_is_flagged(challenges):
    """The case the ORACLES-only version got wrong: no hand plan, but the
    derived plan teleports."""
    ch = challenges["pantry_stocktake"]
    assert ch.id not in ORACLES, "this challenge grew a hand plan; pick another"
    assert any(s[0] in ("put", "put_near") for s in plan_for(ch))
    assert teleport_assisted(ch)


def test_a_pure_navigation_challenge_is_not_flagged(challenges):
    ch = challenges["gallery_ring_tour"]
    assert not any(s[0] in ("put", "put_near") for s in plan_for(ch))
    assert not teleport_assisted(ch)


def test_the_flag_matches_the_plan_for_every_challenge(challenges):
    """Whole-suite agreement, so a new challenge cannot quietly opt out."""
    flagged = set()
    for cid, ch in challenges.items():
        try:
            steps = plan_for(ch)
        except Exception:
            assert not teleport_assisted(ch), f"{cid}: flagged with no plan"
            continue
        # autoplan returns None for a challenge it cannot plan (SkillDone,
        # unmodelled goals); no plan is not a teleporting plan.
        expect = any(s and s[0] in ("put", "put_near") for s in (steps or ()))
        assert teleport_assisted(ch) == expect, cid
        if expect:
            flagged.add(cid)
    # Reading only the hand-written table found 3 of these.
    assert len(flagged) > len([c for c in flagged if c in ORACLES]), (
        "no auto-planned challenge teleports -- this test would pass vacuously"
    )
