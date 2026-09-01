"""VALID must not imply manipulation the oracle never performed.

Three scripted plans reach their goal with `put`, which teleports the carried
prop. That proves the goal logic and the route, which is what those plans are
for; it does not prove the arm can do it. The verdict has to carry that
difference, and the flag has to keep tracking the plans rather than a list
someone has to remember to update.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_common import gate_verdict
from oracles import ORACLES, TELEPORT_ASSISTED

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


def test_flag_is_derived_from_the_plans():
    expected = {cid for cid, steps in ORACLES.items() if any(s and s[0] == "put" for s in steps)}
    assert TELEPORT_ASSISTED == expected
    assert expected, "no plan uses put -- this test would pass vacuously"
