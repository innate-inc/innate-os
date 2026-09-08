# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the People block (no ROS, no cv2, no network).

Covers what the model actually reads: the identity wording per state, which
facts win a place and which never get one, the open loops that are always
there, the token budgets, and the "not drawn this turn" caveat that keeps the
text honest about the picture.
"""

from datetime import datetime
from typing import cast

import pytest

from brain_client.brain import people_context
from brain_client.people.types import PeopleSnapshotDict

NOW = (datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)).timestamp()
HOUR = 3600.0
DAY = 86400.0


def fact(text: str, *, kind: str = "preference", importance: float = 0.6, age_sec: float = HOUR) -> dict:
    return {
        "id": f"f_{abs(hash(text)) % 1000:03d}",
        "text": text,
        "kind": kind,
        "importance": importance,
        "last_confirmed": NOW - age_sec,
    }


def person(**overrides) -> dict:
    base = {
        "tag": "P3",
        "person_id": "person_7f92a1b3",
        "name": "Theo",
        "state": "known",
        "evidence": ["face"],
        "confidence": 0.91,
        "bbox": [100, 300, 930, 560],
        "head_bbox": [100, 380, 260, 480],
        "range_m": 1.8,
        "bearing_deg": -4.0,
        "tracked_sec": 41.2,
        "lost": False,
        "description": "Man, 30s, glasses.",
        "hint": None,
        "learned": None,
        "digest": None,
    }
    return {**base, **overrides}


def digest(**overrides) -> dict:
    base = {"facts": [], "open_loops": [], "episodes": [], "last_seen": None, "encounters": 0}
    return {**base, **overrides}


def snapshot(people: list[dict] | None = None, **overrides) -> PeopleSnapshotDict:
    base = {
        "schema": 1,
        "stamp": NOW,
        "frame_stamp_ns": "1788818400123456789",
        "image_size": [640, 480],
        "health": {"camera": "ok"},
        "collection_enabled": True,
        "attention": None,
        "people": people or [],
        "recent": [],
    }
    return cast("PeopleSnapshotDict", {**base, **overrides})


def render(snap: PeopleSnapshotDict, *, events=(), conversation=(), boxes_drawn: bool = True) -> str:
    text = people_context.render(snap, list(events), list(conversation), NOW, boxes_drawn)
    assert text is not None
    return text


# ---------- identity wording ----------


def test_known_person_reads_as_the_rfc_block():
    text = render(
        snapshot(
            [
                person(
                    digest=digest(
                        facts=[fact("likes pasta", importance=0.7), fact("works upstairs", importance=0.6)],
                        open_loops=[{"id": "o_01", "text": "find the blue socks", "due": "2026-09-09"}],
                        last_seen={"stamp": NOW - 300, "map": "kitchen"},
                        encounters=41,
                    )
                )
            ]
        )
    )
    lines = text.splitlines()
    assert lines[0] == "People in view:"
    assert lines[1].startswith("- P3 = Theo (known, face). Man, 30s, glasses.")
    assert lines[2] == "  Facts: likes pasta; works upstairs; find the blue socks (open, due 2026-09-09)."
    assert lines[3] == "  Last seen 5 min ago in kitchen. 41 encounters."


def test_possible_person_is_probable_and_their_facts_are_conditional():
    text = render(
        snapshot(
            [
                person(
                    tag="P4",
                    name="Ana",
                    state="possible",
                    evidence=["outfit"],
                    description=None,
                    digest=digest(facts=[fact("prefers tea")]),
                )
            ]
        )
    )
    assert "- P4 = probably Ana (by clothes, face not seen yet)." in text
    assert "  If this is Ana: prefers tea." in text
    # Body evidence never asserts anything about the person in front of you.
    assert "P4 = Ana" not in text


def test_unknown_person_shows_how_long_they_have_been_tracked():
    text = render(snapshot([person(tag="P5", name=None, person_id=None, state="unknown", evidence=[], tracked_sec=40)]))
    assert "- P5 = unknown (tracked 40 s)." in text


def test_familiar_person_has_no_name_to_use():
    text = render(snapshot([person(tag="P6", name=None, state="familiar", evidence=["face", "outfit"])]))
    assert "- P6 = someone you have met before, no name on file (by face, clothes)." in text


def test_conflict_names_both_candidates_and_degrades_to_one():
    both = render(snapshot([person(tag="P4", state="conflict", runner_up_name="Ana")]))
    assert "- P4 = unsure (Theo or Ana)." in both
    alone = render(snapshot([person(tag="P4", state="conflict")]))
    assert "- P4 = unsure (maybe Theo, the evidence disagrees)." in alone


def test_a_track_that_just_left_is_marked_rather_than_claimed_present():
    text = render(snapshot([person(tag="P3", lost=True)]))
    assert "- P3 = Theo (known, face), just left view." in text


def test_the_box_rides_the_text_so_a_tag_can_be_pointed_at():
    text = render(snapshot([person()]))
    assert "box [100, 300, 930, 560]" in text


# ---------- what the block never says ----------


def test_sensitive_facts_never_reach_the_block():
    text = render(
        snapshot(
            [
                person(
                    digest=digest(
                        facts=[
                            fact("takes medication for his heart", kind="sensitive", importance=1.0),
                            fact("likes pasta"),
                        ]
                    )
                )
            ]
        )
    )
    assert "medication" not in text
    assert "likes pasta" in text


def test_accumulated_confidence_is_never_shown_as_a_number():
    text = render(snapshot([person(confidence=0.91)]))
    assert "%" not in text and "0.91" not in text


# ---------- fact selection ----------


def test_the_conversation_decides_which_facts_surface():
    facts = [
        fact("works upstairs", importance=0.9, age_sec=HOUR),
        fact("likes pasta", importance=0.4, age_sec=30 * DAY),
        fact("has a dog", importance=0.4, age_sec=HOUR),
        fact("plays the cello", importance=0.4, age_sec=HOUR),
        fact("runs on Sundays", importance=0.4, age_sec=HOUR),
    ]
    quiet = render(snapshot([person(digest=digest(facts=facts))]))
    assert "likes pasta" not in quiet  # five facts, four places, and nothing to recommend it

    dinner = render(
        snapshot([person(digest=digest(facts=facts))]),
        conversation=[("user", "what should we make for dinner, is there pasta left?")],
    )
    assert "likes pasta" in dinner


def test_events_count_as_context_too():
    facts = [fact(f"filler fact {i}", importance=0.6) for i in range(4)] + [fact("allergic to walnuts", importance=0.5)]
    text = render(
        snapshot([person(digest=digest(facts=facts))]),
        events=["Skill inspect_shelf completed: found walnuts and cereal"],
    )
    assert "allergic to walnuts" in text


def test_at_most_four_facts_but_every_open_loop():
    loops = [{"id": f"o_{i}", "text": f"open thing {i}", "due": None} for i in range(3)]
    text = render(snapshot([person(digest=digest(facts=[fact(f"fact {i}") for i in range(9)], open_loops=loops))]))
    facts_line = next(line for line in text.splitlines() if line.startswith("  Facts:"))
    assert facts_line.count("fact ") == 4
    assert all(f"open thing {i} (open)" in facts_line for i in range(3))


def test_open_loops_survive_a_person_whose_facts_do_not_fit():
    long_facts = [fact(" ".join(f"word{i}{j}" for j in range(90))) for i in range(4)]
    text = render(
        snapshot(
            [
                person(
                    digest=digest(
                        facts=long_facts, open_loops=[{"id": "o_1", "text": "bring the socks", "due": "tomorrow"}]
                    )
                )
            ]
        )
    )
    assert "bring the socks (open, due tomorrow)" in text
    assert "word00" not in text


# ---------- budgets ----------


def test_one_person_stays_within_their_token_budget():
    text = render(
        snapshot(
            [
                person(
                    description="Man, 30s, glasses, blue jacket, standing by the counter",
                    hint="unnamed person in conversation for 40 s; if it fits, ask their name",
                    learned='P3 said "I\'m Theo" — P3 = Theo from here on',
                    digest=digest(
                        facts=[fact(f"a moderately wordy fact number {i} about him") for i in range(4)],
                        open_loops=[],
                        episodes=[{"start": NOW - DAY, "end": NOW - DAY + 200, "map": "kitchen", "summary": "Talked."}],
                        last_seen={"stamp": NOW - 3 * HOUR, "map": "kitchen"},
                        encounters=41,
                    ),
                )
            ]
        )
    )
    body = "\n".join(line for line in text.splitlines() if line != "People in view:")
    assert len(body.split()) * 1.3 <= 120


def test_a_crowd_stays_within_the_block_budget():
    crowd = [
        person(
            tag=f"P{i}",
            person_id=f"person_{i:08x}",
            name=f"Person{i}",
            digest=digest(
                facts=[fact(f"a reasonably wordy fact {j} about person {i}") for j in range(4)],
                last_seen={"stamp": NOW - 2 * HOUR, "map": "kitchen"},
                encounters=12,
            ),
        )
        for i in range(6)
    ]
    text = render(snapshot(crowd))
    assert len(text.split()) * 1.3 <= 400
    # Every person is still named: the budget trims what they carry, never who is there.
    assert all(f"P{i} = Person{i}" in text for i in range(6))


# ---------- the rest of the block ----------


def test_learned_and_hint_ride_verbatim():
    learned = 'P5 said "I\'m Zoe" — P5 = Zoe from here on'
    hint = "heard 'I'm Ana' but two people are in view; if it matters, ask which one"
    text = render(snapshot([person(tag="P5", learned=learned, hint=hint)]))
    assert f"  learned just now: {learned}." in text
    assert f"  {hint}." in text


def test_the_attention_line_says_what_the_subconscious_is_doing():
    text = render(
        snapshot(
            [person(tag="P4")],
            attention={"tag": "P4", "text": "trying to see P4's face (2.5 m away, turned aside)", "head_bbox": None},
        )
    )
    assert "Attention: trying to see P4's face (2.5 m away, turned aside)." in text


def test_seen_earlier_today_lists_the_day_not_the_week():
    text = render(
        snapshot(
            [person()],
            recent=[
                {"person_id": "person_11aa22bb", "name": "Marc", "last_seen": NOW - 3 * HOUR, "map": "hallway"},
                {"person_id": "person_33cc44dd", "name": "Yesterday's guest", "last_seen": NOW - DAY, "map": "hallway"},
                {"person_id": "person_7f92a1b3", "name": "Theo", "last_seen": NOW - 60, "map": "kitchen"},
            ],
        )
    )
    line = next(line for line in text.splitlines() if line.startswith("Seen earlier today:"))
    assert "Marc (" in line and "hallway" in line
    assert "Yesterday" not in line
    assert "Theo" not in line  # he is standing right here


def test_positions_not_drawn_is_stated_when_the_frame_could_not_be_paired():
    text = render(snapshot([person()]), boxes_drawn=False)
    assert text.splitlines()[0] == "People in view (positions not drawn this turn):"


def test_no_people_and_no_recent_means_no_block():
    assert people_context.render(snapshot([]), [], [], NOW, True) is None


def test_a_stale_snapshot_stops_claiming_anyone_is_in_view():
    stale = snapshot(
        [person()],
        stamp=NOW - 30,
        attention={"tag": "P3", "text": "trying to see P3's face", "head_bbox": None},
        recent=[{"person_id": "person_11aa22bb", "name": "Marc", "last_seen": NOW - HOUR, "map": "hallway"}],
    )
    text = render(stale)
    assert "People in view" not in text and "Attention" not in text
    assert text.startswith("Seen earlier today: Marc")


# ---------- frame pairing ----------


def test_frame_stamp_ns_parses_the_wire_string_and_rejects_junk():
    assert people_context.frame_stamp_ns(snapshot()) == 1788818400123456789
    assert people_context.frame_stamp_ns(snapshot(frame_stamp_ns=None)) is None
    assert people_context.frame_stamp_ns(snapshot(frame_stamp_ns="")) is None
    assert people_context.frame_stamp_ns(snapshot(frame_stamp_ns="1.5e9")) is None


def test_relative_times_read_like_a_person_would_say_them():
    times = [
        ({"stamp": NOW - 300, "map": None}, "Last seen 5 min ago."),
        ({"stamp": NOW - 2 * HOUR, "map": None}, "Last seen 2 hours ago."),
        ({"stamp": NOW - 25 * HOUR, "map": None}, "Last seen yesterday."),
        ({"stamp": NOW - 5 * DAY, "map": None}, "Last seen 5 days ago."),
    ]
    for last_seen, expected in times:
        text = render(snapshot([person(digest=digest(last_seen=last_seen))]))
        assert f"  {expected}" in text


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
