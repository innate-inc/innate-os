"""_TaskStack.apply merges, never replaces -- every way that has been got wrong.

A single reply that re-lists its goals incompletely (which a flash-tier model
over a 30+ turn episode will do) must not delete what it forgot to re-type;
"done" must remove exactly what it names; ids are coerced rather than dropped;
constraints are append-only, de-duplicated, capped, and refreshed on re-mention.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

from backends_v2 import CONSTRAINT_CAP, GOAL_CAP, _TaskStack


def _ids(stack: _TaskStack) -> list:
    return [g["id"] for g in stack.goals]


# 1. Partial goal re-list does NOT drop the goal that was omitted.


def _after_partial_relist() -> _TaskStack:
    s = _TaskStack()
    s.apply({"goals": [{"id": "gate1", "side": "R"}, {"id": "gate2", "side": "L"}]})
    s.apply({"goals": [{"id": "gate2", "side": "L", "note": "cleared"}]})  # forgot to re-mention gate1
    return s


def test_partial_relist_preserves_the_omitted_goal() -> None:
    assert any(g["id"] == "gate1" for g in _after_partial_relist().goals)


def test_partial_relist_still_updates_the_mentioned_goal() -> None:
    s = _after_partial_relist()
    assert next(g for g in s.goals if g["id"] == "gate2").get("note") == "cleared"


def test_partial_relist_duplicates_nothing() -> None:
    assert len(_after_partial_relist().goals) == 2


# 2. Explicit "done" DOES remove a goal, even with no "goals" key at all.


def test_explicit_done_removes_exactly_that_goal_without_a_goals_key() -> None:
    s = _after_partial_relist()
    s.apply({"done": ["gate1"]})
    assert _ids(s) == ["gate2"]


# 3. done accepted as a bare id, not only a list.


def test_bare_string_done_also_removes_the_goal() -> None:
    s = _after_partial_relist()
    s.apply({"done": ["gate1"]})
    s.apply({"done": "gate2"})
    assert s.goals == []


# 4. Numeric ids are coerced, not silently dropped -- the regression an
#    earlier version of this fix introduced (strict isinstance(id, str)
#    meant a model emitting numeric ids lost every goal forever, which is
#    WORSE than the destructive-replace bug being fixed).


def test_numeric_id_is_coerced_and_kept() -> None:
    s = _TaskStack()
    s.apply({"goals": [{"id": 1, "side": "R"}]})
    assert _ids(s) == [1]


def test_numeric_id_also_matches_on_the_done_side() -> None:
    s = _TaskStack()
    s.apply({"goals": [{"id": 1, "side": "R"}]})
    s.apply({"done": [1]})  # done side also needs to match a numeric id via str()
    assert s.goals == []


# 5. Goals with no usable id are dropped AND counted, not silently vanished.


def _after_malformed_entries() -> tuple[_TaskStack, int]:
    s = _TaskStack()
    before = s.dropped
    s.apply({"goals": [{"id": "ok"}, {"no_id": True}, "not a dict", 5, {"id": ["bad"]}]})
    return s, before


def test_malformed_goal_entries_are_dropped_without_crashing() -> None:
    s, _ = _after_malformed_entries()
    assert _ids(s) == ["ok"]


def test_dropped_counter_increments_for_each_unusable_entry() -> None:
    s, before = _after_malformed_entries()
    assert s.dropped - before == 4


# 6. Non-dict update to apply() itself is a safe no-op.


def test_non_dict_update_is_a_safe_noop() -> None:
    s, _ = _after_malformed_entries()
    s.apply("not a dict at all")
    assert _ids(s) == ["ok"]


# 7. facts still merge (unchanged behavior), never dropped by an unrelated update.


def test_facts_survive_an_unrelated_goals_only_update() -> None:
    s = _TaskStack()
    s.apply({"facts": {"delivered": "true"}})
    s.apply({"goals": [{"id": "x"}]})  # unrelated update, should not touch facts
    assert s.facts.get("delivered") == "true"


# 8. constraints are additive, de-duplicated, capped -- AND re-mention
#    refreshes position instead of aging out under first-occurrence order.
#    This is the exact bug class being fixed, reintroduced in miniature by
#    a naive first pass at this same fix -- caught before shipping.


def _after_reasserting_every_round() -> _TaskStack:
    s = _TaskStack()
    s.apply({"constraints": ["important: do not claim X"]})
    for i in range(CONSTRAINT_CAP):
        s.apply({"constraints": [f"c{i}"]})
        s.apply({"constraints": ["important: do not claim X"]})  # re-asserted every round
    return s


def test_a_constraint_reasserted_every_round_survives_past_its_cap_worth_of_neighbours() -> None:
    assert "important: do not claim X" in _after_reasserting_every_round().constraints


def test_constraints_still_capped() -> None:
    assert len(_after_reasserting_every_round().constraints) <= CONSTRAINT_CAP


def test_constraints_deduplicated() -> None:
    s = _after_reasserting_every_round()
    assert len(s.constraints) == len(set(s.constraints))


# 9. A constraint that stops being re-mentioned DOES eventually age out
#    (append-only does not mean unbounded -- the cap still does its job).


def test_an_unrepeated_constraint_ages_out_past_the_cap() -> None:
    s = _TaskStack()
    s.apply({"constraints": ["stale one"]})
    for i in range(CONSTRAINT_CAP):
        s.apply({"constraints": [f"c{i}"]})
    assert "stale one" not in s.constraints


# 10. note_released writes a fact mechanically, independent of apply(), and
#     records position and time when given them.


def test_note_released_writes_a_fact_with_position_and_time() -> None:
    s = _TaskStack()
    s.note_released("test_item", (1.234, -0.5, 0.0), elapsed_s=42.4)
    assert s.facts.get("released:test_item") == "true,at(1.23,-0.5),t=42s"


def test_note_released_degrades_gracefully_with_no_pose_or_time() -> None:
    s = _TaskStack()
    s.note_released("test_item", None)
    assert s.facts.get("released:test_item") == "true"


# 11. Goal count backstop: pathological growth (e.g. runaway id drift)
#     cannot grow the goal list without bound.


def _after_runaway_ids() -> _TaskStack:
    s = _TaskStack()
    for i in range(GOAL_CAP + 25):
        s.apply({"goals": [{"id": f"g{i}"}]})
    return s


def test_goal_list_capped_despite_unbounded_distinct_ids() -> None:
    assert len(_after_runaway_ids().goals) == GOAL_CAP


def test_the_most_recent_goal_survives_the_cap() -> None:
    assert _after_runaway_ids().goals[-1]["id"] == f"g{GOAL_CAP + 24}"


# 12. The cap evicts by RECENCY OF UPDATE, not by original insertion order --
# this is the exact scenario an adversarial review demonstrated failing
# against a naive `by_id[gid] = g` (Python keeps an updated key's ORIGINAL
# dict position, so a plain overwrite does not move it to the end): one
# goal inserted first and then legitimately updated on every subsequent
# turn must survive, while a pile of newer-but-never-touched stale ids
# (simulating id-drift duplicates) are the ones that should get evicted.


def _after_updating_the_oldest_every_round() -> _TaskStack:
    s = _TaskStack()
    s.apply({"goals": [{"id": "alive", "note": "v0"}]})
    for i in range(GOAL_CAP + 10):
        s.apply({"goals": [{"id": f"stale{i}"}, {"id": "alive", "note": f"v{i + 1}"}]})
    return s


def test_a_goal_updated_every_round_survives_the_cap_despite_being_the_oldest_insertion() -> None:
    assert any(g["id"] == "alive" for g in _after_updating_the_oldest_every_round().goals)


def test_its_latest_update_value_is_preserved() -> None:
    s = _after_updating_the_oldest_every_round()
    assert next((g for g in s.goals if g["id"] == "alive"), {}).get("note") == f"v{GOAL_CAP + 10}"


# 13. bool and empty-string ids are rejected as malformed (not silently
# accepted and collapsed together -- bool is a subtype of int in Python,
# and an empty string looks valid but is not a real id).


def _after_bool_and_empty_ids() -> tuple[_TaskStack, int]:
    s = _TaskStack()
    before = s.dropped
    s.apply({"goals": [{"id": True}, {"id": False}, {"id": ""}, {"id": "real"}]})
    return s, before


def test_bool_and_empty_string_ids_are_dropped_as_malformed() -> None:
    s, _ = _after_bool_and_empty_ids()
    assert _ids(s) == ["real"]


def test_dropped_count_reflects_all_three_rejected_entries() -> None:
    s, before = _after_bool_and_empty_ids()
    assert s.dropped - before == 3
