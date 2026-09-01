"""An Episode crosses a process boundary, so every field has to survive it.

A field holding an arbitrary object does not cost the episode it is in: it
fails in the pool's result feeder, which aborts the whole sweep. Converting
selected fields at each return meant every new return path was a chance to
miss one, and three were missed -- `reason` taken from an agent's
`failed_reason`, the two optional metrics, and the agent name on the
start-refusal path.

The other half is that describing a failure must not fail: an exception whose
__str__ raises used to raise from inside the handler that had caught it.
"""

import json
import pickle
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

from runner import Episode, describe, run_episode

MAP, CID = "gallery", "gallery_ring_tour"


def survives_transport(ep):
    """What the pool's result feeder does to it, and what results/ stores."""
    pickle.dumps(ep)
    json.dumps(asdict(ep))
    return True


def oracle_for(ch):
    from oracles import plan_for
    from planner_agent import PlannerAgent

    return PlannerAgent(plan_for(ch))


# --- the coercion boundary ------------------------------------------------


def test_arbitrary_objects_never_reach_the_wire():
    """One Episode built entirely out of things that cannot be pickled."""
    weird = lambda: None  # noqa: E731
    ep = Episode(
        map=weird,
        challenge=weird,
        agent=weird,
        passed=weird,
        goals_done=weird,
        goals_total=weird,
        elapsed_s=weird,
        reason=weird,
        wall_s=weird,
        steps=weird,
        error=weird,
        blocked=weird,
        turns=weird,
        path_len_m=weird,
        goal_times_s=weird,
        utterances=weird,
        first_utterance_s=weird,
        tempt_min_m=weird,
        camera_errors=weird,
        heard=weird,
    )
    assert survives_transport(ep)
    assert isinstance(ep.agent, str) and isinstance(ep.goals_done, int)
    assert isinstance(ep.elapsed_s, float) and ep.goal_times_s == ()


def test_a_field_that_cannot_even_be_printed_is_still_a_string():
    class Unprintable:
        def __str__(self):
            raise RuntimeError("cannot render")

    ep = Episode("m", "c", Unprintable(), False, 0, 0, 0.0, "", 0.0, 0)
    assert ep.agent == "<unprintable>"
    assert survives_transport(ep)


def test_ordinary_values_are_left_alone():
    ep = Episode(
        "gallery",
        "x",
        "oracle",
        True,
        2,
        4,
        12.5,
        "done",
        1.5,
        99,
        goal_times_s=[1.0, 2.0],
        first_utterance_s=3.5,
        tempt_min_m=None,
    )
    assert (ep.map, ep.agent, ep.passed, ep.goals_done) == ("gallery", "oracle", True, 2)
    assert ep.goal_times_s == (1.0, 2.0) and ep.first_utterance_s == 3.5
    assert ep.tempt_min_m is None, "None is meaningful -- it never approached"


def test_an_agents_failed_reason_cannot_abort_the_sweep():
    """`reason` comes straight from agent.failed_reason when a plan runs out."""

    class Weird:
        name = "weird"
        done = True
        failed_reason = lambda: "not a string"  # noqa: E731

        def reset(self, *a, **k):
            pass

        def act(self, *a, **k):
            pass

    ep = run_episode(MAP, CID, lambda ch: Weird(), agent_name="oracle")
    assert isinstance(ep.reason, str)
    assert survives_transport(ep)


def test_a_start_refusal_also_sanitises(monkeypatch):
    """That path returns early and bypassed the finalisation conversions."""
    from mars_sim_driver.challenges import ChallengeEngine

    class Weird:
        done = True

        @property
        def name(self):
            return lambda: "not a string"

        def reset(self, *a, **k):
            pass

        def act(self, *a, **k):
            pass

    monkeypatch.setattr(ChallengeEngine, "start", lambda self, cid: False)
    ep = run_episode(MAP, CID, lambda ch: Weird(), agent_name="oracle")
    assert ep.blocked, "a refused start should be blocked"
    assert survives_transport(ep)


def test_unconvertible_metrics_do_not_abort_the_sweep(monkeypatch):
    from mars_sim_driver.challenges import ChallengeEngine

    monkeypatch.setattr(
        ChallengeEngine,
        "metrics",
        lambda self: {
            "path_len_m": lambda: 1,
            "goal_times_s": [lambda: 2],
            "utterances": lambda: 3,
            "first_utterance_s": lambda: 4,
            "tempt_min_m": lambda: 5,
        },
    )
    ep = run_episode(MAP, CID, oracle_for, agent_name="oracle")
    assert survives_transport(ep)


# --- describing a failure -------------------------------------------------


def test_describe_survives_an_exception_that_cannot_render():
    class Broken(Exception):
        def __str__(self):
            raise RuntimeError("broken exception text")

    assert describe(Broken()) == "Broken: <unprintable>"


def test_describe_keeps_the_ordinary_message():
    assert describe(ValueError("no plan")) == "ValueError: no plan"
    assert describe(ValueError()) == "ValueError"


