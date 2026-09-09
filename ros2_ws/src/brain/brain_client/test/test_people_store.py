# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The people store: enrolment, names, merge, forget and tombstones, capacity,
retention, thumbnails, the memory records, and what a digest may carry.

Filesystem only — no ROS, no network."""

from __future__ import annotations

import base64
import json
import stat

import numpy as np
import pytest

from brain_client.people import store as store_module
from brain_client.people.memory import EPISODE_IDLE_SEC, Attribution, FactKind, FactSource, latest_open
from brain_client.people.store import (
    MAX_FACE_TEMPLATES,
    MAX_OUTFITS,
    MAX_THUMBNAILS,
    MAX_UNNAMED,
    OUTFIT_TTL_SEC,
    PeopleStore,
)
from brain_client.people.types import FaceTemplate, OutfitTemplate, RosterView

DAY = 86400.0
NOW = 1_788_818_400.0
EMB = np.ones(4, dtype=np.float32) / 2.0


@pytest.fixture
def store(tmp_path) -> PeopleStore:
    return PeopleStore(tmp_path / "people")


def face(
    stamp: float = NOW,
    model: str = "sface",
    bucket: str = "frontal",
    quality: float = 0.9,
    embedding: np.ndarray | None = None,
) -> FaceTemplate:
    return FaceTemplate(
        embedding=EMB if embedding is None else embedding,
        model=model,
        stamp=stamp,
        pose_bucket=bucket,
        quality=quality,
    )


def unit(index: int, size: int = 32) -> np.ndarray:
    """A unit vector orthogonal to every other one this returns."""
    vector = np.zeros(size, dtype=np.float32)
    vector[index] = 1.0
    return vector


def enrol(store: PeopleStore, now: float = NOW, thumbnail: bytes | None = b"jpeg") -> str:
    return store.create_unnamed([face(now)], thumbnail, now)


# ------------------------------------------------------------ the roster view


def test_the_store_is_a_roster_view(store: PeopleStore):
    roster: RosterView = store
    assert roster.collection_enabled() is True
    assert roster.person_ids() == []


def test_enrolment_mints_a_person_id_of_the_documented_shape(store: PeopleStore):
    person_id = enrol(store)
    assert person_id.startswith("person_")
    assert len(person_id) == len("person_") + 8
    assert int(person_id.removeprefix("person_"), 16) >= 0


def test_enrolment_writes_the_index_the_profile_and_the_templates(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    root = tmp_path / "people"
    assert json.loads((root / "index.json").read_text())["people"][0]["id"] == person_id
    assert json.loads((root / person_id / "person.json").read_text())["id"] == person_id
    assert (root / person_id / "templates.npz").is_file()


def test_a_reopened_store_has_the_roster_the_first_one_left(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    store.add_outfit(person_id, OutfitTemplate(embedding=EMB, model="osnet", stamp=NOW))
    store.add_height_sample(person_id, 1.7, 0.01)
    store.rename(person_id, "Ana", "conversation", now=NOW)

    reopened = PeopleStore(tmp_path / "people")
    assert reopened.person_ids() == [person_id]
    assert reopened.name_of(person_id) == "Ana"
    assert len(reopened.face_templates(person_id, "sface")) == 1
    assert len(reopened.outfits(person_id, "osnet", NOW)) == 1
    height = reopened.height(person_id)
    assert height is not None
    assert (height.mean_m, height.samples) == (pytest.approx(1.7), 1)
    assert height.variance == pytest.approx(0.01)


def test_templates_of_another_model_are_never_returned(store: PeopleStore):
    person_id = enrol(store)
    store.add_face_template(person_id, face(model="inspireface"), None)
    assert len(store.face_templates(person_id, "sface")) == 1
    assert len(store.face_templates(person_id, "inspireface")) == 1


def test_an_outfit_older_than_forty_eight_hours_is_not_offered(store: PeopleStore):
    person_id = enrol(store)
    store.add_outfit(person_id, OutfitTemplate(embedding=EMB, model="osnet", stamp=NOW))
    assert store.outfits(person_id, "osnet", NOW + OUTFIT_TTL_SEC - 1) != []
    assert store.outfits(person_id, "osnet", NOW + OUTFIT_TTL_SEC + 1) == []


def test_the_face_gallery_stays_at_ten_and_keeps_the_rare_pose(store: PeopleStore):
    person_id = store.create_unnamed([], None, NOW)
    for index in range(MAX_FACE_TEMPLATES + 4):
        bucket = "left" if index == 0 else "frontal"
        store.add_face_template(person_id, face(stamp=NOW + index, bucket=bucket, quality=0.9), None)
    templates = store.face_templates(person_id, "sface")
    assert len(templates) == MAX_FACE_TEMPLATES
    assert any(template.pose_bucket == "left" for template in templates)


def test_the_gallery_prunes_templates_that_carry_their_own_embeddings(store: PeopleStore):
    """Every template is a different vector in the field: comparing two of them
    with ``==`` compares their embeddings elementwise, which is not a boolean."""
    person_id = store.create_unnamed([], None, NOW)
    for index in range(MAX_FACE_TEMPLATES + 4):
        bucket = "left" if index == 0 else "frontal"
        store.add_face_template(person_id, face(stamp=NOW + index, bucket=bucket, embedding=unit(index)), None)
    templates = store.face_templates(person_id, "sface")
    assert len(templates) == MAX_FACE_TEMPLATES
    assert any(template.pose_bucket == "left" for template in templates)


def test_templates_of_two_embedding_spaces_live_side_by_side_on_disk(store: PeopleStore, tmp_path):
    """128-d SFace and 512-d InspireFace vectors cannot share one rectangular
    array, and one unwritable file would cost the person their whole gallery."""
    person_id = store.create_unnamed(
        [face(model="sface", embedding=np.ones(128, dtype=np.float32) / 128**0.5)], None, NOW
    )
    store.add_face_template(
        person_id, face(model="inspireface", embedding=np.ones(512, dtype=np.float32) / 512**0.5), None
    )
    store.add_outfit(person_id, OutfitTemplate(embedding=unit(0, 256), model="osnet", stamp=NOW))

    reopened = PeopleStore(tmp_path / "people")
    assert [t.embedding.shape for t in reopened.face_templates(person_id, "sface")] == [(128,)]
    assert [t.embedding.shape for t in reopened.face_templates(person_id, "inspireface")] == [(512,)]
    assert [o.embedding.shape for o in reopened.outfits(person_id, "osnet", NOW)] == [(256,)]


def test_merging_two_people_of_different_embedding_spaces_keeps_both_galleries(store: PeopleStore):
    source = store.create_unnamed([face(model="sface", embedding=np.ones(128, dtype=np.float32) / 128**0.5)], None, NOW)
    target = store.create_unnamed(
        [face(model="inspireface", embedding=np.ones(512, dtype=np.float32) / 512**0.5)], None, NOW + 1
    )
    assert store.merge(source, target, now=NOW + 2) is True
    assert len(store.face_templates(target, "sface")) == 1
    assert len(store.face_templates(target, "inspireface")) == 1


def test_the_same_outfit_seen_again_refreshes_the_one_on_file(store: PeopleStore):
    """A face confirmation every five seconds is ~17k vectors a day, all of them
    scanned per body frame and rewritten on every height sample."""
    person_id = enrol(store)
    for index in range(100):
        store.add_outfit(person_id, OutfitTemplate(embedding=EMB, model="osnet", stamp=NOW + index))
    outfits = store.outfits(person_id, "osnet", NOW + 100)
    assert [outfit.stamp for outfit in outfits] == [NOW + 99]


def test_the_outfit_gallery_keeps_the_most_recent_distinct_looks(store: PeopleStore):
    person_id = enrol(store)
    for index in range(20):
        store.add_outfit(person_id, OutfitTemplate(embedding=unit(index), model="osnet", stamp=NOW + index))
    outfits = store.outfits(person_id, "osnet", NOW + 20)
    assert [outfit.stamp for outfit in outfits] == [NOW + index for index in range(20 - MAX_OUTFITS, 20)]


def test_height_is_a_robust_mean_of_the_samples(store: PeopleStore):
    person_id = enrol(store)
    for value in (1.70, 1.72, 1.68, 2.90):
        store.add_height_sample(person_id, value, 0.01)
    estimate = store.height(person_id)
    assert estimate is not None
    assert estimate.mean_m == pytest.approx(1.71)
    assert estimate.samples == 4


def test_height_is_none_without_samples(store: PeopleStore):
    assert store.height(enrol(store)) is None


def test_a_sighting_moves_last_seen_and_counts_a_new_encounter_only_across_a_gap(store: PeopleStore):
    person_id = enrol(store)
    store.record_sighting(person_id, NOW + 10, "home", (1.0, 2.0, 0.0))
    store.record_sighting(person_id, NOW + 20, "home", (1.0, 2.0, 0.0))
    profile = store.profile(person_id)
    assert profile is not None and profile.encounters == 1
    store.record_sighting(person_id, NOW + 4000, "home", (1.0, 2.0, 0.0))
    profile = store.profile(person_id)
    assert profile is not None and profile.encounters == 2
    assert profile.last_seen is not None and profile.last_seen.map == "home"


def test_a_sighting_reaches_disk_on_flush_not_on_every_tick(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    store.record_sighting(person_id, NOW + 5, "home", None)
    on_disk = json.loads((tmp_path / "people" / person_id / "person.json").read_text())
    assert on_disk["last_seen"]["stamp"] == NOW
    store.flush()
    on_disk = json.loads((tmp_path / "people" / person_id / "person.json").read_text())
    assert on_disk["last_seen"]["stamp"] == NOW + 5


def test_a_sighting_of_an_unknown_person_is_ignored(store: PeopleStore):
    store.record_sighting("person_deadbeef", NOW, "home", None)
    assert store.person_ids() == []


def test_a_sighting_opens_the_episode_the_scribe_writes_its_note_onto(store: PeopleStore):
    """Nothing else opens one, so without this every episode note is dropped."""
    person_id = enrol(store)
    store.record_sighting(person_id, NOW + 10, "kitchen", (3.1, 1.4, 0.0))
    profile = store.profile(person_id)
    assert profile is not None
    episode = latest_open(profile.episodes)
    assert episode is not None
    assert (episode.start, episode.map, episode.x) == (NOW + 10, "kitchen", 3.1)
    assert store.note_episode(person_id, "Asked about the socks.", now=NOW + 20) is True


def test_an_episode_closes_once_the_person_has_been_gone_five_minutes(store: PeopleStore):
    person_id = enrol(store)
    store.record_sighting(person_id, NOW + 10, "kitchen", None)
    store.note_episode(person_id, "Asked about the socks.", now=NOW + 20)

    store.flush(now=NOW + 10 + EPISODE_IDLE_SEC - 1)
    profile = store.profile(person_id)
    assert profile is not None and latest_open(profile.episodes) is not None

    store.flush(now=NOW + 10 + EPISODE_IDLE_SEC + 60)
    profile = store.profile(person_id)
    assert profile is not None
    assert latest_open(profile.episodes) is None
    assert profile.episodes[-1].end == NOW + 10  # they left when they were last seen
    assert profile.episodes[-1].summary == "Asked about the socks."


def test_coming_back_after_the_gap_closes_the_old_episode_and_opens_a_new_one(store: PeopleStore):
    person_id = enrol(store)
    store.record_sighting(person_id, NOW + 10, "kitchen", None)
    store.record_sighting(person_id, NOW + 4000, "hallway", None)
    profile = store.profile(person_id)
    assert profile is not None
    assert [(episode.start, episode.end) for episode in profile.episodes] == [
        (NOW + 10, NOW + 10),
        (NOW + 4000, None),
    ]
    assert profile.encounters == len(profile.episodes)


# ---------------------------------------------------------------- capacity


def test_enrolment_stops_at_the_unnamed_capacity(store: PeopleStore):
    for index in range(MAX_UNNAMED):
        assert store.can_enrol()
        assert enrol(store, NOW + index) != ""
    assert not store.can_enrol()
    assert enrol(store, NOW + 100) == ""
    assert len(store.person_ids()) == MAX_UNNAMED


def test_naming_frees_a_slot_in_the_unnamed_pool(store: PeopleStore):
    ids = [enrol(store, NOW + index) for index in range(MAX_UNNAMED)]
    store.rename(ids[0], "Ana", "app", now=NOW)
    assert store.counts() == (1, MAX_UNNAMED - 1)
    assert store.can_enrol()


def test_collection_off_stops_enrolment_and_persists(store: PeopleStore, tmp_path):
    store.set_collection(False, now=NOW)
    assert not store.can_enrol()
    assert enrol(store) == ""
    assert PeopleStore(tmp_path / "people").collection_enabled() is False


def test_turning_collection_back_on_is_audited(store: PeopleStore):
    store.set_collection(False, now=NOW)
    store.set_collection(True, now=NOW + 1)
    actions = [json.loads(line)["action"] for line in store.audit_path.read_text().splitlines()]
    assert actions.count("collection") == 2


# ------------------------------------------------------------------- names


def test_renaming_records_the_name_the_consent_path_and_an_audit_line(store: PeopleStore):
    person_id = enrol(store)
    assert store.rename(person_id, "Ana", "conversation", now=NOW + 5) is True
    profile = store.profile(person_id)
    assert profile is not None and profile.consent.how == "conversation"
    line = json.loads(store.audit_path.read_text().splitlines()[-1])
    assert line["action"] == "name_learned" and line["name"] == "Ana" and line["previous"] is None


def test_a_correction_keeps_the_old_name_as_an_alias_and_audits_a_rename(store: PeopleStore):
    person_id = enrol(store)
    store.rename(person_id, "Ana", "conversation", now=NOW)
    store.rename(person_id, "Anna", "conversation", now=NOW + 60)
    profile = store.profile(person_id)
    assert profile is not None
    assert profile.names.preferred == "Anna" and profile.names.aliases == ("Ana",)
    assert json.loads(store.audit_path.read_text().splitlines()[-1])["action"] == "renamed"


def test_renaming_back_does_not_duplicate_the_alias(store: PeopleStore):
    person_id = enrol(store)
    store.rename(person_id, "Ana", "app", now=NOW)
    store.rename(person_id, "Anna", "app", now=NOW + 1)
    store.rename(person_id, "Ana", "app", now=NOW + 2)
    profile = store.profile(person_id)
    assert profile is not None and profile.names.aliases == ("Anna",)


def test_renaming_an_unknown_or_empty_name_is_refused(store: PeopleStore):
    person_id = enrol(store)
    assert store.rename("person_deadbeef", "Ana", "app", now=NOW) is False
    assert store.rename(person_id, "  ", "app", now=NOW) is False


def test_two_people_may_carry_the_same_name(store: PeopleStore):
    first, second = enrol(store, NOW), enrol(store, NOW + 1)
    store.rename(first, "Alex", "app", now=NOW)
    store.rename(second, "Alex", "app", now=NOW)
    assert store.name_of(first) == store.name_of(second) == "Alex"
    assert first != second


# ------------------------------------------------------- merge and forget


def test_merge_folds_the_memory_into_the_target_and_tombstones_the_source(store: PeopleStore, tmp_path):
    source, target = enrol(store, NOW), enrol(store, NOW + 1)
    store.add_fact(source, "likes pasta", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    store.add_open_loop(source, "find the socks", now=NOW)
    store.open_episode(source, NOW)
    store.close_episode(source, NOW + 10, "Talked about socks.")
    store.rename(target, "Theo", "app", now=NOW)

    assert store.merge(source, target, now=NOW + 20) is True
    profile = store.profile(target)
    assert profile is not None
    assert [item.text for item in profile.facts] == ["likes pasta"]
    assert [loop.text for loop in profile.open_loops] == ["find the socks"]
    assert profile.names.preferred == "Theo"
    assert profile.encounters == 2
    assert store.person_ids() == [target]
    assert store.is_tombstoned(source)
    assert not (tmp_path / "people" / source).exists()


def test_merge_reissues_colliding_record_ids(store: PeopleStore):
    source, target = enrol(store, NOW), enrol(store, NOW + 1)
    for person in (source, target):
        store.add_fact(person, f"fact of {person}", FactKind.BIOGRAPHY, now=NOW, attribution=Attribution.SELF)
    store.merge(source, target, now=NOW)
    profile = store.profile(target)
    assert profile is not None
    assert sorted(item.id for item in profile.facts) == ["f_01", "f_02"]


def test_merge_gives_an_unnamed_target_the_source_name(store: PeopleStore):
    source, target = enrol(store, NOW), enrol(store, NOW + 1)
    store.rename(source, "Ana", "conversation", now=NOW)
    store.merge(source, target, now=NOW)
    assert store.name_of(target) == "Ana"


def test_merge_keeps_a_named_target_and_files_the_other_name_as_an_alias(store: PeopleStore):
    source, target = enrol(store, NOW), enrol(store, NOW + 1)
    store.rename(source, "Ana", "conversation", now=NOW)
    store.rename(target, "Anna", "app", now=NOW)
    store.merge(source, target, now=NOW)
    profile = store.profile(target)
    assert profile is not None and profile.names.preferred == "Anna" and "Ana" in profile.names.aliases


def test_merge_moves_the_templates_and_the_thumbnails(store: PeopleStore):
    source, target = enrol(store, NOW), enrol(store, NOW + 1)
    store.add_outfit(source, OutfitTemplate(embedding=EMB, model="osnet", stamp=NOW))
    store.merge(source, target, now=NOW)
    assert len(store.face_templates(target, "sface")) == 2
    assert len(store.outfits(target, "osnet", NOW)) == 1
    assert len(store.thumbnails(target)) == 2


def test_merge_writes_one_audit_line_naming_both_ids(store: PeopleStore):
    source, target = enrol(store, NOW), enrol(store, NOW + 1)
    store.merge(source, target, now=NOW + 5)
    line = json.loads(store.audit_path.read_text().splitlines()[-1])
    assert line == {"stamp": NOW + 5, "action": "merged", "person_id": target, "source_id": source}


def test_a_merge_that_cannot_write_the_target_leaves_the_source_whole(store: PeopleStore, tmp_path, monkeypatch):
    """The source's directory is the only copy of what it knows until the target
    has been written and the index has stopped naming it."""
    source, target = enrol(store, NOW), enrol(store, NOW + 1)

    def _no_space(_person_id: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_commit_templates_locked", _no_space)
    with pytest.raises(OSError):
        store.merge(source, target, now=NOW + 2)

    assert (tmp_path / "people" / source).is_dir()
    reopened = PeopleStore(tmp_path / "people")
    assert reopened.profile(source) is not None
    assert len(reopened.face_templates(source, "sface")) == 1


def test_merge_refuses_an_unknown_or_self_target(store: PeopleStore):
    person_id = enrol(store)
    assert store.merge(person_id, person_id, now=NOW) is False
    assert store.merge(person_id, "person_deadbeef", now=NOW) is False
    assert store.merge("person_deadbeef", person_id, now=NOW) is False


def test_forget_removes_everything_and_tombstones_the_id(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    store.add_fact(person_id, "likes pasta", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    assert store.forget(person_id, now=NOW + 1) is True
    assert store.person_ids() == []
    assert store.profile(person_id) is None
    assert store.digest(person_id, NOW) is None
    assert store.is_tombstoned(person_id)
    assert not (tmp_path / "people" / person_id).exists()
    assert json.loads(store.audit_path.read_text().splitlines()[-1])["action"] == "forgotten"


def test_a_tombstone_survives_a_restart_and_the_id_is_never_reused(store: PeopleStore, tmp_path, monkeypatch):
    person_id = enrol(store)
    store.forget(person_id, now=NOW)
    reopened = PeopleStore(tmp_path / "people")
    assert reopened.is_tombstoned(person_id)

    real = store_module.os.urandom
    forced = iter([bytes.fromhex(person_id.removeprefix("person_"))])
    monkeypatch.setattr(store_module.os, "urandom", lambda n: next(forced, real(n)))
    assert enrol(reopened, NOW + 1) != person_id


def test_forget_of_an_unknown_person_is_refused(store: PeopleStore):
    assert store.forget("person_deadbeef", now=NOW) is False


# --------------------------------------------------------------- the memory


def test_a_fact_is_stored_ranked_and_truncated(store: PeopleStore):
    person_id = enrol(store)
    fact_id = store.add_fact(
        person_id,
        "x" * 400,
        FactKind.PREFERENCE,
        now=NOW,
        attribution=Attribution.SELF,
        source=FactSource(utterance_id="u_1", stamp=NOW, quote="x", speaker_tag="P2"),
    )
    profile = store.profile(person_id)
    assert profile is not None and fact_id == "f_01"
    assert len(profile.facts[0].text) == 200
    assert profile.facts[0].source.utterance_id == "u_1"


def test_an_empty_fact_or_an_unknown_person_is_refused(store: PeopleStore):
    person_id = enrol(store)
    assert store.add_fact(person_id, "   ", FactKind.PREFERENCE, now=NOW) is None
    assert store.add_fact("person_deadbeef", "hi", FactKind.PREFERENCE, now=NOW) is None


def test_superseding_a_fact_keeps_both_and_audits_the_replacement(store: PeopleStore):
    person_id = enrol(store)
    first = store.add_fact(person_id, "drinks tea", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    assert first is not None
    second = store.supersede_fact(
        person_id, first, "drinks coffee now", FactKind.PREFERENCE, now=NOW + 60, attribution=Attribution.SELF
    )
    profile = store.profile(person_id)
    assert profile is not None
    assert profile.facts[0].superseded_by == second
    digest = store.digest(person_id, NOW + 60)
    assert digest is not None and [item["text"] for item in digest["facts"]] == ["drinks coffee now"]
    assert json.loads(store.audit_path.read_text().splitlines()[-1])["action"] == "fact_superseded"


def test_open_loops_ride_the_digest_until_they_are_done(store: PeopleStore):
    person_id = enrol(store)
    loop_id = store.add_open_loop(person_id, "find the blue socks", now=NOW, due="2026-09-09")
    assert loop_id is not None
    digest = store.digest(person_id, NOW)
    assert digest is not None and digest["open_loops"] == [
        {"id": "o_01", "text": "find the blue socks", "due": "2026-09-09"}
    ]
    assert store.complete_open_loop(person_id, loop_id, now=NOW + 1) is True
    digest = store.digest(person_id, NOW + 1)
    assert digest is not None and digest["open_loops"] == []


def test_completing_an_unknown_loop_is_refused(store: PeopleStore):
    assert store.complete_open_loop(enrol(store), "o_09", now=NOW) is False


def test_an_episode_opens_closes_and_reaches_the_digest_with_its_summary(store: PeopleStore):
    person_id = enrol(store)
    assert store.open_episode(person_id, NOW, map_name="home", pose=(3.1, 1.4, 0.0), present=("P3",)) == "e_01"
    assert store.close_episode(person_id, NOW + 240, "Asked about the socks.") is True
    digest = store.digest(person_id, NOW + 300)
    assert digest is not None
    assert digest["episodes"] == [{"start": NOW, "end": NOW + 240, "map": "home", "summary": "Asked about the socks."}]


def test_the_digest_carries_only_the_last_three_episode_summaries(store: PeopleStore):
    person_id = enrol(store)
    for index in range(5):
        store.open_episode(person_id, NOW + index * 1000)
        store.close_episode(person_id, NOW + index * 1000 + 10, f"episode {index}")
    digest = store.digest(person_id, NOW)
    assert digest is not None
    assert [episode["summary"] for episode in digest["episodes"]] == ["episode 2", "episode 3", "episode 4"]


def test_an_episode_note_lands_on_the_open_episode_only(store: PeopleStore):
    person_id = enrol(store)
    assert store.note_episode(person_id, "nothing is open", now=NOW) is False
    store.open_episode(person_id, NOW)
    assert store.note_episode(person_id, "Came into the kitchen.", now=NOW + 5) is True
    profile = store.profile(person_id)
    assert profile is not None and profile.episodes[0].summary == "Came into the kitchen."


def test_name_candidates_replace_by_name_and_stay_bounded(store: PeopleStore):
    person_id = enrol(store)
    for index in range(7):
        store.add_name_candidate(person_id, f"Name{index}", now=NOW + index, quote="I'm someone", tag="P2")
    store.add_name_candidate(person_id, "Name6", now=NOW + 99, quote="again", tag="P2")
    profile = store.profile(person_id)
    assert profile is not None
    assert len(profile.name_candidates) == 5
    assert profile.name_candidates[-1].stamp == NOW + 99
    store.clear_name_candidates(person_id, NOW + 100)
    profile = store.profile(person_id)
    assert profile is not None and profile.name_candidates == ()


def test_a_description_is_stored_and_read_back(store: PeopleStore):
    person_id = enrol(store)
    assert store.set_description(person_id, "Man, 30s, glasses.", now=NOW, thumbnail_id="thumb_0") is True
    assert store.description(person_id) == "Man, 30s, glasses."
    assert store.set_description(person_id, "  ", now=NOW) is False


# --------------------------------------------------------------- the digest


def test_the_digest_ranks_facts_and_hides_what_must_not_be_asserted(store: PeopleStore):
    person_id = enrol(store)
    store.add_fact(person_id, "likes pasta", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    store.add_fact(person_id, "sees a cardiologist", FactKind.SENSITIVE, now=NOW, attribution=Attribution.SELF)
    store.add_fact(person_id, "red jacket today", FactKind.APPEARANCE, now=NOW, attribution=Attribution.ROBOT)
    store.add_fact(
        person_id, "somebody said they cycle", FactKind.BIOGRAPHY, now=NOW, attribution=Attribution.UNCERTAIN
    )
    store.add_fact(
        person_id, "works upstairs", FactKind.IDENTITY, now=NOW, attribution=Attribution.SELF, importance=0.9
    )
    digest = store.digest(person_id, NOW)
    assert digest is not None
    assert [item["text"] for item in digest["facts"]] == ["works upstairs", "likes pasta"]


def test_the_digest_carries_last_seen_and_the_encounter_count(store: PeopleStore):
    person_id = enrol(store)
    store.record_sighting(person_id, NOW + 4000, "kitchen", (3.1, 1.4, 0.0))
    digest = store.digest(person_id, NOW + 4000)
    assert digest is not None
    assert digest["encounters"] == 2
    assert digest["last_seen"] == {"stamp": NOW + 4000, "map": "kitchen", "x": 3.1, "y": 1.4}


def test_the_digest_of_an_unknown_person_is_none(store: PeopleStore):
    assert store.digest("person_deadbeef", NOW) is None


def test_recent_lists_who_was_seen_since_a_stamp_most_recent_first(store: PeopleStore):
    old, fresh = enrol(store, NOW - 4 * DAY), enrol(store, NOW)
    store.rename(fresh, "Marc", "app", now=NOW)
    store.record_sighting(old, NOW - 4 * DAY, "hallway", None)
    store.record_sighting(fresh, NOW, "kitchen", None)
    assert [entry["person_id"] for entry in store.recent(NOW - DAY)] == [fresh]
    assert store.recent(NOW - DAY)[0]["name"] == "Marc"
    assert len(store.recent(NOW - 10 * DAY)) == 2


def test_the_roster_row_carries_what_the_settings_page_shows(store: PeopleStore):
    person_id = enrol(store)
    store.rename(person_id, "Ana", "app", now=NOW)
    store.set_description(person_id, "Woman, 30s, glasses.", now=NOW)
    row = store.roster()[0]
    assert row["person_id"] == person_id
    assert row["name"] == "Ana"
    assert row["unnamed"] is False
    assert row["encounters"] == 1
    assert row["last_seen"] == {"stamp": NOW, "map": None, "x": None, "y": None}
    assert row["description"] == "Woman, 30s, glasses."
    assert row["thumbnail"] is None
    assert store.roster(include_thumbnails=True)[0]["thumbnail"] == base64.b64encode(b"jpeg").decode()


# ------------------------------------------------------------- thumbnails


def test_at_most_three_thumbnails_are_kept_and_the_oldest_goes(store: PeopleStore, tmp_path):
    person_id = store.create_unnamed([], None, NOW)
    for index in range(MAX_THUMBNAILS + 2):
        assert store.add_thumbnail(person_id, f"jpeg{index}".encode()) == f"thumb_{index}"
    assert store.thumbnail_ids(person_id) == ["thumb_2", "thumb_3", "thumb_4"]
    assert store.thumbnails(person_id) == [b"jpeg2", b"jpeg3", b"jpeg4"]
    assert sorted(path.name for path in (tmp_path / "people" / person_id).glob("thumb_*.jpg")) == [
        "thumb_2.jpg",
        "thumb_3.jpg",
        "thumb_4.jpg",
    ]


def test_thumbnails_reload_with_the_store(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    assert PeopleStore(tmp_path / "people").thumbnails(person_id) == [b"jpeg"]


def test_a_thumbnail_for_an_unknown_person_is_refused(store: PeopleStore):
    assert store.add_thumbnail("person_deadbeef", b"jpeg") is None


# --------------------------------------------------------------- retention


def test_an_unnamed_person_expires_after_fourteen_days_unseen(store: PeopleStore):
    person_id = enrol(store)
    assert store.expire(NOW + 13 * DAY) == []
    assert store.expire(NOW + 15 * DAY) == [person_id]
    assert store.is_tombstoned(person_id)
    assert json.loads(store.audit_path.read_text().splitlines()[-1])["action"] == "expired"


def test_a_named_person_survives_far_longer_and_then_expires(store: PeopleStore):
    person_id = enrol(store)
    store.rename(person_id, "Ana", "app", now=NOW)
    assert store.expire(NOW + 100 * DAY) == []
    assert store.expire(NOW + 549 * DAY) == [person_id]


def test_a_profile_may_carry_its_own_retention(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    path = tmp_path / "people" / person_id / "person.json"
    data = json.loads(path.read_text())
    data["retention_days"] = 1.0
    path.write_text(json.dumps(data))
    reopened = PeopleStore(tmp_path / "people")
    assert reopened.expire(NOW + 2 * DAY) == [person_id]


def test_expiry_drops_stale_outfits_without_touching_the_person(store: PeopleStore):
    person_id = enrol(store)
    store.add_outfit(person_id, OutfitTemplate(embedding=EMB, model="osnet", stamp=NOW))
    assert store.expire(NOW + OUTFIT_TTL_SEC + 60) == []
    assert store.outfits(person_id, "osnet", NOW) == []


def test_retention_windows_are_configurable(tmp_path):
    store = PeopleStore(tmp_path / "people", retention_unnamed_days=1.0, retention_named_days=2.0)
    unnamed, named = enrol(store, NOW), enrol(store, NOW)
    store.rename(named, "Ana", "app", now=NOW)
    assert store.expire(NOW + 1.5 * DAY) == [unnamed]
    assert store.expire(NOW + 2.5 * DAY) == [named]


# ---------------------------------------------------------------- on disk


def test_the_store_is_private_to_the_robot(tmp_path):
    """RFC section 10: face templates are special-category data and the files
    under data/people/ are mode 0700, person directories included."""
    root = tmp_path / "people"
    store = PeopleStore(root)
    person_id = enrol(store)

    assert stat.S_IMODE(root.stat().st_mode) == store_module.DIR_MODE
    assert stat.S_IMODE((root / person_id).stat().st_mode) == store_module.DIR_MODE


def test_a_directory_that_was_already_there_is_tightened_rather_than_left_open(tmp_path):
    """``mkdir(mode=...)`` sets the mode only on a directory it creates, so a
    people directory restored from a backup would stay world-readable."""
    root = tmp_path / "people"
    root.mkdir(mode=0o755)
    store = PeopleStore(root)
    person_id = enrol(store)

    assert stat.S_IMODE(root.stat().st_mode) == store_module.DIR_MODE
    assert stat.S_IMODE((root / person_id).stat().st_mode) == store_module.DIR_MODE
    for path in ("index.json", "audit.log", f"{person_id}/person.json", f"{person_id}/templates.npz"):
        assert stat.S_IMODE((root / path).stat().st_mode) == store_module.FILE_MODE, path


def test_reopening_a_store_tightens_a_directory_somebody_loosened(tmp_path):
    root = tmp_path / "people"
    person_id = enrol(PeopleStore(root))
    root.chmod(0o755)
    (root / person_id).chmod(0o755)

    PeopleStore(root)
    assert stat.S_IMODE(root.stat().st_mode) == store_module.DIR_MODE
    assert stat.S_IMODE((root / person_id).stat().st_mode) == store_module.DIR_MODE


def test_the_scribes_queue_lands_inside_that_directory_with_the_same_mode(tmp_path):
    """The queue holds transcripts and person ids waiting on a connection; it is
    the same data under the same lock-and-key as the roster beside it."""
    from brain_client.people.scribe import WindowQueue

    root = tmp_path / "people"
    WindowQueue(root / "scribe_queue.jsonl").clear()
    assert stat.S_IMODE(root.stat().st_mode) == store_module.DIR_MODE


def test_no_temporary_files_survive_a_burst_of_writes(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    store.add_fact(person_id, "likes pasta", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    store.add_face_template(person_id, face(stamp=NOW + 1), b"jpeg2")
    store.flush()
    assert list((tmp_path / "people").rglob("*.tmp")) == []


def test_the_tag_counter_persists_and_never_goes_backwards(store: PeopleStore, tmp_path):
    assert store.next_tag() == 1
    store.set_next_tag(7)
    store.set_next_tag(3)
    assert PeopleStore(tmp_path / "people").next_tag() == 7


def test_an_unreadable_profile_costs_that_person_not_the_roster(store: PeopleStore, tmp_path):
    kept, broken = enrol(store, NOW), enrol(store, NOW + 1)
    (tmp_path / "people" / broken / "person.json").write_text("{ this is not json")
    assert PeopleStore(tmp_path / "people").person_ids() == [kept]


def test_unreadable_templates_cost_recognition_not_the_memory(store: PeopleStore, tmp_path):
    person_id = enrol(store)
    store.add_fact(person_id, "likes pasta", FactKind.PREFERENCE, now=NOW, attribution=Attribution.SELF)
    (tmp_path / "people" / person_id / "templates.npz").write_bytes(b"not an npz")
    reopened = PeopleStore(tmp_path / "people")
    assert reopened.face_templates(person_id, "sface") == []
    digest = reopened.digest(person_id, NOW)
    assert digest is not None and len(digest["facts"]) == 1


def test_an_unreadable_index_starts_an_empty_roster(store: PeopleStore, tmp_path):
    enrol(store)
    (tmp_path / "people" / "index.json").write_text("]not json[")
    assert PeopleStore(tmp_path / "people").person_ids() == []


def test_an_index_from_a_future_version_is_not_interpreted(store: PeopleStore, tmp_path):
    enrol(store)
    path = tmp_path / "people" / "index.json"
    data = json.loads(path.read_text())
    data["version"] = 99
    path.write_text(json.dumps(data))
    assert PeopleStore(tmp_path / "people").person_ids() == []
