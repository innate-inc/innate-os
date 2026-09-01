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

    # started=False, because a setup failure is a thing we KNOW never began.
    never = Episode("m", "c", "a", False, 0, 0, 0.0, "", 1.0, 0, blocked="harness: setup failed", started=False)
    assert "not attempted" in never.as_row()

    # And a job whose worker died is neither: nobody watched it end.
    unknown = Episode("m", "c", "a", False, 0, 0, 0.0, "", 1.0, 0, blocked="harness: a worker died")
    assert unknown.started is None
    assert "no result" in unknown.as_row(), unknown.as_row()
    assert "not attempted" not in unknown.as_row(), "claimed to know it never started"


def test_started_is_coerced_like_every_other_field():
    """It was added after the coercer and fell through it: a numpy bool left
    the episode unserialisable while the class promised otherwise."""

    class BadBool:
        def __bool__(self):
            raise RuntimeError("broken bool")

    assert Episode("m", "c", "a", False, 0, 0, 0.0, "", 0.0, 0, started=BadBool()).started is False
    assert Episode("m", "c", "a", False, 0, 0, 0.0, "", 0.0, 0, started=1).started is True
    assert Episode("m", "c", "a", False, 0, 0, 0.0, "", 0.0, 0).started is None, (
        "unknown must stay unknown -- it is the honest answer for a lost worker"
    )
    assert survives_transport(Episode("m", "c", "a", False, 0, 0, 0.0, "", 0.0, 0, started=lambda: 1))


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


def _renders_caught_exception(source: str) -> list[str]:
    """Lines where a caught exception is turned into text without describe().

    Every form that calls its __str__ counts, because that is what raises: an
    f-string, %-formatting, .format, format(), str/repr, or handing it to
    print. Names assigned FROM the caught exception are tainted too -- an alias
    renders exactly the same.
    """
    import ast

    found = []
    tree = ast.parse(source)
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name):
        tainted = {handler.name}
        for node in ast.walk(handler):  # aliases, before looking for renders
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                if node.value.id in tainted:
                    tainted |= {t.id for t in node.targets if isinstance(t, ast.Name)}

        def names(node, tainted=tainted):
            """Tainted names in this expression, NOT counting ones already
            inside a describe() call -- `f"{describe(exc)}"` is the fix, not
            the defect -- and not counting `type(exc).__name__`, which reads the
            class and never calls the instance's __str__."""
            found, stack = set(), [node]
            while stack:
                n = stack.pop()
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "describe":
                    continue
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "type":
                    continue  # type(exc).__name__ never calls __str__
                if isinstance(n, ast.Name) and n.id in tainted:
                    found.add(n.id)
                stack.extend(ast.iter_child_nodes(n))
            return found

        for node in ast.walk(handler):
            where = None
            if isinstance(node, ast.FormattedValue) and names(node.value):
                where = "interpolated"
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and names(node.right):
                where = "%-formatted"
            elif isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "id", "") or getattr(fn, "attr", "")
                if name == "describe":
                    continue
                if name in ("str", "repr", "format", "print") and any(names(a) for a in node.args):
                    where = f"passed to {name}()"
                elif name == "format" and isinstance(fn, ast.Attribute) and any(names(a) for a in node.args):
                    where = "str.format"
            if where:
                found.append(f"line {node.lineno}: {where}")
    return found


def test_the_guards_that_record_an_exception_all_use_describe():
    """describe() exists because an exception's __str__ can raise. A handler
    that renders the exception it caught raises from inside itself, which is
    how a sweep aborts rather than losing one episode.

    Read from the syntax tree: an earlier version matched one spelling and
    stayed green while two sites used another.
    """
    import pathlib

    here = pathlib.Path(__file__).resolve().parent
    offenders = []
    for path in sorted(here.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        if path.name == "runner.py":
            # Only describe() itself may touch the exception, not the file.
            source = source.replace(source[source.index("def describe(") : source.index("def _opt_float(")], "")
        offenders += [f"{path.name} {w}" for w in _renders_caught_exception(source)]
    assert not offenders, (
        "these guards render the exception they caught; one whose __str__ raises would "
        "raise from inside the handler -- use runner.describe():" + "".join(chr(10) + "  " + o for o in offenders)
    )


def _handler(*body: str) -> str:
    """A try/except with the given lines in the handler."""
    return chr(10).join(("try:", "    f()", "except Exception as exc:", *("    " + b for b in body)))


def test_the_scanner_recognises_the_forms_that_defeated_it():
    """The check is only worth its green if it fails on the real patterns."""
    unsafe = (
        _handler("x = f'{exc}'"),
        _handler("msg = exc", "x = f'{msg}'"),
        _handler("x = 'failed: %s' % exc"),
        _handler("x = '{}'.format(exc)"),
        _handler("x = str(exc)"),
        _handler("x = repr(exc)"),
        _handler("print(exc)"),
        _handler("x = f'a {format(exc)} b'"),
    )
    for snippet in unsafe:
        assert _renders_caught_exception(snippet), "scanner missed:" + chr(10) + snippet

    safe = (
        _handler("x = describe(exc)"),
        _handler("x = f'{describe(exc)}'"),
        _handler("raise ValueError('nope') from exc"),
        chr(10).join(("try:", "    f()", "except Exception:", "    x = 'lost it'")),
    )
    safe += (_handler("x = f'upstream {type(exc).__name__}'"),)
    for snippet in safe:
        assert not _renders_caught_exception(snippet), "scanner false-positived:" + chr(10) + snippet