def test_a_broken_exception_in_finalisation_does_not_lose_the_episode():
    """It used to raise from inside the guard, reach the last-resort handler,
    and be scored as a robot failure with 0/0 goals."""

    class Broken(Exception):
        def __str__(self):
            raise RuntimeError("broken exception text")

    class BadProp:
        name = "bad"
        done = True

        def reset(self, *a, **k):
            pass

        def act(self, *a, **k):
            pass

        @property
        def blocked_reason(self):
            raise Broken()

    ep = run_episode(MAP, CID, lambda ch: BadProp(), agent_name="oracle")
    assert "<unprintable>" in ep.error, ep.error
    assert ep.goals_total > 0, f"the episode was discarded: {ep}"


# --- presentation ---------------------------------------------------------


def test_a_run_that_happened_is_not_printed_as_not_attempted():
    ran = Episode(
        "m", "c", "a", False, 2, 4, 9.0, "", 1.0, 500, blocked="harness: could not read engine.state", started=True
    )
    assert "not scored" in ran.as_row() and "not attempted" not in ran.as_row()
    assert "2/4" in ran.as_row(), "the measurements it produced were hidden"

    never = Episode("m", "c", "a", False, 0, 0, 0.0, "", 1.0, 0, blocked="harness: setup failed")
    assert "not attempted" in never.as_row()


def test_a_value_whose_truthiness_raises_is_still_a_bool():
    """bool() was the one conversion left outside the guard, in a coercer
    whose docstring says it never raises."""

    class BadBool:
        def __bool__(self):
            raise RuntimeError("broken bool")

    ep = Episode("m", "c", "a", BadBool(), 0, 0, 0.0, "", 0.0, 0)
    assert ep.passed is False
    assert survives_transport(ep)


def test_goal_times_cannot_be_mutated_back_onto_the_wire():
    """A list stays mutable, so the guarantee held only until someone
    appended to it."""
    ep = Episode("m", "c", "a", False, 0, 0, 0.0, "", 0.0, 0, goal_times_s=[1.0, 2.0])
    assert isinstance(ep.goal_times_s, tuple), "a mutable list is one append from unpicklable"
    with pytest.raises(AttributeError):
        ep.goal_times_s.append(lambda: None)
    assert survives_transport(ep)
    assert json.loads(json.dumps(asdict(ep)))["goal_times_s"] == [1.0, 2.0], "JSON still writes an array"


def test_a_live_episode_that_ran_is_not_printed_as_not_attempted():
    """The live runner never sets `steps`, so deciding on that alone printed a
    real attempt -- one that drove and scored goals -- as never made. Deciding
    on any measurement was still a heuristic: a live episode can reach
    `running` and be blocked before one of them moves."""
    live = Episode(
        "live",
        "c",
        "brain",
        False,
        2,
        4,
        9.0,
        "",
        1.0,
        0,
        blocked="harness: brief not delivered",
        path_len_m=3.0,
        started=True,
    )
    assert "not scored" in live.as_row() and "not attempted" not in live.as_row()
    assert "2/4" in live.as_row()


def test_a_started_episode_with_nothing_measured_yet_still_counts_as_run():
    """The case the measurement heuristic could not see: it began, and was
    blocked before anything moved."""
    e = Episode(
        "live", "c", "brain", False, 0, 2, 0.0, "", 1.0, 0, blocked="harness: brief not delivered", started=True
    )
    assert "not scored" in e.as_row() and "not attempted" not in e.as_row()


def test_a_refusal_before_the_run_is_still_not_attempted():
    """A start refusal fills in goals_total, so that alone cannot decide it."""
    e = Episode("m", "c", "a", False, 0, 4, 0.0, "", 1.0, 0, blocked="harness: engine.start refused", started=False)
    assert "not attempted" in e.as_row()


def test_the_guards_that_record_an_exception_all_use_describe():
    """describe() exists because an exception's __str__ can raise. A handler
    that formats the caught exception itself raises from inside the handler,
    which is how a sweep aborts instead of losing one episode.

    Read from the syntax tree, not by matching one spelling: the previous
    version of this test rejected `type(exc).__name__` and stayed green while
    two sites interpolated the exception directly.
    """
    import ast
    import pathlib

    here = pathlib.Path(__file__).resolve().parent
    offenders = []
    for path in sorted(here.glob("*.py")):
        if path.name.startswith("test_") or path.name == "runner.py":
            continue  # runner.py defines describe()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name):
            caught = handler.name

            def renders(node, caught=caught):
                """Does this expression turn the caught exception into text
                without going through describe()?"""
                if isinstance(node, ast.Call):
                    if getattr(node.func, "id", "") == "describe":
                        return False
                    if getattr(node.func, "id", "") in ("str", "repr"):
                        return any(isinstance(a, ast.Name) and a.id == caught for a in node.args)
                return False

            for node in ast.walk(handler):
                if isinstance(node, ast.FormattedValue):
                    inner = node.value
                    if isinstance(inner, ast.Name) and inner.id == caught:
                        offenders.append(f"{path.name}:{node.lineno} interpolates `{caught}`")
                    elif any(renders(n) for n in ast.walk(inner)):
                        offenders.append(f"{path.name}:{node.lineno} renders `{caught}`")
                elif renders(node):
                    offenders.append(f"{path.name}:{node.lineno} renders `{caught}`")
    assert not offenders, (
        "these guards render the exception they caught; one whose __str__ raises would "
        "raise from inside the handler -- use runner.describe():" + "".join(chr(10) + "  " + o for o in offenders)
    )
