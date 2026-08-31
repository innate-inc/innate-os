"""Pure helpers shared by main.py (runs the sweep) and report.py (merges the
per-map result files): the category names, the validity-gate verdict and the
per-category scorecard.

Both callers read the same episode shape -- the dict `dataclasses.asdict(
runner.Episode)` produces, which is also what the results files store -- so a
rule changed here changes in both places. They used to carry private copies
and had drifted: report.py had no NEEDS-ARM tier at all.
"""

from __future__ import annotations

from dataclasses import dataclass

CATEGORY_NAMES = {
    1: "easy observation and conversation",
    2: "simple instruction following",
    3: "long-horizon instruction following",
    0: "uncategorised",
}
# Print order: the three categories the brief names, then whatever fell outside.
CATEGORY_ORDER = (1, 2, 3, 0)

VERDICTS = ("VALID", "NEEDS-ARM", "INVALID", "INCOMPLETE")


def median(xs: list[float]) -> float | None:
    """Middle value (mean of the middle two for an even count); None if empty."""
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def gate_verdict(req: str, oracle: dict | None, rnd: list[dict]) -> tuple[str, str]:
    """(verdict, why) for one challenge under the validity gate.

    A challenge counts (VALID) only if the oracle PASSES -- it is solvable, so
    a failure is the agent's -- and every random rollout FAILS -- it is not
    solvable by flailing, so a pass means something. `req` is autoplan's
    classification of the challenge (Episode.needs): "arm" or "unknown" means
    no scripted oracle could witness solvability, so the challenge is held to
    the weaker half of the rule only and reported NEEDS-ARM rather than folded
    into VALID.
    """
    trivial = any(e["passed"] for e in rnd)
    if req in ("arm", "unknown"):
        if trivial:
            return "INVALID", "random passed"
        return "NEEDS-ARM", f"no auto-plan ({req}); solvability unproven"
    if not rnd or oracle is None:
        return "INCOMPLETE", "not all agents ran"
    if trivial:
        return "INVALID", "random passed -- measures nothing"
    if not oracle["passed"]:
        why = oracle.get("error") or oracle.get("reason") or f"oracle {oracle['goals_done']}/{oracle['goals_total']}"
        return "INVALID", why
    return "VALID", ""


@dataclass(frozen=True)
class ScoreRow:
    """One scorecard line: an agent's tally over one category, with its time,
    turns and path each as a ratio to the oracle on the same challenges."""

    category: int
    passed: int
    episodes: int
    goals_done: int
    goals_total: int
    time_x: float | None
    turns_x: float | None
    path_x: float | None


def scorecard(
    rows: list[dict], categories: dict[str, int], valid: set[str], agent: str
) -> tuple[list[ScoreRow], tuple[int, int, int, int]] | None:
    """Per-category rows for `agent` over the VALID challenges, plus the
    (passed, episodes, goals done, goals total) total; None if the agent has
    no episode on a VALID challenge.

    Ratios, not absolutes: seconds and metres are properties of the map, and
    the ratio to the oracle on the SAME challenge is a property of the agent.
    Only challenges where BOTH the agent and the oracle produced a number are
    compared -- a ratio against a missing denominator is not a number -- and
    a ratio is None when there were none.
    """
    ref = {e["challenge"]: e for e in rows if e["agent"] == "oracle" and e["passed"]}
    mine = [e for e in rows if e["agent"] == agent and e["challenge"] in valid]
    if not mine:
        return None

    def ratio(eps: list[dict], key: str) -> float | None:
        return median(
            [
                e[key] / ref[e["challenge"]][key]
                for e in eps
                if e["challenge"] in ref and ref[e["challenge"]].get(key, 0) and e.get(key, 0)
            ]
        )

    out = []
    for cat in CATEGORY_ORDER:
        eps = [e for e in mine if categories.get(e["challenge"], 0) == cat]
        if not eps:
            continue
        out.append(
            ScoreRow(
                category=cat,
                passed=sum(1 for e in eps if e["passed"]),
                episodes=len(eps),
                goals_done=sum(e["goals_done"] for e in eps),
                goals_total=sum(e["goals_total"] for e in eps),
                time_x=ratio(eps, "elapsed_s"),
                turns_x=ratio(eps, "turns"),
                path_x=ratio(eps, "path_len_m"),
            )
        )
    total = (
        sum(1 for e in mine if e["passed"]),
        len(mine),
        sum(e["goals_done"] for e in mine),
        sum(e["goals_total"] for e in mine),
    )
    return out, total


def format_scorecard(rows: list[ScoreRow], total: tuple[int, int, int, int]) -> list[str]:
    """The scorecard as printable lines: a header, one line per category and
    a total line. Callers put their own title above it."""

    def x(m: float | None) -> str:
        return f"{m:.2f}x" if m is not None else "-"

    lines = [f"  {'category':<38} {'pass':>7}  {'goals':>9}  {'time':>6} {'turns':>6} {'path':>6}"]
    for r in rows:
        lines.append(
            f"  {CATEGORY_NAMES[r.category]:<38} {r.passed:>3}/{r.episodes:<3} "
            f"{r.goals_done:>4}/{r.goals_total:<4} {x(r.time_x):>6} {x(r.turns_x):>6} {x(r.path_x):>6}"
        )
    passed, n, done, avail = total
    lines.append(f"  {'-' * 38} {passed:>3}/{n:<3} {done:>4}/{avail:<4}")
    return lines
