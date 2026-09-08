# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The scribe: window buffering, the request it builds, every rule
:func:`apply` enforces on what comes back (attribution, passive naming,
ambiguity, sensitive facts, superseding), the offline queue, and deep recall.

The transport is a fake; no ROS, no network."""

from __future__ import annotations

import base64
import json

import cv2
import numpy as np
import pytest

from brain_client.people import description
from brain_client.people.memory import Attribution, FactKind
from brain_client.people.scribe import (
    WINDOW_IDLE_SEC,
    WINDOW_MAX_MESSAGES,
    Change,
    ChangeKind,
    Introduction,
    Scribe,
    ScribeFact,
    ScribeLoop,
    ScribeName,
    ScribeNote,
    ScribeOutput,
    Speaker,
    TagView,
    Utterance,
    Window,
    WindowBuffer,
    WindowQueue,
    apply,
    build_request,
    is_memory_question,
    parse_output,
    parse_recall,
    recall_request,
    window_from_dict,
    window_text,
    window_to_dict,
)
from brain_client.people.store import PeopleStore
from brain_client.people.types import FaceTemplate, IdentityState

NOW = 1_788_818_400.0
EMPTY: dict = {
    "facts": [],
    "name_candidates": [],
    "open_loops": [],
    "episode_note": "",
    "appearance_notes": [],
    "sensitive": [],
}


@pytest.fixture
def store(tmp_path) -> PeopleStore:
    return PeopleStore(tmp_path / "people")


def enrol(store: PeopleStore, now: float = NOW) -> str:
    template = FaceTemplate(
        embedding=np.ones(4, dtype=np.float32), model="sface", stamp=now, pose_bucket="frontal", quality=0.9
    )
    return store.create_unnamed([template], b"jpeg", now)


def view(
    tag: str,
    person_id: str | None = None,
    *,
    state: IdentityState = IdentityState.KNOWN,
    name: str | None = None,
    enrolling: bool = False,
) -> TagView:
    return TagView(tag=tag, state=state, person_id=person_id, name=name, enrolling=enrolling)


def said(
    text: str,
    *,
    id_: str = "u_1",
    stamp: float = NOW,
    speaker: Speaker = Speaker.USER,
    in_view: tuple[TagView, ...] = (),
) -> Utterance:
    return Utterance(id=id_, stamp=stamp, speaker=speaker, text=text, in_view=in_view)


def response(payload: dict) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]}


def only(changes: list[Change], kind: ChangeKind) -> list[Change]:
    return [change for change in changes if change.kind is kind]


# --------------------------------------------------------------- buffering


def test_a_window_closes_at_twelve_messages():
    buffer = WindowBuffer()
    for index in range(WINDOW_MAX_MESSAGES - 1):
        assert buffer.add(said("hello", id_=f"u_{index}")) is None
    window = buffer.add(said("last", id_="u_last"))
    assert window is not None and len(window.messages) == WINDOW_MAX_MESSAGES
    assert buffer.pending() == 0


def test_a_window_closes_eight_seconds_after_the_last_message():
    buffer = WindowBuffer()
    buffer.add(said("hello", stamp=NOW))
    assert buffer.due(NOW + WINDOW_IDLE_SEC - 0.1) is None
    window = buffer.due(NOW + WINDOW_IDLE_SEC)
    assert window is not None and window.closed == NOW


def test_an_empty_buffer_closes_nothing():
    assert WindowBuffer().due(NOW + 1000) is None


def test_a_window_reports_the_latest_view_of_every_tag_seen():
    window = Window(
        (
            said("a", id_="u_1", in_view=(view("P2", "person_1", state=IdentityState.UNKNOWN),)),
            said("b", id_="u_2", in_view=(view("P2", "person_1"), view("P3", "person_2"))),
        )
    )
    assert set(window.views()) == {"P2", "P3"}
    assert window.views()["P2"].state is IdentityState.KNOWN
    assert window.person_ids() == ["person_1", "person_2"]


# ----------------------------------------------------------- the request


def test_the_request_carries_the_system_text_the_window_and_the_schema():
    window = Window((said("Hi, I'm Ana", in_view=(view("P2", "person_1"),)),))
    body = build_request(window)
    assert "scribe" in body["systemInstruction"]["parts"][0]["text"]
    assert "never infer emotion" in body["systemInstruction"]["parts"][0]["text"].lower()
    assert "Hi, I'm Ana" in body["contents"][0]["parts"][0]["text"]
    schema = body["generationConfig"]["responseSchema"]
    assert set(schema["required"]) == {
        "facts",
        "name_candidates",
        "open_loops",
        "episode_note",
        "appearance_notes",
        "sensitive",
    }
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_the_window_text_names_the_people_their_state_and_the_facts_on_file(store: PeopleStore):
    person_id = enrol(store)
    store.add_fact(person_id, "likes pasta", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    profile = store.profile(person_id)
    assert profile is not None
    window = Window((said("what's for dinner?", in_view=(view("P2", person_id, name="Ana"),)),))
    text = window_text(window, {"P2": list(profile.facts)})
    assert "P2 = Ana (known)" in text
    assert "on file [f_01] preference: likes pasta" in text
    assert '[u_1] user (in view: P2): "what\'s for dinner?"' in text


def test_the_window_text_says_when_nobody_was_in_view():
    assert "nobody was in view" in window_text(Window((said("hello"),)), {})


# ---------------------------------------------------------- parsing output


def test_the_full_documented_payload_parses():
    output = parse_output(
        response(
            {
                "facts": [
                    {
                        "who": "P2",
                        "kind": "preference",
                        "text": "likes pasta",
                        "confidence": 0.9,
                        "attribution": "self",
                        "quote": "I love pasta",
                        "utterance": "u_1843",
                        "supersedes": "",
                    }
                ],
                "name_candidates": [
                    {
                        "who": "P2",
                        "name": "Ana",
                        "confidence": 0.95,
                        "quote": "Hi, I'm Ana",
                        "utterance": "u_1842",
                        "introduction": "self",
                        "referent": "",
                    }
                ],
                "open_loops": [{"who": "P2", "text": "find the blue socks", "due": "2026-09-09"}],
                "episode_note": "Ana came into the kitchen.",
                "appearance_notes": [{"who": "P2", "text": "red jacket, glasses today"}],
                "sensitive": [],
            }
        )
    )
    assert output is not None
    assert output.facts[0].kind is FactKind.PREFERENCE
    assert output.facts[0].attribution is Attribution.SELF
    assert output.name_candidates[0].introduction is Introduction.SELF
    assert output.open_loops[0].due == "2026-09-09"
    assert output.appearance_notes[0].text == "red jacket, glasses today"
    assert output.episode_note == "Ana came into the kitchen."


def test_an_unreadable_answer_parses_to_none():
    assert parse_output({}) is None
    assert parse_output({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}) is None


def test_thinking_parts_are_skipped():
    payload = {
        "candidates": [{"content": {"parts": [{"text": "musing", "thought": True}, {"text": json.dumps(EMPTY)}]}}]
    }
    assert parse_output(payload) == ScribeOutput()


def test_unknown_enum_values_fall_back_rather_than_failing():
    output = parse_output(
        response(EMPTY | {"facts": [{"who": "P2", "text": "x", "kind": "vibes", "attribution": "telepathy"}]})
    )
    assert output is not None
    assert output.facts[0].kind is FactKind.BIOGRAPHY
    assert output.facts[0].attribution is Attribution.UNCERTAIN


def test_sensitive_items_are_forced_to_the_sensitive_kind():
    output = parse_output(
        response(EMPTY | {"sensitive": [{"who": "P2", "text": "sees a cardiologist", "kind": "biography"}]})
    )
    assert output is not None and output.sensitive[0].kind is FactKind.SENSITIVE


# ------------------------------------------------------------- fact rules


def test_a_fact_is_written_when_the_person_was_in_view_while_it_was_said(store: PeopleStore):
    person_id = enrol(store)
    seen = view("P2", person_id)
    window = Window((said("I love pasta", in_view=(seen,)),))
    output = ScribeOutput(
        facts=(
            ScribeFact(
                who="P2",
                text="likes pasta",
                kind=FactKind.PREFERENCE,
                attribution=Attribution.SELF,
                quote="I love pasta",
                utterance="u_1",
            ),
        )
    )
    changes = apply(output, window, store, NOW)
    assert [change.kind for change in changes] == [ChangeKind.FACT]
    profile = store.profile(person_id)
    assert profile is not None
    assert profile.facts[0].attribution is Attribution.SELF
    assert profile.facts[0].source.quote == "I love pasta"
    digest = store.digest(person_id, NOW)
    assert digest is not None and [item["text"] for item in digest["facts"]] == ["likes pasta"]


def test_a_fact_quoted_from_a_line_the_person_was_not_in_view_for_is_only_uncertain(store: PeopleStore):
    person_id = enrol(store)
    window = Window(
        (
            said("Ana loves pasta", id_="u_1", in_view=(view("P3", "person_other"),)),
            said("hello", id_="u_2", stamp=NOW + 1, in_view=(view("P2", person_id),)),
        )
    )
    output = ScribeOutput(
        facts=(
            ScribeFact(
                who="P2",
                text="likes pasta",
                kind=FactKind.PREFERENCE,
                attribution=Attribution.THIRD_PARTY,
                quote="Ana loves pasta",
                utterance="u_1",
            ),
        )
    )
    changes = apply(output, window, store, NOW)
    assert changes[0].kind is ChangeKind.FACT
    assert changes[0].reason == "not in view during the quoted line"
    profile = store.profile(person_id)
    assert profile is not None and profile.facts[0].attribution is Attribution.UNCERTAIN
    digest = store.digest(person_id, NOW)
    assert digest is not None and digest["facts"] == []


def test_a_fact_about_a_tag_with_no_tracked_person_is_refused(store: PeopleStore):
    window = Window((said("I love pasta", in_view=(view("P2", None),)),))
    output = ScribeOutput(facts=(ScribeFact(who="P9", text="likes pasta", quote="I love pasta", utterance="u_1"),))
    changes = apply(output, window, store, NOW)
    assert changes[0].kind is ChangeKind.REJECTED
    assert changes[0].reason == "no tracked person for that tag"


def test_a_fact_whose_quote_is_nowhere_in_the_window_falls_back_to_uncertain(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("hello there", in_view=(view("P2", person_id),)),))
    output = ScribeOutput(
        facts=(
            ScribeFact(
                who="P2",
                text="likes pasta",
                attribution=Attribution.SELF,
                quote="I never said this",
                utterance="u_missing",
            ),
        )
    )
    apply(output, window, store, NOW)
    profile = store.profile(person_id)
    assert profile is not None and profile.facts[0].attribution is Attribution.UNCERTAIN


def test_the_scribe_supersedes_the_fact_it_names(store: PeopleStore):
    person_id = enrol(store)
    old = store.add_fact(person_id, "drinks tea", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    assert old is not None
    window = Window((said("actually I drink coffee now", in_view=(view("P2", person_id),)),))
    output = ScribeOutput(
        facts=(
            ScribeFact(
                who="P2",
                text="drinks coffee",
                kind=FactKind.PREFERENCE,
                attribution=Attribution.SELF,
                quote="actually I drink coffee now",
                utterance="u_1",
                supersedes=old,
            ),
        )
    )
    changes = apply(output, window, store, NOW + 5)
    assert changes[0].kind is ChangeKind.FACT_SUPERSEDED
    profile = store.profile(person_id)
    assert profile is not None and profile.facts[0].superseded_by == changes[0].record_id
    digest = store.digest(person_id, NOW + 5)
    assert digest is not None and [item["text"] for item in digest["facts"]] == ["drinks coffee"]


def test_superseding_an_id_that_is_not_on_file_writes_a_plain_fact(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("I drink coffee", in_view=(view("P2", person_id),)),))
    output = ScribeOutput(
        facts=(
            ScribeFact(
                who="P2",
                text="drinks coffee",
                attribution=Attribution.SELF,
                quote="I drink coffee",
                utterance="u_1",
                supersedes="f_99",
            ),
        )
    )
    assert apply(output, window, store, NOW)[0].kind is ChangeKind.FACT


def test_fact_importance_follows_the_kind(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("my sister lives here", in_view=(view("P2", person_id),)),))
    output = ScribeOutput(
        facts=(
            ScribeFact(
                who="P2",
                text="has a sister here",
                kind=FactKind.RELATIONSHIP,
                confidence=1.0,
                attribution=Attribution.SELF,
                quote="my sister lives here",
                utterance="u_1",
            ),
        )
    )
    apply(output, window, store, NOW)
    profile = store.profile(person_id)
    assert profile is not None and profile.facts[0].importance == pytest.approx(0.8)


# -------------------------------------------------------- sensitive facts


def test_a_sensitive_fact_is_refused_unless_the_robot_was_asked_to_remember(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("I see a cardiologist", in_view=(view("P2", person_id),)),))
    output = ScribeOutput(
        sensitive=(
            ScribeFact(
                who="P2",
                text="sees a cardiologist",
                kind=FactKind.SENSITIVE,
                attribution=Attribution.SELF,
                quote="I see a cardiologist",
                utterance="u_1",
            ),
        )
    )
    changes = apply(output, window, store, NOW)
    assert changes[0].kind is ChangeKind.REJECTED
    assert changes[0].reason == "sensitive and nobody asked the robot to remember it"
    profile = store.profile(person_id)
    assert profile is not None and profile.facts == ()


def test_a_sensitive_fact_asked_for_is_stored_but_never_surfaced(store: PeopleStore):
    person_id = enrol(store)
    seen = view("P2", person_id)
    window = Window(
        (
            said("remember that I see a cardiologist on Thursdays", in_view=(seen,)),
            said("I will", id_="u_2", stamp=NOW + 1, speaker=Speaker.ROBOT, in_view=(seen,)),
        )
    )
    output = ScribeOutput(
        sensitive=(
            ScribeFact(
                who="P2",
                text="sees a cardiologist on Thursdays",
                kind=FactKind.SENSITIVE,
                attribution=Attribution.SELF,
                quote="remember that I see a cardiologist on Thursdays",
                utterance="u_1",
            ),
        )
    )
    changes = apply(output, window, store, NOW)
    assert changes[0].kind is ChangeKind.FACT
    profile = store.profile(person_id)
    assert profile is not None and profile.facts[0].kind is FactKind.SENSITIVE
    digest = store.digest(person_id, NOW)
    assert digest is not None and digest["facts"] == []


def test_a_remember_request_from_someone_else_does_not_unlock_a_sensitive_fact(store: PeopleStore):
    person_id = enrol(store)
    window = Window(
        (
            said("remember this", id_="u_1", in_view=(view("P3", "person_other"),)),
            said("I see a cardiologist", id_="u_2", stamp=NOW + 1, in_view=(view("P2", person_id),)),
        )
    )
    output = ScribeOutput(
        sensitive=(
            ScribeFact(
                who="P2",
                text="sees a cardiologist",
                kind=FactKind.SENSITIVE,
                attribution=Attribution.SELF,
                quote="I see a cardiologist",
                utterance="u_2",
            ),
        )
    )
    assert apply(output, window, store, NOW)[0].kind is ChangeKind.REJECTED


# ------------------------------------------------------------ name rules


def name_output(**overrides) -> ScribeOutput:
    base = {
        "who": "P2",
        "name": "Ana",
        "confidence": 0.95,
        "quote": "Hi, I'm Ana",
        "utterance": "u_1",
        "introduction": Introduction.SELF,
    }
    return ScribeOutput(name_candidates=(ScribeName(**(base | overrides)),))


def test_a_first_person_introduction_with_one_person_in_view_commits_the_name(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("Hi, I'm Ana", in_view=(view("P2", person_id),)),))
    changes = apply(name_output(), window, store, NOW)
    assert changes[0].kind is ChangeKind.NAME
    assert changes[0].text == 'P2 said "Hi, I\'m Ana" — P2 = Ana from here on'
    assert store.name_of(person_id) == "Ana"
    profile = store.profile(person_id)
    assert profile is not None and profile.consent.how == "conversation"


def test_a_name_commits_onto_a_track_that_is_still_enrolling(store: PeopleStore):
    person_id = enrol(store)
    seen = view("P2", person_id, state=IdentityState.UNKNOWN, enrolling=True)
    window = Window((said("Hi, I'm Ana", in_view=(seen,)),))
    assert apply(name_output(), window, store, NOW)[0].kind is ChangeKind.NAME
    assert store.name_of(person_id) == "Ana"


def test_two_people_in_view_hold_the_name_as_a_candidate_with_the_documented_hint(store: PeopleStore):
    first, second = enrol(store, NOW), enrol(store, NOW + 1)
    window = Window((said("I'm Ana", in_view=(view("P2", first), view("P3", second))),))
    changes = apply(name_output(quote="I'm Ana"), window, store, NOW)
    assert changes[0].kind is ChangeKind.NAME_CANDIDATE
    assert changes[0].text == "heard 'I'm Ana' but two people are in view; if it matters, ask which one"
    assert store.name_of(first) is None and store.name_of(second) is None
    for person_id in (first, second):
        profile = store.profile(person_id)
        assert profile is not None and profile.name_candidates[0].name == "Ana"


def test_a_transcript_that_says_which_person_resolves_the_ambiguity(store: PeopleStore):
    first, second = enrol(store, NOW), enrol(store, NOW + 1)
    window = Window((said("this is my daughter Zoe", in_view=(view("P2", first), view("P3", second))),))
    changes = apply(
        name_output(name="Zoe", quote="this is my daughter Zoe", introduction=Introduction.THIRD_PARTY, referent="P3"),
        window,
        store,
        NOW,
    )
    assert changes[0].kind is ChangeKind.NAME
    assert store.name_of(second) == "Zoe"
    assert store.name_of(first) is None


def test_a_third_party_introduction_without_a_referent_stays_a_candidate(store: PeopleStore):
    first, second = enrol(store, NOW), enrol(store, NOW + 1)
    window = Window((said("this is my daughter", in_view=(view("P2", first), view("P3", second))),))
    changes = apply(
        name_output(name="Zoe", quote="this is my daughter", introduction=Introduction.THIRD_PARTY),
        window,
        store,
        NOW,
    )
    assert changes[0].kind is ChangeKind.NAME_CANDIDATE


def test_a_name_never_commits_onto_an_unsettled_track(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("Hi, I'm Ana", in_view=(view("P2", person_id, state=IdentityState.UNKNOWN),)),))
    changes = apply(name_output(), window, store, NOW)
    assert changes[0].kind is ChangeKind.NAME_CANDIDATE
    assert changes[0].reason == "the track is neither confirmed nor enrolling"
    assert store.name_of(person_id) is None


def test_a_name_never_commits_over_a_name_already_on_file(store: PeopleStore):
    person_id = enrol(store)
    store.rename(person_id, "Bob", "app", now=NOW)
    window = Window((said("Hi, I'm Ana", in_view=(view("P2", person_id, name="Bob"),)),))
    changes = apply(name_output(), window, store, NOW)
    assert changes[0].kind is ChangeKind.NAME_CANDIDATE
    assert changes[0].text == "heard 'Hi, I'm Ana' but P2 is already Bob; ask if that is a correction"
    assert store.name_of(person_id) == "Bob"


def test_a_correction_by_the_person_supersedes_the_name_with_an_audit_line(store: PeopleStore):
    person_id = enrol(store)
    store.rename(person_id, "Ana", "conversation", now=NOW)
    window = Window((said("it's Anna, two n's", in_view=(view("P2", person_id, name="Ana"),)),))
    changes = apply(name_output(name="Anna", quote="it's Anna, two n's", correction=True), window, store, NOW + 60)
    assert changes[0].kind is ChangeKind.NAME
    assert changes[0].reason == "correction"
    assert store.name_of(person_id) == "Anna"
    profile = store.profile(person_id)
    assert profile is not None and profile.names.aliases == ("Ana",)
    assert json.loads(store.audit_path.read_text().splitlines()[-1])["action"] == "renamed"


def test_the_owner_naming_someone_wins_and_records_the_app_consent_path(store: PeopleStore):
    person_id = enrol(store)
    store.rename(person_id, "Ana", "conversation", now=NOW)
    window = Window((said("her name is Anna", in_view=(view("P2", person_id, name="Ana"),)),))
    apply(
        name_output(name="Anna", quote="her name is Anna", introduction=Introduction.OWNER, correction=True),
        window,
        store,
        NOW + 60,
    )
    profile = store.profile(person_id)
    assert profile is not None and profile.names.preferred == "Anna" and profile.consent.how == "app"


def test_words_that_are_not_an_introduction_never_commit_a_name(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("Ana called earlier", in_view=(view("P2", person_id),)),))
    changes = apply(name_output(quote="Ana called earlier", introduction=Introduction.OTHER), window, store, NOW)
    assert changes[0].kind is ChangeKind.NAME_CANDIDATE
    assert changes[0].reason == "the words are not an introduction"
    assert store.name_of(person_id) is None


def test_a_name_quoted_from_outside_the_window_never_commits(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("hello", in_view=(view("P2", person_id),)),))
    changes = apply(name_output(quote="Hi, I'm Ana", utterance="u_elsewhere"), window, store, NOW)
    assert changes[0].kind is ChangeKind.NAME_CANDIDATE
    assert changes[0].reason == "the quoted words are not in this window"
    assert store.name_of(person_id) is None


def test_committing_a_name_clears_the_candidates_that_were_waiting(store: PeopleStore):
    person_id = enrol(store)
    store.add_name_candidate(person_id, "Ana", now=NOW, quote="I'm Ana", tag="P2")
    window = Window((said("Hi, I'm Ana", in_view=(view("P2", person_id),)),))
    apply(name_output(), window, store, NOW + 10)
    profile = store.profile(person_id)
    assert profile is not None and profile.name_candidates == ()


def test_a_name_for_a_tag_with_no_person_is_refused(store: PeopleStore):
    window = Window((said("Hi, I'm Ana", in_view=(view("P2", None),)),))
    changes = apply(name_output(), window, store, NOW)
    assert changes[0].kind is ChangeKind.REJECTED


# ------------------------------------------------- loops, notes, episodes


def test_an_open_loop_is_stored_with_its_due_date(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("can you find the blue socks by tomorrow", in_view=(view("P2", person_id),)),))
    output = ScribeOutput(open_loops=(ScribeLoop(who="P2", text="find the blue socks", due="2026-09-09"),))
    changes = apply(output, window, store, NOW)
    assert changes[0].kind is ChangeKind.OPEN_LOOP
    digest = store.digest(person_id, NOW)
    assert digest is not None and digest["open_loops"][0]["due"] == "2026-09-09"


def test_an_open_loop_for_an_untracked_tag_is_refused(store: PeopleStore):
    window = Window((said("find the socks", in_view=(view("P2", None),)),))
    output = ScribeOutput(open_loops=(ScribeLoop(who="P2", text="find the socks"),))
    assert apply(output, window, store, NOW)[0].kind is ChangeKind.REJECTED


def test_an_appearance_note_is_an_encounter_note_not_a_fact_about_the_person(store: PeopleStore):
    person_id = enrol(store)
    window = Window((said("hello", in_view=(view("P2", person_id),)),))
    output = ScribeOutput(appearance_notes=(ScribeNote(who="P2", text="red jacket, glasses today"),))
    changes = apply(output, window, store, NOW)
    assert changes[0].kind is ChangeKind.APPEARANCE
    profile = store.profile(person_id)
    assert profile is not None and profile.facts[0].kind is FactKind.APPEARANCE
    digest = store.digest(person_id, NOW)
    assert digest is not None and digest["facts"] == []


def test_an_episode_note_lands_on_everyone_with_an_open_episode(store: PeopleStore):
    first, second = enrol(store, NOW), enrol(store, NOW + 1)
    store.open_episode(first, NOW)
    window = Window((said("hello", in_view=(view("P2", first), view("P3", second))),))
    output = ScribeOutput(episode_note="Ana came into the kitchen and left.")
    changes = only(apply(output, window, store, NOW), ChangeKind.EPISODE_NOTE)
    assert [change.person_id for change in changes] == [first]
    profile = store.profile(first)
    assert profile is not None and profile.episodes[0].summary == "Ana came into the kitchen and left."


def test_an_empty_episode_note_changes_nothing(store: PeopleStore):
    person_id = enrol(store)
    store.open_episode(person_id, NOW)
    window = Window((said("hello", in_view=(view("P2", person_id),)),))
    assert apply(ScribeOutput(episode_note="   "), window, store, NOW) == []


# ------------------------------------------------------------ the queue


def test_the_queue_round_trips_windows_through_disk(tmp_path):
    window = Window((said("Hi, I'm Ana", in_view=(view("P2", "person_1", name="Ana"),)),))
    queue = WindowQueue(tmp_path / "scribe_queue.jsonl")
    queue.push(window, NOW)
    assert WindowQueue(tmp_path / "scribe_queue.jsonl").pending(NOW) == [window]
    queue.pop(window)
    assert WindowQueue(tmp_path / "scribe_queue.jsonl").pending(NOW) == []


def test_the_queue_drops_windows_older_than_an_hour(tmp_path):
    queue = WindowQueue(tmp_path / "queue.jsonl")
    queue.push(Window((said("old", stamp=NOW),)), NOW)
    queue.push(Window((said("new", stamp=NOW + 3600),)), NOW + 3600)
    assert len(queue.pending(NOW + 3601)) == 1


def test_the_queue_is_bounded_by_count_as_well(tmp_path):
    queue = WindowQueue(tmp_path / "queue.jsonl", limit=3)
    for index in range(6):
        queue.push(Window((said(f"m{index}", stamp=NOW + index),)), NOW + index)
    assert [window.messages[0].text for window in queue.pending(NOW + 6)] == ["m3", "m4", "m5"]


def test_a_torn_queue_line_costs_only_that_window(tmp_path):
    path = tmp_path / "queue.jsonl"
    good = json.dumps(window_to_dict(Window((said("kept"),))))
    path.write_text("{ torn\n" + good + "\n")
    assert [window.messages[0].text for window in WindowQueue(path).pending(NOW)] == ["kept"]


def test_a_window_survives_serialization_with_its_views():
    window = Window(
        (
            said(
                "Hi, I'm Ana",
                in_view=(view("P2", "person_1", state=IdentityState.FAMILIAR, name="Ana", enrolling=True),),
            ),
        )
    )
    assert window_from_dict(window_to_dict(window)) == window


# ----------------------------------------------------------- the worker


def test_a_window_that_gemini_refuses_waits_on_disk_and_drains_later(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    calls: list[dict] = []

    def dead(path: str, body: dict, timeout: float | None) -> dict:
        raise RuntimeError("connection refused")

    def alive(path: str, body: dict, timeout: float | None) -> dict:
        calls.append(body)
        return response(EMPTY | {"appearance_notes": [{"who": "P2", "text": "red jacket"}]})

    scribe = Scribe(store, dead, model="gemini-flash", queue_path=tmp_path / "queue.jsonl")
    window = Window((said("hello", in_view=(view("P2", person_id),)),))
    assert scribe.process(window, NOW) == []
    assert scribe.queued == 1
    assert "connection refused" in scribe.last_error

    scribe = Scribe(store, alive, model="gemini-flash", queue_path=tmp_path / "queue.jsonl")
    changes = scribe.drain(NOW + 60)
    assert [change.kind for change in changes] == [ChangeKind.APPEARANCE]
    assert scribe.queued == 0
    assert len(calls) == 1


def test_a_drain_stops_at_the_first_failure_and_keeps_the_rest(store: PeopleStore, tmp_path):
    def dead(path: str, body: dict, timeout: float | None) -> dict:
        raise RuntimeError("still down")

    queue_path = tmp_path / "queue.jsonl"
    queue = WindowQueue(queue_path)
    for index in range(3):
        queue.push(Window((said(f"m{index}", stamp=NOW + index),)), NOW + index)
    scribe = Scribe(store, dead, model="gemini-flash", queue_path=queue_path)
    assert scribe.drain(NOW + 10) == []
    assert scribe.queued == 3


def test_the_worker_spends_a_window_as_soon_as_the_message_cap_fills_it(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    seen = (view("P2", person_id),)
    bodies: list[dict] = []

    def transport(path: str, body: dict, timeout: float | None) -> dict:
        bodies.append(body)
        return response(EMPTY)

    scribe = Scribe(store, transport, model="gemini-flash", queue_path=tmp_path / "queue.jsonl")
    for index in range(WINDOW_MAX_MESSAGES - 1):
        assert scribe.observe(said(f"m{index}", id_=f"u_{index}", stamp=NOW + index, in_view=seen), NOW + index) == []
    assert bodies == []
    scribe.observe(said("last", id_="u_last", stamp=NOW + 20, in_view=seen), NOW + 20)
    assert len(bodies) == 1
    sent = bodies[0]["contents"][0]["parts"][0]["text"]
    assert sent.count("] user (in view: P2)") == WINDOW_MAX_MESSAGES


def test_a_tick_closes_an_idle_window_and_uses_the_generate_path(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    paths: list[str] = []

    def transport(path: str, body: dict, timeout: float | None) -> dict:
        paths.append(path)
        return response(EMPTY)

    scribe = Scribe(store, transport, model="gemini-2.5-flash", queue_path=tmp_path / "queue.jsonl")
    scribe.observe(said("hello", in_view=(view("P2", person_id),)), NOW)
    assert scribe.tick(NOW + 1) == []
    assert paths == []
    assert scribe.tick(NOW + WINDOW_IDLE_SEC) == []
    assert paths == ["/v1beta/models/gemini-2.5-flash:generateContent"]


def test_an_unreadable_answer_is_not_queued_for_a_retry(store: PeopleStore, tmp_path):
    def nonsense(path: str, body: dict, timeout: float | None) -> dict:
        return {"candidates": []}

    scribe = Scribe(store, nonsense, model="gemini-flash", queue_path=tmp_path / "queue.jsonl")
    assert scribe.process(Window((said("hello"),)), NOW) == []
    assert scribe.queued == 0
    assert scribe.last_error == "unreadable scribe answer"


def test_a_scribe_without_a_transport_queues_rather_than_losing_the_window(store: PeopleStore, tmp_path):
    scribe = Scribe(store, None, model="gemini-flash", queue_path=tmp_path / "queue.jsonl")
    assert scribe.process(Window((said("hello"),)), NOW) == []
    assert scribe.queued == 1


def test_an_empty_window_is_never_sent(store: PeopleStore, tmp_path):
    def explode(path: str, body: dict, timeout: float | None) -> dict:
        raise AssertionError("must not be called")

    scribe = Scribe(store, explode, model="gemini-flash", queue_path=tmp_path / "queue.jsonl")
    assert scribe.process(Window(()), NOW) == []


# ------------------------------------------------------------ deep recall


def test_a_memory_question_names_the_person_it_is_about():
    assert is_memory_question("do you remember what Ana asked for?", ["Ana", "Theo"]) == "Ana"
    assert is_memory_question("what did P3 say last time?", []) == "P3"
    assert is_memory_question("did they tell you their name?", []) == "they"


def test_the_longest_matching_name_wins():
    assert is_memory_question("what did Ana Maria say?", ["Ana", "Ana Maria"]) == "Ana Maria"


def test_an_ordinary_sentence_is_not_a_memory_question():
    assert is_memory_question("can you bring me the socks?", ["Ana"]) is None
    assert is_memory_question("hello there", ["Ana"]) is None


def test_a_memory_verb_without_any_referent_is_not_a_question_for_one_person():
    assert is_memory_question("remember to charge yourself", []) is None


def test_the_recall_request_carries_the_record_and_the_question():
    body = recall_request({"id": "person_1", "facts": []}, "what did she ask for?")
    text = " ".join(part["text"] for part in body["contents"][0]["parts"])
    assert "person_1" in text
    assert "what did she ask for?" in text
    assert body["generationConfig"]["responseSchema"]["required"] == ["found", "answer"]


def test_a_recall_answer_is_read_back_only_when_something_was_found():
    assert parse_recall(response({"found": True, "answer": "Ana asked for the blue socks."})) == (
        "Ana asked for the blue socks."
    )
    assert parse_recall(response({"found": False, "answer": ""})) == ""
    assert parse_recall({}) == ""


def test_the_worker_recalls_over_one_persons_whole_memory(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    store.add_fact(person_id, "asked for the blue socks", FactKind.REQUEST, now=NOW, attribution=Attribution.SELF)
    bodies: list[dict] = []

    def transport(path: str, body: dict, timeout: float | None) -> dict:
        bodies.append(body)
        return response({"found": True, "answer": "They asked for the blue socks."})

    scribe = Scribe(store, transport, model="gemini-flash", queue_path=tmp_path / "queue.jsonl")
    assert scribe.recall(person_id, "what did they want?") == "They asked for the blue socks."
    assert "asked for the blue socks" in bodies[0]["contents"][0]["parts"][0]["text"]


def test_recall_is_silence_when_the_person_or_the_connection_is_gone(store: PeopleStore, tmp_path):
    def dead(path: str, body: dict, timeout: float | None) -> dict:
        raise RuntimeError("no connection")

    scribe = Scribe(store, dead, model="gemini-flash", queue_path=tmp_path / "queue.jsonl")
    assert scribe.recall("person_deadbeef", "what did they want?") == ""
    assert scribe.recall(enrol(store), "what did they want?") == ""
    assert "no connection" in scribe.last_error


# ------------------------------------------------- the appearance description
# description.py is the scribe's sibling: one Gemini call, one schema, one line.


def test_the_description_request_carries_the_crop_and_the_constraints():
    body = description.build_request(b"jpegbytes")
    system = body["systemInstruction"]["parts"][0]["text"].lower()
    assert "clothing" in system and "glasses" in system and "age band" in system
    for forbidden in ("emotion", "ethnicity", "health"):
        assert "never mention or infer" in system and forbidden in system
    inline = body["contents"][0]["parts"][0]["inlineData"]
    assert inline["mimeType"] == "image/jpeg"
    assert base64.b64decode(inline["data"]) == b"jpegbytes"
    assert body["generationConfig"]["responseSchema"]["required"] == ["description"]


def test_a_description_is_read_back_and_bounded():
    assert description.parse_description(response({"description": "Man, 30s, glasses."})) == "Man, 30s, glasses."
    assert len(description.parse_description(response({"description": "x" * 500})) or "") == (
        description.MAX_DESCRIPTION_CHARS
    )


def test_an_unusable_crop_or_an_unreadable_answer_yields_no_description():
    assert description.parse_description(response({"description": "   "})) is None
    assert description.parse_description({"candidates": []}) is None
    assert description.parse_description({"candidates": [{"content": {"parts": [{"text": "nope"}]}}]}) is None


def test_describe_calls_the_generate_path_and_survives_an_outage():
    paths: list[str] = []

    def transport(path: str, body: dict, timeout: float | None) -> dict:
        paths.append(path)
        return response({"description": "Woman, 30s, red jacket."})

    def dead(path: str, body: dict, timeout: float | None) -> dict:
        raise RuntimeError("no connection")

    assert description.describe(transport, b"jpeg", model="gemini-flash") == "Woman, 30s, red jacket."
    assert paths == ["/v1beta/models/gemini-flash:generateContent"]
    assert description.describe(dead, b"jpeg", model="gemini-flash") is None


def test_the_crop_is_a_decodable_jpeg_no_larger_than_the_budget():
    frame = np.random.default_rng(0).integers(0, 255, size=(480, 640, 3), dtype=np.uint8)
    jpeg = description.crop_for_description(frame, (0.0, 0.25, 1.0, 0.75))
    assert jpeg is not None
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert max(decoded.shape[:2]) == description.CROP_PX


def test_a_box_running_off_the_frame_is_clamped_and_a_sliver_is_refused():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    assert description.crop_for_description(frame, (-0.5, -0.5, 1.5, 1.5)) is not None
    assert description.crop_for_description(frame, (0.5, 0.5, 0.5, 0.5)) is None
