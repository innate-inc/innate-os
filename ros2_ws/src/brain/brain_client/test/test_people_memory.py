# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The person memory's pure records: fact ranking, what may surface,
superseding, episodes, and the person.json round trip. No ROS, no I/O."""

from __future__ import annotations

from dataclasses import replace

import pytest

from brain_client.people.memory import (
    Attribution,
    Consent,
    Description,
    Episode,
    Fact,
    FactKind,
    FactSource,
    LastSeen,
    NameCandidate,
    Names,
    OpenLoop,
    Profile,
    Relationship,
    close_episode,
    fact_score,
    latest_open,
    next_sequence_id,
    open_episode,
    profile_from_dict,
    profile_to_dict,
    rank_facts,
    recency_weight,
    supersede,
    surfaceable,
    words,
)

DAY = 86400.0
NOW = 1_788_818_400.0


def fact(
    id_: str = "f_01",
    text: str = "likes pasta",
    *,
    kind: FactKind = FactKind.PREFERENCE,
    importance: float = 0.7,
    last_confirmed: float = NOW,
    attribution: Attribution = Attribution.SELF,
    superseded_by: str | None = None,
) -> Fact:
    return Fact(
        id=id_,
        text=text,
        kind=kind,
        importance=importance,
        attribution=attribution,
        first_confirmed=last_confirmed,
        last_confirmed=last_confirmed,
        superseded_by=superseded_by,
    )


# ------------------------------------------------------------------ ranking


def test_words_drops_stopwords_and_single_letters():
    assert words("She asked for the blue socks") == {"asked", "blue", "socks"}


def test_recency_weight_halves_every_thirty_days():
    assert recency_weight(0.0) == 1.0
    assert recency_weight(30 * DAY) == pytest.approx(0.5)
    assert recency_weight(60 * DAY) == pytest.approx(0.25)


def test_recency_weight_treats_a_future_stamp_as_now():
    assert recency_weight(-DAY) == 1.0


def test_rank_facts_puts_the_more_important_fact_first():
    low = fact("f_01", "works upstairs", importance=0.3)
    high = fact("f_02", "allergic to nuts", importance=0.9)
    assert [item.id for item in rank_facts([low, high], NOW)] == ["f_02", "f_01"]


def test_rank_facts_decays_an_old_confirmation():
    fresh = fact("f_01", "drinks tea", importance=0.5, last_confirmed=NOW)
    stale = fact("f_02", "drinks coffee", importance=0.5, last_confirmed=NOW - 120 * DAY)
    assert [item.id for item in rank_facts([fresh, stale], NOW)] == ["f_01", "f_02"]


def test_rank_facts_lifts_a_fact_the_conversation_is_about():
    pasta = fact("f_01", "likes pasta", importance=0.4)
    upstairs = fact("f_02", "works upstairs", importance=0.5)
    ranked = rank_facts([pasta, upstairs], NOW, words("what should we cook, pasta again?"))
    assert [item.id for item in ranked] == ["f_01", "f_02"]


def test_fact_score_rises_with_context_overlap():
    item = fact(text="likes pasta and pesto")
    assert fact_score(item, NOW, words("pasta")) > fact_score(item, NOW, ())


def test_fact_score_caps_the_context_bonus():
    item = fact(text="likes pasta pesto basil garlic olive")
    many = fact_score(item, NOW, words("pasta pesto basil garlic olive"))
    three = fact_score(item, NOW, words("pasta pesto basil"))
    assert many == pytest.approx(three)


def test_rank_facts_is_stable_for_equal_scores():
    first = fact("f_01", "a fact")
    second = fact("f_02", "b fact")
    assert [item.id for item in rank_facts([second, first], NOW)] == ["f_01", "f_02"]


# ------------------------------------------------------------- surfaceable


def test_superseded_facts_never_surface():
    assert not surfaceable(fact(superseded_by="f_09"))


def test_sensitive_facts_never_surface():
    assert not surfaceable(fact(kind=FactKind.SENSITIVE))


def test_appearance_notes_never_surface():
    assert not surfaceable(fact(kind=FactKind.APPEARANCE))


def test_unattributed_facts_never_surface():
    assert not surfaceable(fact(attribution=Attribution.UNCERTAIN))


def test_an_ordinary_attributed_fact_surfaces():
    assert surfaceable(fact())


def test_rank_facts_drops_everything_unsurfaceable():
    keep = fact("f_01")
    ranked = rank_facts(
        [keep, fact("f_02", kind=FactKind.SENSITIVE), fact("f_03", superseded_by="f_01")],
        NOW,
    )
    assert [item.id for item in ranked] == ["f_01"]


# ------------------------------------------------------------- record edits


def test_next_sequence_id_starts_at_one_and_fills_the_first_gap():
    assert next_sequence_id("f", []) == "f_01"
    assert next_sequence_id("f", ["f_01", "f_03"]) == "f_02"


def test_supersede_points_the_old_fact_at_its_replacement():
    old = fact("f_01", "name is Ana")
    new = fact("f_02", "name is Anna", last_confirmed=NOW + 60)
    updated = supersede([old], "f_01", new)
    assert updated[0].superseded_by == "f_02"
    assert updated[1].id == "f_02"


def test_supersede_keeps_the_original_first_confirmed():
    old = replace(fact("f_01"), first_confirmed=NOW - 10 * DAY)
    new = fact("f_02", last_confirmed=NOW)
    assert supersede([old], "f_01", new)[1].first_confirmed == NOW - 10 * DAY


def test_supersede_of_an_unknown_id_just_appends():
    updated = supersede([fact("f_01")], "f_99", fact("f_02"))
    assert [item.id for item in updated] == ["f_01", "f_02"]
    assert updated[0].superseded_by is None


def test_open_episode_closes_one_left_open():
    first = Episode(id="e_01", start=NOW - 600)
    episodes = open_episode([first], Episode(id="e_02", start=NOW))
    assert episodes[0].end == NOW
    assert episodes[1].end is None


def test_close_episode_sets_the_end_and_the_summary():
    episodes = close_episode([Episode(id="e_01", start=NOW - 60)], NOW, "Asked about the socks.")
    assert episodes[0].end == NOW
    assert episodes[0].summary == "Asked about the socks."


def test_close_episode_keeps_an_existing_summary_when_none_is_given():
    episodes = close_episode([Episode(id="e_01", start=NOW, summary="kept")], NOW + 1)
    assert episodes[0].summary == "kept"


def test_close_episode_without_an_open_one_changes_nothing():
    closed = [Episode(id="e_01", start=NOW - 60, end=NOW)]
    assert close_episode(closed, NOW + 10, "late") == tuple(closed)


def test_latest_open_finds_the_last_unclosed_episode():
    episodes = [Episode(id="e_01", start=1.0, end=2.0), Episode(id="e_02", start=3.0)]
    found = latest_open(episodes)
    assert found is not None and found.id == "e_02"


# ------------------------------------------------------------ serialization


def full_profile() -> Profile:
    return Profile(
        id="person_7f92a1b3",
        names=Names(preferred="Theo", aliases=("Teo",)),
        relationship=Relationship.HOUSEHOLD,
        description=Description(text="Man, 30s, glasses.", stamp=NOW - 100, thumbnail_id="thumb_0"),
        consent=Consent(how="conversation", stamp=NOW - 200),
        created=NOW - 10 * DAY,
        last_seen=LastSeen(stamp=NOW - 300, map="home", x=3.1, y=1.4),
        encounters=41,
        facts=(
            Fact(
                id="f_01",
                text="likes pasta",
                kind=FactKind.PREFERENCE,
                confidence=0.9,
                attribution=Attribution.SELF,
                source=FactSource(utterance_id="u_1843", stamp=NOW - 400, quote="I love pasta", speaker_tag="P2"),
                first_confirmed=NOW - 400,
                last_confirmed=NOW - 400,
                importance=0.7,
            ),
        ),
        episodes=(Episode(id="e_01", start=NOW - 500, end=NOW - 300, map="home", present=("P3",), summary="Chat."),),
        open_loops=(OpenLoop(id="o_01", text="find the blue socks", created=NOW - 500, due="2026-09-09"),),
        name_candidates=(NameCandidate(name="Theodore", stamp=NOW - 100, quote="call me Theodore", tag="P3"),),
        retention_days=None,
    )


def test_profile_survives_a_json_round_trip():
    profile = full_profile()
    assert profile_from_dict(profile_to_dict(profile)) == profile


def test_profile_to_dict_is_json_serializable():
    import json

    assert json.loads(json.dumps(profile_to_dict(full_profile())))["id"] == "person_7f92a1b3"


def test_profile_from_dict_tolerates_a_nearly_empty_record():
    profile = profile_from_dict({"id": "person_1"})
    assert profile.id == "person_1"
    assert profile.names.preferred is None
    assert profile.facts == ()


def test_profile_from_dict_falls_back_on_unknown_enum_values():
    profile = profile_from_dict(
        {"id": "person_1", "relationship": "nemesis", "facts": [{"id": "f_01", "text": "x", "kind": "vibes"}]}
    )
    assert profile.relationship is Relationship.UNKNOWN
    assert profile.facts[0].kind is FactKind.BIOGRAPHY


def test_profile_from_dict_survives_wrong_types():
    profile = profile_from_dict({"id": "person_1", "facts": "not a list", "encounters": None})
    assert profile.facts == ()
    assert profile.encounters == 0


def test_named_is_true_only_with_a_preferred_name():
    assert full_profile().named
    assert not Profile(id="person_1").named
