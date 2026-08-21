# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Spatial memory: admission policy, visit tracking, frame quality, the
on-disk store, the recorder's gates, and the Gemini search with its
context-cache lifecycle."""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from brain_client.brain import memory_search as memory_search_module
from brain_client.brain.memory_search import MemorySearch, verdict_text
from brain_client.brain.transport import CACHED_CONTENTS_PATH, GeminiHttpError, GeminiRest
from brain_client.memory import recorder as recorder_module
from brain_client.memory import selection as selection_module
from brain_client.memory.coverage import Coverage, wedge_mask
from brain_client.memory.quality import MIN_SHARPNESS, frame_sharpness, frame_worth_keeping
from brain_client.memory.recorder import MemoryRecorder
from brain_client.memory.selection import MAX_MEMORIES, plan_admission
from brain_client.memory.store import MAPPING_SESSION, Memory, MemoryStore, StaleStageError
from brain_client.state.map import Map


def encoded(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    return buffer.tobytes()


def frame(seed: int) -> bytes:
    """A distinct, deterministic frame that passes the quality gates."""
    rng = np.random.default_rng(seed)
    return encoded(rng.integers(0, 255, size=(240, 320), dtype=np.uint8))


GOOD_JPEG = frame(0)
DARK_JPEG = encoded(np.full((240, 320), 5, dtype=np.uint8))


def memory(id_: int, x: float, y: float = 0.0, theta: float = 0.0, stamp: float = 1000.0) -> Memory:
    return Memory(id=id_, x=x, y=y, theta=theta, stamp=stamp)


def grid_of(cells: np.ndarray, resolution: float = 0.1) -> Map:
    """A Map over a raw cell array; world (0,0) at the grid corner."""
    return Map(
        resolution=resolution,
        width=cells.shape[1],
        height=cells.shape[0],
        origin_x=0.0,
        origin_y=0.0,
        raw_source=SimpleNamespace(data=cells.flatten().tolist()),
    )


def open_room(size: int = 30) -> np.ndarray:
    return np.zeros((size, size), dtype=np.int8)


# ================= admission policy =================


def test_first_viewpoint_is_recorded():
    plan = plan_admission([], 0.0, 0.0, 0.0, stamp=1000.0)
    assert plan.record and plan.replace is None and plan.evict is None


def test_near_duplicate_viewpoint_is_skipped():
    plan = plan_admission([memory(1, 0.0, 0.0)], 0.3, 0.2, 0.2, stamp=1010.0)
    assert not plan.record


def test_same_spot_facing_elsewhere_is_a_new_view():
    plan = plan_admission([memory(1, 0.0, 0.0, theta=0.0)], 0.0, 0.0, math.radians(120), stamp=1010.0)
    assert plan.record and plan.replace is None


def test_distance_alone_makes_a_new_view():
    plan = plan_admission([memory(1, 0.0, 0.0)], 1.5, 0.0, 0.0, stamp=1010.0)
    assert plan.record and plan.replace is None


def test_the_same_view_refreshes_once_aged():
    old = memory(1, 0.0, 0.0, stamp=1000.0)
    plan = plan_admission([old], 0.1, 0.05, math.radians(8), stamp=1070.0)
    assert plan.record and plan.replace == old and plan.evict is None


def test_a_recent_picture_is_not_rewritten():
    old = memory(1, 0.0, 0.0, stamp=1000.0)
    plan = plan_admission([old], 0.1, 0.05, math.radians(8), stamp=1005.0)
    assert not plan.record


def test_an_oblique_frame_never_replaces_a_straight_on_view():
    # The mid-turn case: still redundant with the memory (heading < 100 deg),
    # but no longer the same picture — skip, never overwrite.
    old = memory(1, 0.5, 0.5, stamp=1000.0)
    plan = plan_admission([old], 0.5, 0.5, math.radians(40), stamp=2000.0, grid=grid_of(open_room()))
    assert not plan.record


def test_a_same_heading_frame_with_clear_sight_refreshes_from_a_step_back():
    # Same aim, displaced along it, free space between the capture points: the
    # same information, one frame merely a bit further away — it may update.
    old = memory(1, 0.5, 0.5, stamp=1000.0)
    plan = plan_admission([old], 1.3, 0.5, math.radians(5), stamp=2000.0, grid=grid_of(open_room()))
    assert plan.record and plan.replace == old


def test_a_side_step_off_the_view_axis_never_refreshes():
    # Same aim, but displaced ACROSS it: the frame slides new information into
    # view even though both cameras point the same way.
    old = memory(1, 0.5, 0.5, theta=0.0, stamp=1000.0)
    plan = plan_admission([old], 0.5, 1.3, math.radians(5), stamp=2000.0, grid=grid_of(open_room()))
    assert not plan.record


def test_a_wall_between_the_capture_points_blocks_the_refresh():
    cells = open_room()
    cells[:, 9] = 100  # a wall at world x = 0.9-1.0
    old = memory(1, 0.5, 0.5, stamp=1000.0)
    plan = plan_admission([old], 1.3, 0.5, math.radians(5), stamp=2000.0, grid=grid_of(cells))
    assert not plan.record


def test_unknown_cells_between_the_capture_points_block_the_refresh():
    cells = open_room()
    cells[:, 9] = -1  # the map cannot vouch for what is here
    old = memory(1, 0.5, 0.5, stamp=1000.0)
    plan = plan_admission([old], 1.3, 0.5, math.radians(5), stamp=2000.0, grid=grid_of(cells))
    assert not plan.record


def test_without_a_grid_only_a_tight_pose_match_refreshes():
    old = memory(1, 0.0, 0.0, stamp=1000.0)
    assert not plan_admission([old], 0.6, 0.0, 0.0, stamp=2000.0).record
    assert plan_admission([old], 0.2, 0.0, 0.0, stamp=2000.0).replace == old


# ================= visibility paint =================


def test_the_wedge_stops_at_walls():
    cells = open_room()
    cells[:, 20] = 100  # a wall at world x = 2.0-2.1
    mask = wedge_mask(grid_of(cells), 1.5, 1.5, 0.0)
    assert mask is not None
    assert mask[15, 16]  # free floor ahead, before the wall
    assert not mask[:, 21:].any()  # nothing painted beyond it


def test_a_quarter_turn_at_a_painted_spot_is_novel():
    # The old 100-degree rule skipped this; paint says ~70% of the wedge is new.
    old = memory(1, 1.5, 1.5, theta=0.0, stamp=1000.0)
    plan = plan_admission([old], 1.5, 1.5, math.pi / 2, stamp=1005.0, grid=grid_of(open_room()), coverage=Coverage())
    assert plan.record and plan.replace is None


def test_a_fully_painted_wedge_is_skipped():
    old = memory(1, 1.5, 1.5, theta=0.0, stamp=1000.0)
    plan = plan_admission(
        [old], 1.5, 1.5, math.radians(5), stamp=1005.0, grid=grid_of(open_room()), coverage=Coverage()
    )
    assert not plan.record


def test_a_wall_closeup_earns_a_slot_where_the_wall_is_unknown():
    cells = open_room()
    cells[:, :2] = 100  # a wall along world x < 0.2
    plan = plan_admission([], 0.35, 1.5, math.pi, stamp=1000.0, grid=grid_of(cells), coverage=Coverage())
    assert plan.record  # cramped, but nothing shows this wall yet — a closeup beats nothing


def test_wall_closeups_do_not_pile_up():
    cells = open_room()
    cells[:, :2] = 100
    seen = memory(1, 0.35, 1.5, theta=math.pi, stamp=1000.0)
    plan = plan_admission([seen], 0.35, 1.5, math.pi, stamp=1005.0, grid=grid_of(cells), coverage=Coverage())
    assert not plan.record  # the wall is known: a cramped view must be nearly all new


def test_stale_paint_no_longer_vouches_for_the_scene():
    # Side-stepped off the view axis: the same-view refresh can't fire, and
    # fresh paint reads the wedge as covered — but once the paint ages out,
    # the area records anew without touching the old shot's slot.
    old = memory(1, 1.5, 1.5, theta=0.0, stamp=1000.0)
    grid = grid_of(open_room())
    fresh = plan_admission([old], 1.5, 1.9, 0.0, stamp=1005.0, grid=grid, coverage=Coverage())
    assert not fresh.record
    stale = plan_admission([old], 1.5, 1.9, 0.0, stamp=1400.0, grid=grid, coverage=Coverage())
    assert stale.record and stale.replace is None


def test_a_blind_pose_earns_no_slot():
    cells = open_room()
    cells[:, 10:] = 100  # solid mass from x = 1.0
    plan = plan_admission([], 1.05, 1.5, 0.0, stamp=1000.0, grid=grid_of(cells), coverage=Coverage())
    assert not plan.record


def test_the_further_back_shot_outlives_the_closeups_it_subsumes():
    cells = open_room(60)
    cells[:, 35] = 100  # a wall at world x = 3.5
    wide = memory(1, 1.0, 3.0, theta=0.0)
    twin = memory(2, 1.0, 3.0, theta=0.0)  # zeroes the wide view's unique paint, like the closeup's
    closeup = memory(3, 3.0, 3.0, theta=0.0)  # against the wall, inside the wide view
    assert Coverage().least_unique([wide, twin, closeup], grid_of(cells)) == closeup


def test_at_capacity_the_most_replaceable_paint_makes_room(monkeypatch):
    monkeypatch.setattr(selection_module, "MAX_MEMORIES", 3)
    twins = [memory(1, 1.0, 3.0, theta=0.0), memory(2, 1.0, 3.0, theta=0.0)]
    distinct = memory(3, 4.0, 3.0, theta=0.0)
    plan = plan_admission(
        [*twins, distinct], 2.0, 3.0, math.pi, stamp=2000.0, grid=grid_of(open_room(60)), coverage=Coverage()
    )
    assert plan.record and plan.evict is not None and plan.evict.id in (1, 2)  # never the lone wide view


def test_at_capacity_evicts_the_older_of_the_closest_pair():
    spread = [memory(i, x=3.0 * i, stamp=500.0 + i) for i in range(MAX_MEMORIES - 2)]
    older_twin = memory(90, x=200.0, stamp=100.0)
    newer_twin = memory(91, x=200.4, stamp=200.0)
    plan = plan_admission([*spread, older_twin, newer_twin], -5.0, 0.0, 0.0, stamp=1000.0)
    assert plan.record and plan.evict == older_twin


# ================= frame quality =================


def test_a_textured_bright_frame_is_kept():
    assert frame_worth_keeping(GOOD_JPEG)


def test_a_dark_frame_is_rejected():
    assert not frame_worth_keeping(DARK_JPEG)


def test_a_featureless_frame_is_rejected():
    assert not frame_worth_keeping(encoded(np.full((240, 320), 128, dtype=np.uint8)))


def test_a_blurred_frame_is_rejected():
    rng = np.random.default_rng(3)
    noise = rng.integers(0, 255, size=(240, 320), dtype=np.uint8)
    assert not frame_worth_keeping(encoded(cv2.GaussianBlur(noise, (31, 31), 0)))


def test_undecodable_bytes_are_rejected():
    assert not frame_worth_keeping(b"\xff\xd8not-a-jpeg")


# ================= store =================


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / "A.pgm").write_bytes(b"map-A-content")
    (tmp_path / "maps" / "B.pgm").write_bytes(b"map-B-content")
    return tmp_path


def test_store_round_trips_across_instances(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    first = store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.add(4.0, 2.0, 0.5, 1001.0, b"jpg-two")
    assert first is not None

    reloaded = MemoryStore(data_dir)
    reloaded.switch_map("A.yaml")
    snapshot = reloaded.snapshot()
    assert [m.id for m in snapshot.memories] == [1, 2]
    assert snapshot.memories[0] == first
    path = reloaded.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-one"


def test_remapping_under_the_same_name_wipes_stale_memories(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")

    (data_dir / "maps" / "A.pgm").write_bytes(b"remapped-content")
    reloaded = MemoryStore(data_dir)
    reloaded.switch_map("A.yaml")
    assert reloaded.snapshot().memories == ()
    assert list((data_dir / "spatial_memory" / "A").glob("*.jpg")) == []


def test_a_same_name_remap_is_caught_without_a_map_switch(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.switch_map("A.yaml")  # the every-tick call: a no-op while the file is unchanged
    assert len(store.snapshot().memories) == 1

    (data_dir / "maps" / "A.pgm").write_bytes(b"remapped-content")
    store.switch_map("A.yaml")
    assert store.snapshot().memories == ()


def test_a_touched_but_identical_map_keeps_memories(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    (data_dir / "maps" / "A.pgm").write_bytes(b"map-A-content")
    store.switch_map("A.yaml")
    assert len(store.snapshot().memories) == 1


def test_maps_have_isolated_memories(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.switch_map("B.yaml")
    assert store.snapshot().memories == ()
    store.switch_map("A.yaml")
    assert len(store.snapshot().memories) == 1


def test_eviction_removes_the_image_file(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    added = store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    assert added is not None
    path = store.image_path(added.id)
    assert path is not None and path.exists()
    store.evict(added)
    assert not path.exists()
    assert store.snapshot().memories == ()


def test_forget_removes_the_memory_and_its_image(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    added = store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    assert added is not None
    # The truncated digest from /brain/memory_positions — what a client sends.
    assert store.forget(added.id, store.snapshot().fingerprint[:12]) == added
    path = store.image_path(added.id)
    assert path is not None and not path.exists()
    assert store.snapshot().memories == ()


def test_forget_with_a_stale_fingerprint_is_refused(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    added = store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    assert added is not None
    assert store.forget(added.id, "fingerprint-of-the-old-map") is None
    assert len(store.snapshot().memories) == 1


def test_forget_an_unknown_id_is_a_noop(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    assert store.forget(99) is None
    assert len(store.snapshot().memories) == 1


def test_replace_keeps_the_slot_and_updates_everything_else(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    added = store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-old")
    assert added is not None
    store.replace(added, 1.1, 2.1, 0.6, 2000.0, b"jpg-new")
    (kept,) = store.snapshot().memories
    assert kept.id == added.id and kept.stamp == 2000.0 and kept.x == 1.1
    path = store.image_path(kept.id)
    assert path is not None and path.read_bytes() == b"jpg-new"


def test_clear_forgets_the_map_for_good(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.add(4.0, 2.0, 0.5, 1001.0, b"jpg-two")
    assert store.clear() == 2
    assert store.snapshot().memories == ()
    assert list((data_dir / "spatial_memory" / "A").glob("*.jpg")) == []

    reloaded = MemoryStore(data_dir)
    reloaded.switch_map("A.yaml")
    assert reloaded.snapshot().memories == ()  # the wipe was committed, not just in-memory
    fresh = reloaded.add(1.0, 2.0, 0.5, 2000.0, b"jpg-new")
    assert fresh is not None and fresh.id == 1  # ids restart with the fresh memory


def test_clear_without_a_map_is_a_noop(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map(None)
    assert store.clear() == 0


def test_without_a_map_nothing_is_recorded(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map(None)
    assert store.add(1.0, 2.0, 0.5, 1000.0, b"jpg") is None
    assert store.snapshot().memories == ()


def test_promote_hands_the_stage_to_the_saved_map(data_dir):
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.add(4.0, 2.0, 0.5, 1001.0, b"jpg-two")
    (data_dir / "maps" / "new.pgm").write_bytes(b"new-map-content")
    assert store.promote_mapping_session("new.yaml") == 2

    store.switch_map("new.yaml")
    snapshot = store.snapshot()
    assert [m.id for m in snapshot.memories] == [1, 2] and snapshot.fingerprint
    path = store.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-one"
    # The stage outlives the promotion (a re-save must re-promote the whole
    # tour); the next session's entry wipes it.
    assert (data_dir / "spatial_memory" / MAPPING_SESSION / "1.jpg").exists()


def test_a_resave_promotes_the_whole_tour_again(data_dir):
    # A failed mode switch leaves the robot mapping and the webapp retries the
    # save. The second promotion must carry the whole tour — not replace the
    # first batch with just the frames staged since.
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-v1")
    assert store.promote_mapping_session("tour.yaml") == 1

    store.add(3.0, 2.0, 0.5, 1001.0, b"jpg-two")
    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-v2")  # SLAM kept refining
    assert store.promote_mapping_session("tour.yaml") == 2

    store.switch_map("tour.yaml")
    snapshot = store.snapshot()
    assert [m.id for m in snapshot.memories] == [1, 2] and snapshot.fingerprint
    path = store.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-one"


def test_promote_replaces_the_names_previous_memories(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(9.0, 9.0, 0.0, 900.0, b"jpg-old")
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-new")
    assert store.promote_mapping_session("A.yaml") == 1

    store.switch_map("A.yaml")
    (memory,) = store.snapshot().memories
    assert memory.id == 1 and memory.x == 1.0
    path = store.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-new"


def test_promote_with_nothing_staged_is_a_noop(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    assert store.promote_mapping_session("A.yaml") == 0
    assert len(store.snapshot().memories) == 1


def test_promote_without_a_readable_map_keeps_the_stage(data_dir):
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    assert store.promote_mapping_session("ghost.yaml") is None
    snapshot = store.snapshot()
    assert snapshot.map_name == MAPPING_SESSION and len(snapshot.memories) == 1


def test_promote_reaches_a_stage_the_store_left(data_dir):
    # Mid mode-switch the tick can land the store back on the previous map
    # ("switching" republishes it) before the save announcement arrives; the
    # stage is promoted from disk and picked up on the eventual switch.
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.switch_map("A.yaml")
    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-map-content")
    assert store.promote_mapping_session("tour.yaml") == 1
    assert store.snapshot().map_name == "A.yaml"  # the detour is undisturbed

    store.switch_map("tour.yaml")
    snapshot = store.snapshot()
    assert len(snapshot.memories) == 1 and snapshot.fingerprint
    path = store.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-one"


def test_the_session_survives_its_every_tick_call(data_dir):
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    fingerprint = store.snapshot().fingerprint
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.use_mapping_session()  # the recorder's every-tick call must not wipe
    snapshot = store.snapshot()
    assert len(snapshot.memories) == 1 and snapshot.fingerprint == fingerprint


def test_each_session_is_its_own_coordinate_frame(data_dir):
    # Clients (the search cache, forget's staleness check, the webapp) tell
    # frames apart by map+fingerprint, and every session is named ".mapping" —
    # only a fresh per-session fingerprint keeps one tour's ids from resolving
    # against another's pictures.
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    first = store.snapshot().fingerprint
    store.switch_map("A.yaml")
    store.use_mapping_session()
    assert first and store.snapshot().fingerprint and store.snapshot().fingerprint != first


def test_session_entry_sweeps_crashed_promotion_scratch(data_dir):
    store = MemoryStore(data_dir)
    (data_dir / "spatial_memory" / ".mapping.promote").mkdir(parents=True)
    (data_dir / "spatial_memory" / ".mapping.displaced").mkdir()
    store.use_mapping_session()
    assert not (data_dir / "spatial_memory" / ".mapping.promote").exists()
    assert not (data_dir / "spatial_memory" / ".mapping.displaced").exists()


def test_restart_lands_a_promotion_cut_down_mid_swap(data_dir):
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-tour")
    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-map-content")
    assert store.promote_mapping_session("tour.yaml") == 1

    # Rewind to the crash window between promote's two os.replace calls: the
    # stamped copy back in scratch, the old memories displaced, the map's
    # directory gone.
    root = data_dir / "spatial_memory"
    (root / "tour").rename(root / ".mapping.promote")
    (root / ".mapping.displaced").mkdir()

    reloaded = MemoryStore(data_dir)
    reloaded.switch_map("tour.yaml")
    (memory,) = reloaded.snapshot().memories
    assert memory.id == 1
    path = reloaded.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-tour"
    assert not (root / ".mapping.promote").exists()
    assert not (root / ".mapping.displaced").exists()


def test_a_failed_swap_puts_the_displaced_memories_back(data_dir, monkeypatch):
    # The landing os.replace fails while the process lives on: the map must
    # get its displaced memories back, and the stage must still hold the tour
    # so a re-save can retry the whole promotion.
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(9.0, 9.0, 0.0, 900.0, b"jpg-old")
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-new")

    real_replace = os.replace

    def failing_landing(src, dst):
        if Path(src).name == ".mapping.promote" and Path(dst).name == "A":
            raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_landing)
    with pytest.raises(OSError):
        store.promote_mapping_session("A.yaml")
    monkeypatch.undo()

    store.switch_map("A.yaml")
    (memory,) = store.snapshot().memories
    assert memory.x == 9.0  # the displaced memories are back
    assert store.promote_mapping_session("A.yaml") == 1  # the retry lands the tour
    store.switch_map("A.yaml")
    (memory,) = store.snapshot().memories
    assert memory.x == 1.0


def test_session_entry_lands_a_stranded_promotion(data_dir):
    # A failed swap whose in-line restore also failed leaves the stamped tour
    # in scratch and the map's directory gone; the next session's entry must
    # land it, not sweep it.
    store = MemoryStore(data_dir)
    store.use_mapping_session(1234.5)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-tour")
    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-map-content")
    assert store.promote_mapping_session("tour.yaml") == 1

    root = data_dir / "spatial_memory"
    (root / "tour").rename(root / ".mapping.promote")
    (root / ".mapping.displaced").mkdir()

    store.use_mapping_session(5678.0)  # the next tour begins
    assert not (root / ".mapping.promote").exists()
    assert not (root / ".mapping.displaced").exists()
    store.switch_map("tour.yaml")
    (memory,) = store.snapshot().memories
    assert memory.id == 1
    path = store.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-tour"


def test_restart_leaves_an_unstamped_promotion_copy_alone(data_dir):
    # A crash before the stamp landed leaves the copy still naming ".mapping";
    # landing it anywhere would mint a bogus map dir. Session entry sweeps it.
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-tour")
    root = data_dir / "spatial_memory"
    shutil.copytree(root / MAPPING_SESSION, root / ".mapping.promote")

    MemoryStore(data_dir)
    assert (root / ".mapping.promote").is_dir()
    assert (root / MAPPING_SESSION / "1.jpg").exists()


def test_entering_a_session_wipes_the_previous_ones_leftovers(data_dir):
    store = MemoryStore(data_dir)
    store.use_mapping_session()
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.switch_map("A.yaml")  # mapping abandoned without a save
    store.use_mapping_session()
    assert store.snapshot().memories == ()
    assert list((data_dir / "spatial_memory" / MAPPING_SESSION).glob("*.jpg")) == []


def test_a_restarted_store_adopts_the_same_sessions_stage(data_dir):
    # A brain-only restart mid-tour: slam_toolbox never died, the frame is
    # still live, so the half-tour must survive the respawn.
    store = MemoryStore(data_dir)
    store.use_mapping_session(1234.5)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    fingerprint = store.snapshot().fingerprint

    reborn = MemoryStore(data_dir)
    reborn.use_mapping_session(1234.5)
    snapshot = reborn.snapshot()
    assert [m.id for m in snapshot.memories] == [1] and snapshot.fingerprint == fingerprint
    path = reborn.image_path(1)
    assert path is not None and path.read_bytes() == b"jpg-one"
    added = reborn.add(4.0, 2.0, 0.5, 1001.0, b"jpg-two")
    assert added is not None and added.id == 2  # ids continue, not restart


def test_another_sessions_stage_is_wiped_on_entry(data_dir):
    store = MemoryStore(data_dir)
    store.use_mapping_session(1234.5)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")

    reborn = MemoryStore(data_dir)
    reborn.use_mapping_session(9999.0)
    assert reborn.snapshot().memories == ()
    assert list((data_dir / "spatial_memory" / MAPPING_SESSION).glob("*.jpg")) == []


def test_a_sessionless_entry_keeps_the_legacy_wipe(data_dir):
    store = MemoryStore(data_dir)
    store.use_mapping_session(1234.5)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")

    reborn = MemoryStore(data_dir)
    reborn.use_mapping_session()
    assert reborn.snapshot().memories == ()


def test_a_new_session_stamp_supersedes_the_stage_in_place(data_dir):
    # mode_manager restarted slam under a live brain: the store is already on
    # the stage, but the new stamp names a new frame — the tick must wipe.
    store = MemoryStore(data_dir)
    store.use_mapping_session(1234.5)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    store.use_mapping_session(1234.5)  # the every-tick call must not wipe
    assert len(store.snapshot().memories) == 1
    store.use_mapping_session(5678.0)
    assert store.snapshot().memories == ()


def test_promotion_refuses_a_stage_another_session_built(data_dir):
    # A crashed earlier session's stage replayed against a later save must
    # never land in that foreign map's directory.
    store = MemoryStore(data_dir)
    store.use_mapping_session(1234.5)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-map-content")
    with pytest.raises(StaleStageError):
        store.promote_mapping_session("tour.yaml", mapping_started=9999.0)
    store.switch_map("tour.yaml")
    assert store.snapshot().memories == ()


def test_promotion_lands_its_own_sessions_stage(data_dir):
    store = MemoryStore(data_dir)
    store.use_mapping_session(1234.5)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-one")
    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-map-content")
    assert store.promote_mapping_session("tour.yaml", mapping_started=1234.5) == 1
    store.switch_map("tour.yaml")
    assert len(store.snapshot().memories) == 1


def test_a_transiently_unreadable_map_file_never_wipes(data_dir):
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(1.0, 2.0, 0.5, 1000.0, GOOD_JPEG)
    (data_dir / "maps" / "A.pgm").unlink()  # a rewrite window or IO hiccup, not a re-map
    store.switch_map("A.yaml")
    (data_dir / "maps" / "A.pgm").write_bytes(b"map-A-content")
    store.switch_map("A.yaml")
    (memory,) = store.snapshot().memories
    path = store.image_path(memory.id)
    assert path is not None and path.read_bytes() == GOOD_JPEG


def test_corrupt_index_resets_cleanly(data_dir):
    memory_dir = data_dir / "spatial_memory" / "A"
    memory_dir.mkdir(parents=True)
    (memory_dir / "index.json").write_text("{not json")
    (memory_dir / "7.jpg").write_bytes(b"orphan")
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    assert store.snapshot().memories == ()
    assert not (memory_dir / "7.jpg").exists()  # orphaned images have no poses; they are wiped with the index


# ================= recorder =================


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(recorder_module.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(recorder_module.time, "time", lambda: state.now)  # stamps drive the refresh age gate
    return state


def make_recorder(
    data_dir,
    published: list | None = None,
    pose: SimpleNamespace | None = None,
    cache: SimpleNamespace | None = None,
):
    logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    node = SimpleNamespace(
        get_logger=lambda: logger,
        create_subscription=lambda *a, **k: None,
        create_timer=lambda *a, **k: None,
    )
    config = SimpleNamespace(
        image_topic="/img",
        current_nav_mode_topic="/nav/current_mode",
        current_map_topic="/nav/current_map",
        amcl_pose_topic="/amcl_pose",
        map_saved_topic="/nav/map_saved",
        mapping_session_topic="/nav/mapping_session",
    )
    pose = pose if pose is not None else SimpleNamespace(xyt=(1.0, 2.0, 0.5))
    cache = cache if cache is not None else SimpleNamespace(state="warm")
    store = MemoryStore(data_dir)
    recorder = MemoryRecorder(
        node,
        config,
        store=store,
        pose_tracker=SimpleNamespace(map_pose_xyt=lambda: pose.xyt),
        warm_search=None,
        cache_state=lambda: cache.state,
        positions_pub=SimpleNamespace(publish=(published if published is not None else []).append),
    )
    return recorder, store


def see_confident_world(recorder, clock):
    """Feed the recorder everything a recordable moment needs."""
    recorder._on_current_map(SimpleNamespace(data="A.yaml"))
    recorder._on_nav_mode(SimpleNamespace(data="navigation"))
    recorder._on_amcl_pose(SimpleNamespace(pose=SimpleNamespace(covariance=_covariance(0.02, 0.02, 0.05))))
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder._on_head(SimpleNamespace(data=json.dumps({"current_position": -10.0})))


def observe(recorder, clock, jpeg: bytes, advance: float = 1.0):
    """One recorder tick seeing this frame, the clock advanced past the last.
    Re-feeds AMCL like the live one does (update_min_* = 0: it publishes every
    scan), so covariance freshness holds across any advance."""
    clock.now += advance
    recorder._on_amcl_pose(SimpleNamespace(pose=SimpleNamespace(covariance=_covariance(0.02, 0.02, 0.05))))
    recorder._on_image(SimpleNamespace(data=jpeg))
    recorder.tick()


def stored_image(store: MemoryStore, memory_id: int) -> bytes:
    path = store.image_path(memory_id)
    assert path is not None
    return path.read_bytes()


def _covariance(var_x: float, var_y: float, var_yaw: float) -> list[float]:
    covariance = [0.0] * 36
    covariance[0], covariance[7], covariance[35] = var_x, var_y, var_yaw
    return covariance


def test_confidence_must_hold_before_recording(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder.tick()  # starts the confidence clock
    assert store.snapshot().memories == ()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder.tick()
    assert len(store.snapshot().memories) == 1


def test_a_covariance_spike_resets_the_clock(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 2.0
    recorder._on_amcl_pose(SimpleNamespace(pose=SimpleNamespace(covariance=_covariance(0.9, 0.9, 0.5))))
    recorder.tick()  # lost: the clock resets
    recorder._on_amcl_pose(SimpleNamespace(pose=SimpleNamespace(covariance=_covariance(0.02, 0.02, 0.05))))
    recorder.tick()  # confident again: the clock restarts here, 2s in
    clock.now += 2.9
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder.tick()
    assert store.snapshot().memories == ()
    clock.now += 0.3
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder.tick()
    assert len(store.snapshot().memories) == 1


def test_a_silent_amcl_stops_recording(data_dir, clock):
    # AMCL publishes every scan while alive (update_min_* = 0). When it dies
    # mid-drive, TF keeps composing odom motion under the latched confident
    # covariance — those unvouched poses must not become memories.
    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.5))
    recorder, store = settled_recorder(data_dir, clock, pose)
    pose.xyt = (4.0, 2.0, 0.5)  # still "driving" — but AMCL has gone quiet
    clock.now += 6.0
    recorder._on_image(SimpleNamespace(data=frame(2)))
    recorder.tick()
    assert len(store.snapshot().memories) == 1  # only the pre-death viewpoint


def test_mapfree_never_records(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder._on_nav_mode(SimpleNamespace(data="mapfree"))
    for _ in range(5):
        recorder.tick()
        clock.now += 2.0
        recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    assert store.snapshot().memories == ()


def test_looking_at_the_floor_blocks_capture(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder._on_head(SimpleNamespace(data=json.dumps({"current_position": -45.0})))
    recorder.tick()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder.tick()
    assert store.snapshot().memories == ()


def test_a_stale_frame_blocks_capture(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 3.1  # the frame from see_confident_world is now 3.1s old
    recorder.tick()
    assert store.snapshot().memories == ()


def test_positions_mirror_publishes_map_poses_and_cache_state(data_dir, clock):
    published: list = []
    pose = SimpleNamespace(xyt=(1.23456789, -2.98765432, 0.87654321))
    recorder, store = make_recorder(data_dir, published, pose=pose)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder.tick()
    payload = json.loads(published[-1].data)
    assert payload["map"] == "A.yaml" and payload["cache"] == "warm"
    (position,) = payload["positions"]
    assert position["id"] == 1
    # Display precision, not the store's: full floats bloat the JSON by half.
    assert position["x"] == 1.235 and position["y"] == -2.988 and position["theta"] == 0.8765
    assert position["stamp"] == round(clock.now, 1)


def test_positions_mirror_republishes_only_on_change(data_dir, clock):
    published: list = []
    cache = SimpleNamespace(state="warm")
    recorder, store = make_recorder(data_dir, published, cache=cache)
    see_confident_world(recorder, clock)
    recorder.tick()
    quiet = len(published)
    for _ in range(3):  # nothing changes — the latch alone serves late joiners
        clock.now += 1.0
        recorder._on_amcl_pose(SimpleNamespace(pose=SimpleNamespace(covariance=_covariance(0.02, 0.02, 0.05))))
        recorder.tick()
    assert len(published) == quiet
    observe(recorder, clock, GOOD_JPEG)  # a recorded viewpoint changes the payload
    assert len(published) == quiet + 1
    cache.state = "cold"  # flips by wall clock, without a store change — the gate must see it
    clock.now += 1.0
    recorder.tick()
    assert len(published) == quiet + 2 and json.loads(published[-1].data)["cache"] == "cold"


def settled_recorder(data_dir, clock, pose: SimpleNamespace):
    """A recorder past the confidence hold, its first viewpoint just recorded with frame(1)."""
    recorder, store = make_recorder(data_dir, pose=pose)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 3.1
    observe(recorder, clock, frame(1), advance=0.0)
    assert len(store.snapshot().memories) == 1
    return recorder, store


def test_the_sharpest_frame_of_the_window_wins_with_its_own_pose(data_dir, clock):
    # A quick turn: the tick records the crispest instant of the last second,
    # at the heading it was seen from — not whatever the feed holds at tick time.
    rng = np.random.default_rng(11)
    noise = rng.integers(0, 255, size=(240, 320), dtype=np.uint8)
    sharp, soft = encoded(noise), encoded(cv2.GaussianBlur(noise, (5, 5), 0))
    scores = (frame_sharpness(soft), frame_sharpness(sharp))
    assert scores[0] is not None and scores[1] is not None and MIN_SHARPNESS <= scores[0] < scores[1]

    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.0))
    recorder, store = make_recorder(data_dir, pose=pose)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=soft))
    pose.xyt = (1.0, 2.0, 2.5)
    recorder._on_image(SimpleNamespace(data=sharp))
    pose.xyt = (1.0, 2.0, 3.0)
    recorder._on_image(SimpleNamespace(data=DARK_JPEG))  # what the feed holds at tick time
    recorder.tick()
    (m,) = store.snapshot().memories
    assert m.theta == 2.5 and stored_image(store, m.id) == sharp


def test_a_mid_turn_blur_never_becomes_the_candidate(data_dir, clock):
    rng = np.random.default_rng(12)
    blurred = encoded(cv2.GaussianBlur(rng.integers(0, 255, size=(240, 320), dtype=np.uint8), (31, 31), 0))
    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.0))
    recorder, store = make_recorder(data_dir, pose=pose)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=blurred))  # sweeping past this heading
    pose.xyt = (1.0, 2.0, 2.0)
    recorder._on_image(SimpleNamespace(data=frame(4)))  # the pause at the end of the turn
    recorder.tick()
    (m,) = store.snapshot().memories
    assert m.theta == 2.0 and stored_image(store, m.id) == frame(4)


def test_a_new_viewpoint_needs_a_keepable_frame(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 3.1
    observe(recorder, clock, DARK_JPEG, advance=0.0)
    assert store.snapshot().memories == ()
    observe(recorder, clock, frame(1))
    assert len(store.snapshot().memories) == 1


def test_dwelling_rewrites_nothing_within_the_refresh_window(data_dir, clock):
    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.5))
    recorder, store = settled_recorder(data_dir, clock, pose)
    observe(recorder, clock, frame(2))
    observe(recorder, clock, frame(3))
    assert stored_image(store, 1) == frame(1)


def test_an_aged_same_view_refreshes_with_the_current_frame(data_dir, clock):
    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.5))
    recorder, store = settled_recorder(data_dir, clock, pose)
    observe(recorder, clock, frame(2), advance=61.0)  # staring at the same spot a minute later
    assert stored_image(store, 1) == frame(2)
    assert len(store.snapshot().memories) == 1


def test_a_turned_away_frame_never_overwrites(data_dir, clock):
    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.5))
    recorder, store = settled_recorder(data_dir, clock, pose)
    pose.xyt = (1.0, 2.0, 0.5 + math.radians(40))  # mid-turn: redundant, but a different picture
    observe(recorder, clock, frame(2), advance=61.0)
    assert stored_image(store, 1) == frame(1)
    assert len(store.snapshot().memories) == 1


def grid_msg(cells: np.ndarray, resolution: float = 0.1):
    identity = SimpleNamespace(w=1.0, x=0.0, y=0.0, z=0.0)
    origin = SimpleNamespace(position=SimpleNamespace(x=0.0, y=0.0), orientation=identity)
    info = SimpleNamespace(resolution=resolution, width=cells.shape[1], height=cells.shape[0], origin=origin)
    return SimpleNamespace(info=info, data=cells.flatten().tolist())


def test_the_grid_widens_the_refresh_to_clear_sight_lines(data_dir, clock):
    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.0))
    recorder, store = settled_recorder(data_dir, clock, pose)
    recorder._on_map(grid_msg(open_room(40)))
    pose.xyt = (1.8, 2.0, 0.0)  # a step along the aim, nothing in between
    observe(recorder, clock, frame(2), advance=61.0)
    assert stored_image(store, 1) == frame(2)
    assert len(store.snapshot().memories) == 1


def test_a_wall_on_the_sight_line_blocks_the_refresh(data_dir, clock):
    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.0))
    recorder, store = settled_recorder(data_dir, clock, pose)
    cells = open_room(40)
    cells[:, 14] = 100  # a wall at world x = 1.4-1.5
    recorder._on_map(grid_msg(cells))
    pose.xyt = (1.8, 2.0, 0.0)
    observe(recorder, clock, frame(2), advance=61.0)
    assert stored_image(store, 1) == frame(1)  # the old view survives behind the wall...
    (kept, minted) = store.snapshot().memories
    assert stored_image(store, minted.id) == frame(2)  # ...and the unseen floor ahead earns its own slot


def test_a_quarter_turn_with_a_grid_mints_a_second_viewpoint(data_dir, clock):
    pose = SimpleNamespace(xyt=(2.0, 2.0, 0.0))
    recorder, store = settled_recorder(data_dir, clock, pose)
    recorder._on_map(grid_msg(open_room(40)))
    pose.xyt = (2.0, 2.0, math.pi / 2)  # the old 100-degree rule would skip this turn
    observe(recorder, clock, frame(2))
    memories = store.snapshot().memories
    assert len(memories) == 2 and memories[1].theta == math.pi / 2


def test_bad_frames_never_overwrite_a_view(data_dir, clock):
    pose = SimpleNamespace(xyt=(1.0, 2.0, 0.5))
    recorder, store = settled_recorder(data_dir, clock, pose)
    observe(recorder, clock, DARK_JPEG, advance=61.0)  # refresh is due, but the lights are off
    assert stored_image(store, 1) == frame(1)
    observe(recorder, clock, frame(2))  # the first keepable frame lands it
    assert stored_image(store, 1) == frame(2)


# ================= recording while mapping =================


def see_mapping_world(recorder, started: float = 1000.0, mode: str = "mapping"):
    """Feed the recorder everything a mapping-mode record needs: SLAM's live
    grid stands in for AMCL confidence, mode_manager's latched session stamp
    names the frame."""
    recorder._on_nav_mode(SimpleNamespace(data=mode))
    recorder._on_mapping_session(SimpleNamespace(data=json.dumps({"started": started})))
    recorder._on_map(grid_msg(open_room(40)))
    recorder._on_head(SimpleNamespace(data=json.dumps({"current_position": -10.0})))
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))


def mapping_observe(recorder, clock, jpeg: bytes, advance: float = 1.0):
    """One mapping tick seeing this frame; re-feeds the grid like the live
    slam_toolbox does (map_update_interval 0.5 s)."""
    clock.now += advance
    recorder._on_map(grid_msg(open_room(40)))
    recorder._on_image(SimpleNamespace(data=jpeg))
    recorder.tick()


def announce_save(recorder, clock, name: str, age: float = 0.0, mapping_started: float | None = 1000.0):
    """mode_manager's /nav/map_saved payload, stamped ``age`` seconds ago;
    ``mapping_started`` defaults to see_mapping_world's session, None mimics a
    legacy announcement."""
    payload = {"map": name, "stamp": clock.now - age}
    if mapping_started is not None:
        payload["mapping_started"] = mapping_started
    recorder._on_map_saved(SimpleNamespace(data=json.dumps(payload)))


def test_mapping_records_into_the_session_stage(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()  # starts the hold
    assert store.snapshot().memories == ()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    snapshot = store.snapshot()
    assert snapshot.map_name == MAPPING_SESSION and len(snapshot.memories) == 1


def test_autonomous_mapping_records_into_the_same_session_stage(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder, mode="autonomous_mapping")
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    snapshot = store.snapshot()
    assert snapshot.map_name == MAPPING_SESSION and len(snapshot.memories) == 1


def test_a_silent_slam_stops_recording(data_dir, clock):
    # slam_toolbox republishes /map twice a second while alive. When it dies
    # mid-tour, TF keeps composing odom motion in the map frame — those
    # unvouched poses must not become memories.
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1
    clock.now += 6.0  # the grid has gone stale
    recorder._on_image(SimpleNamespace(data=frame(2)))
    recorder.tick()
    assert len(store.snapshot().memories) == 1


def test_a_saved_map_adopts_the_mapping_memories(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1

    # The webapp's save sequence: save_map, then navigation on the new map.
    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-map-content")
    announce_save(recorder, clock, "tour.yaml")
    recorder._on_nav_mode(SimpleNamespace(data="navigation"))
    recorder._on_current_map(SimpleNamespace(data="tour.yaml"))
    recorder.tick()
    snapshot = store.snapshot()
    assert snapshot.map_name == "tour.yaml" and len(snapshot.memories) == 1
    assert stored_image(store, snapshot.memories[0].id) == GOOD_JPEG


def test_a_promotion_landing_after_the_mode_switch_still_adopts(data_dir, clock):
    # /nav/map_saved and /nav/current_mode arrive on independent topics: the
    # tick may switch the store onto the just-saved map (finding it empty)
    # before the save announcement is handled. The tour must survive the race.
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1

    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-map-content")
    recorder._on_nav_mode(SimpleNamespace(data="navigation"))
    recorder._on_current_map(SimpleNamespace(data="tour.yaml"))
    recorder.tick()  # the switch wins the race: empty store on the new map
    assert store.snapshot().memories == ()
    announce_save(recorder, clock, "tour.yaml")  # save lands late
    snapshot = store.snapshot()
    assert snapshot.map_name == "tour.yaml" and len(snapshot.memories) == 1
    assert stored_image(store, snapshot.memories[0].id) == GOOD_JPEG


def test_a_failing_promotion_never_escapes_the_callback(data_dir, clock):
    # brain_client_node's spin loop re-raises what escapes a callback: a full
    # disk mid-promotion must log, not kill the node.
    recorder, store = make_recorder(data_dir)

    def full_disk(_name, _started):
        raise OSError("disk full")

    store.promote_mapping_session = full_disk
    announce_save(recorder, clock, "tour.yaml")


def test_an_unreadable_save_announcement_never_escapes_the_callback(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    recorder._on_map_saved(SimpleNamespace(data="tour.yaml"))  # pre-stamp wire format
    assert not (data_dir / "spatial_memory" / "tour").exists()  # nothing was promoted


def test_a_stale_save_replay_never_promotes(data_dir, clock):
    # /nav/map_saved is latched so a recorder respawning across the save still
    # hears it — but the same latch replays *old* saves to every restart, and
    # adopting the current stage into a map saved from an earlier SLAM session
    # would stamp this tour's coordinates onto a foreign frame.
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1

    (data_dir / "maps" / "old.pgm").write_bytes(b"old-map-content")
    announce_save(recorder, clock, "old.yaml", age=300.0)
    assert (data_dir / "spatial_memory" / MAPPING_SESSION / "1.jpg").exists()  # the stage is untouched
    store.switch_map("old.yaml")
    assert store.snapshot().memories == ()


def test_an_abandoned_session_is_wiped_when_the_next_begins(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1

    recorder._on_nav_mode(SimpleNamespace(data="navigation"))  # left without saving
    recorder._on_current_map(SimpleNamespace(data="A.yaml"))
    recorder.tick()
    see_mapping_world(recorder, started=2000.0)  # re-entry restarts slam: a new session stamp
    recorder.tick()
    snapshot = store.snapshot()
    assert snapshot.map_name == MAPPING_SESSION and snapshot.memories == ()
    assert list((data_dir / "spatial_memory" / MAPPING_SESSION).glob("*.jpg")) == []


def test_mapping_positions_mirror_carries_the_session_identity(data_dir, clock):
    published: list = []
    recorder, store = make_recorder(data_dir, published)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    payload = json.loads(published[-1].data)
    assert payload["map"] == MAPPING_SESSION and len(payload["fingerprint"]) == 12
    assert len(payload["positions"]) == 1


def test_a_mode_change_restarts_the_confidence_hold(data_dir, clock):
    # A hold accrued in navigation is evidence about the OLD frame; carrying it
    # across the switch would let the first mapping tick record instantly.
    recorder, store = make_recorder(data_dir)
    see_confident_world(recorder, clock)
    recorder.tick()
    clock.now += 5.0  # the navigation hold is long since satisfied
    see_mapping_world(recorder)
    recorder.tick()  # first mapping tick: the hold restarts
    assert store.snapshot().memories == ()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1


def test_a_latched_old_map_grid_never_vouches_for_slam(data_dir, clock):
    # /map is TRANSIENT_LOCAL: entering mapping can replay the previous map's
    # grid, which must not stand in for a slam_toolbox that hasn't spoken yet.
    recorder, store = make_recorder(data_dir)
    recorder._on_nav_mode(SimpleNamespace(data="navigation"))
    recorder._on_map(grid_msg(open_room(40)))  # the old map, latched
    recorder._on_nav_mode(SimpleNamespace(data="mapping"))
    recorder._on_mapping_session(SimpleNamespace(data=json.dumps({"started": 1000.0})))
    recorder._on_head(SimpleNamespace(data=json.dumps({"current_position": -10.0})))
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder.tick()
    clock.now += 3.1
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder.tick()  # would record by now if the stale grid still counted
    assert store.snapshot().memories == ()

    recorder._on_map(grid_msg(open_room(40)))  # slam's first grid: the hold starts here
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1


def test_a_respawned_recorder_adopts_its_sessions_stage(data_dir, clock):
    # A brain-only restart mid-tour: the mode arrives before the latched
    # session replay, and the recorder must not touch the stage until it
    # knows whose it is — then the same stamp adopts the half-tour.
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1

    reborn, reborn_store = make_recorder(data_dir)
    reborn._on_nav_mode(SimpleNamespace(data="mapping"))
    reborn.tick()  # session unknown: no wipe, no record
    assert (data_dir / "spatial_memory" / MAPPING_SESSION / "1.jpg").exists()
    assert reborn_store.snapshot().map_name is None
    reborn._on_mapping_session(SimpleNamespace(data=json.dumps({"started": 1000.0})))
    reborn.tick()
    snapshot = reborn_store.snapshot()
    assert snapshot.map_name == MAPPING_SESSION and len(snapshot.memories) == 1


def test_mapping_without_a_session_identity_never_records(data_dir, clock):
    recorder, store = make_recorder(data_dir)
    recorder._on_nav_mode(SimpleNamespace(data="mapping"))
    recorder._on_map(grid_msg(open_room(40)))
    recorder._on_head(SimpleNamespace(data=json.dumps({"current_position": -10.0})))
    recorder._on_image(SimpleNamespace(data=GOOD_JPEG))
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    snapshot = store.snapshot()
    assert snapshot.map_name is None and snapshot.memories == ()


def test_a_save_from_another_session_never_gets_the_stage(data_dir, clock):
    # A stage left by a crashed earlier session must not ride a later
    # session's save into that foreign map.
    recorder, store = make_recorder(data_dir)
    see_mapping_world(recorder)
    recorder.tick()
    mapping_observe(recorder, clock, GOOD_JPEG, advance=3.1)
    assert len(store.snapshot().memories) == 1

    (data_dir / "maps" / "tour.pgm").write_bytes(b"tour-map-content")
    announce_save(recorder, clock, "tour.yaml", mapping_started=9999.0)
    assert (data_dir / "spatial_memory" / MAPPING_SESSION / "1.jpg").exists()  # the stage is kept
    store.switch_map("tour.yaml")
    assert store.snapshot().memories == ()


# ================= memory search =================


def gemini_json(payload: dict) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(payload)}]}}]}


class FakeGemini:
    """A scriptable GeminiRest: records every call, answers by path."""

    def __init__(self, verdict: dict | None = None):
        self.creates: list[dict] = []
        self.generates: list[dict] = []
        self.deletes: list[str] = []
        self.uploads: list[bytes] = []
        self.create_error: GeminiHttpError | None = None
        self.cached_generate_error: GeminiHttpError | None = None
        self.upload_error: GeminiHttpError | None = None
        self.upload_gate: threading.Event | None = None  # when set, uploads block on it
        self.verdict = verdict if verdict is not None else {"found": True, "frame": 1, "explanation": "matches"}
        self.rest = GeminiRest(post=self._post, delete=self._delete, upload=self._upload)

    def _post(self, path: str, body: dict) -> dict:
        if path == CACHED_CONTENTS_PATH:
            self.creates.append(body)
            if self.create_error is not None:
                raise self.create_error
            return {"name": f"cachedContents/c{len(self.creates)}"}
        self.generates.append(body)
        if "cachedContent" in body and self.cached_generate_error is not None:
            raise self.cached_generate_error
        return gemini_json(self.verdict)

    def _delete(self, path: str) -> dict:
        self.deletes.append(path)
        return {}

    def _upload(self, path: str, data: bytes, mime: str) -> dict:
        if self.upload_gate is not None:
            self.upload_gate.wait(timeout=5)
        if self.upload_error is not None:
            raise self.upload_error
        self.uploads.append(data)
        return {"file": {"name": f"files/f{len(self.uploads)}", "uri": f"https://files/f{len(self.uploads)}"}}


def make_search(data_dir, frames: int, verdict: dict | None = None) -> tuple[MemorySearch, FakeGemini, MemoryStore]:
    (data_dir / "maps").mkdir(parents=True, exist_ok=True)
    map_file = data_dir / "maps" / "A.pgm"
    if not map_file.exists():
        map_file.write_bytes(b"map-A-content")  # the store refuses a map it cannot fingerprint
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    for i in range(frames):
        store.add(float(3 * i), 0.0, 0.0, 1000.0 + i, f"jpg-{i + 1}".encode())
    fake = FakeGemini(verdict)
    logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    return MemorySearch(store, fake.rest, model="test-model", logger=logger), fake, store


def build_cache(search: MemorySearch) -> None:
    """Synchronous stand-in for warm()'s background cache rebuild."""
    snapshot = search._store.snapshot()
    if search._should_warm(snapshot):
        search._create_cache(snapshot)


def upload_frames(search: MemorySearch) -> None:
    """Synchronous stand-in for maintain()'s background upload round."""
    pending, scanned_dir = search._files._pending()
    if scanned_dir is not None:
        search._files._upload(pending, scanned_dir)


def part_kinds(body: dict) -> list[str]:
    return [next(iter(part)) for part in body["contents"][0]["parts"]]


def memory_with_id(store: MemoryStore, memory_id: int):
    (memory,) = [m for m in store.snapshot().memories if m.id == memory_id]
    return memory


def test_search_rides_a_fresh_cache_with_only_the_question(data_dir):
    search, fake, _ = make_search(data_dir, frames=6, verdict={"found": True, "frame": 2, "explanation": "the kitchen"})
    build_cache(search)
    verdict = search.search("the kitchen")
    search.search("the kitchen again")
    assert len(fake.creates) == 1
    assert [body.get("cachedContent") for body in fake.generates] == ["cachedContents/c1", "cachedContents/c1"]
    assert part_kinds(fake.generates[0]) == ["text"]  # fresh cache: no frame bytes at all
    assert verdict.found and verdict.cached and verdict.image == b"jpg-2"
    assert verdict.memory is not None and verdict.memory.x == 3.0
    assert "x=3.00m" in verdict_text(verdict) and "navigate_to_position" in verdict_text(verdict)


def test_a_search_never_builds_the_cache(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    search.search("anything")
    assert fake.creates == []  # a cold search answers from frames; rebuilds belong to warm()


def test_a_stale_cache_serves_with_a_delta_of_new_frames(data_dir):
    search, fake, store = make_search(data_dir, frames=6, verdict={"found": True, "frame": 7, "explanation": "new"})
    build_cache(search)
    store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")  # id 7
    verdict = search.search("the new spot")
    assert len(fake.creates) == 1  # the search path never rebuilds
    body = fake.generates[-1]
    assert body["cachedContent"] == "cachedContents/c1"
    assert part_kinds(body) == ["inlineData", "text", "text"]  # the new frame, its label, the question
    assert "Frame 7" in body["contents"][0]["parts"][1]["text"]
    assert verdict.found and verdict.cached
    assert verdict.memory is not None and verdict.memory.id == 7 and verdict.image == b"jpg-late"


def test_delta_notes_supersede_and_retire_frames(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    build_cache(search)
    store.replace(memory_with_id(store, 2), 3.0, 0.0, 0.0, 5000.0, b"jpg-2-new")
    store.evict(memory_with_id(store, 3))
    search.search("anything")
    parts = fake.generates[-1]["contents"][0]["parts"]
    texts = [part["text"] for part in parts if "text" in part]
    assert any("Frame 2 was re-captured" in text for text in texts)
    assert any("Frame 3 no longer exists" in text for text in texts)
    assert sum("inlineData" in part for part in parts) == 1  # only the re-captured frame's bytes travel


def test_warm_rebuilds_a_stale_cache_and_deletes_the_old(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    build_cache(search)
    store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")
    build_cache(search)
    assert len(fake.creates) == 2
    assert fake.deletes == ["/v1beta/cachedContents/c1"]


def test_few_frames_answer_from_frames_without_caching(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)
    verdict = search.search("anything")
    assert fake.creates == []
    (body,) = fake.generates
    assert "systemInstruction" in body and "cachedContent" not in body
    assert sum("inlineData" in part for part in body["contents"][0]["parts"]) == 3
    assert verdict.image == b"jpg-1" and not verdict.cached


def test_unsupported_backend_disables_caching_permanently(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    fake.create_error = GeminiHttpError(404, "no such endpoint")
    build_cache(search)
    build_cache(search)
    assert len(fake.creates) == 1  # latched after the first answer
    verdict = search.search("first")
    assert "cachedContent" not in fake.generates[-1]
    assert verdict.image == b"jpg-1"


def test_failed_cache_creation_retries_only_after_the_memory_changes(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    fake.create_error = GeminiHttpError(400, "cache too small")
    build_cache(search)
    build_cache(search)
    assert len(fake.creates) == 1
    fake.create_error = None
    store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")
    build_cache(search)
    assert len(fake.creates) == 2


def test_a_transient_cache_failure_backs_off_instead_of_latching(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    fake.create_error = GeminiHttpError(503, "overloaded")
    build_cache(search)
    build_cache(search)
    assert len(fake.creates) == 1  # backed off — warm() rides a 1 Hz tick
    assert search._failed_revision is None  # a 5xx is the backend's fault, not this content's
    fake.create_error = None
    search._retry_at = 0.0  # the backoff window elapses
    build_cache(search)
    assert len(fake.creates) == 2  # same content retries once the backend recovers


def test_a_network_error_during_warm_backs_off_instead_of_raising(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)

    def explode(path: str, body: dict) -> dict:
        raise RuntimeError("connection reset")

    search._rest = GeminiRest(post=explode, delete=lambda path: {}, upload=lambda path, data, mime: {})
    build_cache(search)
    assert search._retry_at > time.monotonic() and search._failed_revision is None


def test_a_busy_search_returns_a_typed_verdict_instead_of_queuing(data_dir, monkeypatch):
    search, fake, _ = make_search(data_dir, frames=3)
    monkeypatch.setattr(memory_search_module, "_BUSY_WAIT_SEC", 0.05)
    search._flight.acquire()  # an abandoned goal (cancels are rejected) still holds the lock
    try:
        verdict = search.search("anything")
    finally:
        search._flight.release()
    assert not verdict.found and "still running" in verdict.error
    assert fake.generates == []  # never reached the network


def test_forget_frame_purges_its_upload_and_the_cache_embedding_it(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    upload_frames(search)
    build_cache(search)
    memory = memory_with_id(store, 2)
    assert store.forget(memory.id) == memory
    search.forget_frame(memory)
    assert search._cache is None
    assert 2 not in search._files._records and len(search._files._records) == 5
    deadline = time.time() + 2.0
    while len(fake.deletes) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert "/v1beta/cachedContents/c1" in fake.deletes
    assert "/v1beta/files/f2" in fake.deletes
    assert len(fake.deletes) == 2  # the other five uploads survive for the next rebuild


def test_forget_frame_keeps_a_cache_that_never_embedded_it(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    build_cache(search)
    added = store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")  # id 7, after the build
    assert added is not None and store.forget(added.id) == added
    search.forget_frame(added)
    assert search._cache is not None and fake.deletes == []


def test_forget_drops_the_cache_and_purges_the_uploads(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    upload_frames(search)
    build_cache(search)
    assert search._cache is not None
    store.clear()
    search.forget()
    assert search._cache is None and search._files._records == {}
    deadline = time.time() + 2.0
    while len(fake.deletes) < 7 and time.time() < deadline:
        time.sleep(0.01)
    assert "/v1beta/cachedContents/c1" in fake.deletes
    assert sum(1 for path in fake.deletes if path.startswith("/v1beta/files/")) == 6


def test_a_dead_cache_falls_back_and_is_forgotten(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    build_cache(search)
    fake.cached_generate_error = GeminiHttpError(403, "cache expired")
    verdict = search.search("second")
    assert verdict.image == b"jpg-1" and not verdict.cached
    assert "cachedContent" not in fake.generates[-1]  # answered from frames within the same search
    fake.cached_generate_error = None
    build_cache(search)
    assert len(fake.creates) == 2  # the dead handle was dropped; warm rebuilt


def test_no_match_is_a_clean_verdict_not_an_error(data_dir):
    search, fake, _ = make_search(data_dir, frames=6, verdict={"found": False, "frame": 0, "explanation": "no kitchen"})
    verdict = search.search("the kitchen")
    assert not verdict.found and verdict.image is None and verdict.error == ""
    assert "nothing in the remembered views matches" in verdict_text(verdict)


def test_unreadable_answer_becomes_an_error_verdict(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)
    fake.verdict = None  # type: ignore[assignment] — json.dumps(None) is valid JSON but not a verdict
    verdict = search.search("anything")
    assert verdict.error == "unreadable answer" and verdict.image is None
    assert "failed" in verdict_text(verdict)


def test_a_transport_crash_becomes_an_error_verdict_not_an_exception(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)

    def explode(path: str, body: dict) -> dict:
        raise RuntimeError("network down")

    search._rest = GeminiRest(post=explode, delete=lambda path: {}, upload=lambda path, data, mime: {})
    verdict = search.search("anything")
    assert not verdict.found and "network down" in verdict.error


def test_empty_memory_answers_without_the_network(data_dir):
    search, fake, _ = make_search(data_dir, frames=0)
    verdict = search.search("anything")
    assert fake.generates == [] and verdict.image is None and not verdict.found
    assert "no memories" in verdict_text(verdict)


def test_warm_builds_the_cache_in_the_background(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    store.last_change_monotonic = time.monotonic() - 60.0
    search.warm()
    deadline = time.time() + 2.0
    while search._cache is None and time.time() < deadline:
        time.sleep(0.01)
    assert search._cache is not None and len(fake.creates) == 1
    assert fake.generates == []  # warming never asks a question


def test_warm_waits_for_recording_to_settle(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    store.last_change_monotonic = time.monotonic()  # just changed
    search.warm()
    time.sleep(0.05)
    assert fake.creates == []


def test_warm_rebuilds_mid_burst_once_the_delta_grows_too_big(data_dir):
    # The quiet window must not let the stale-cache delta grow without bound
    # during a long recording tour — past the threshold, rebuild anyway.
    search, fake, store = make_search(data_dir, frames=6)
    build_cache(search)
    for i in range(10):
        store.add(40.0 + i, 0.0, 0.0, 3000.0 + i, b"jpg-burst")
    assert time.monotonic() - store.last_change_monotonic < 1.0  # mid-burst
    search.warm()
    deadline = time.time() + 2.0
    while len(fake.creates) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert len(fake.creates) == 2


def test_search_mirrors_verdicts_to_the_ui(data_dir):
    search, fake, _ = make_search(data_dir, frames=6, verdict={"found": True, "frame": 2, "explanation": "kitchen"})
    build_cache(search)
    reports: list[dict] = []
    search.on_result = reports.append
    search.search("the kitchen")
    (report,) = reports
    assert report["found"] and report["id"] == 2 and report["x"] == 3.0 and report["cached"]
    assert report["query"] == "the kitchen" and report["explanation"] == "kitchen"
    assert report["latency_sec"] >= 0 and report["stamp"] > 0 and report["seen_stamp"] == 1001.0

    fake.verdict = {"found": False, "frame": 0, "explanation": "no such view"}
    search.search("a unicorn")
    assert reports[-1] == {
        "query": "a unicorn",
        "found": False,
        "explanation": "no such view",
        "latency_sec": reports[-1]["latency_sec"],
        "cached": True,
        "stamp": reports[-1]["stamp"],
    }


def test_a_broken_ui_mirror_does_not_break_the_search(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)

    def explode(payload: dict) -> None:
        raise RuntimeError("publisher gone")

    search.on_result = explode
    verdict = search.search("anything")
    assert verdict.image == b"jpg-1"  # the search itself still succeeded


def test_cache_state_reflects_the_lifecycle(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    assert search.cache_state() == "cold"
    build_cache(search)
    assert search.cache_state() == "warm"
    store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")
    assert search.cache_state() == "warm"  # stale-but-usable: the delta keeps recall instant

    small, _, _ = make_search(data_dir / "small", frames=0)
    assert small.cache_state() == "inline"

    unsupported, fake2, _ = make_search(data_dir / "unsup", frames=6)
    fake2.create_error = GeminiHttpError(404, "no endpoint")
    build_cache(unsupported)
    assert unsupported.cache_state() == "unsupported"


# ================= files tier =================


def test_frames_upload_once_and_ride_as_file_references(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    upload_frames(search)
    assert len(fake.uploads) == 6
    upload_frames(search)
    assert len(fake.uploads) == 6  # nothing left to do
    search.search("anything")
    parts = fake.generates[-1]["contents"][0]["parts"]
    file_parts = [part for part in parts if "fileData" in part]
    assert len(file_parts) == 6 and not any("inlineData" in part for part in parts)
    assert file_parts[0]["fileData"]["fileUri"].startswith("https://files/")


def test_the_upload_registry_survives_a_restart(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    upload_frames(search)
    reborn, fake2, _ = make_search(data_dir, frames=0)  # same map dir, fresh process
    upload_frames(reborn)
    assert fake2.uploads == []  # every frame is already server-side


def test_an_aging_upload_is_refreshed_before_server_expiry(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    upload_frames(search)
    files = search._files
    with files._lock:
        files._records = {i: replace(r, uploaded_at=r.uploaded_at - 41 * 3600) for i, r in files._records.items()}
    upload_frames(search)
    assert len(fake.uploads) == 12


def test_an_expired_reference_rides_inline_instead(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)
    upload_frames(search)
    files = search._files
    with files._lock:
        files._records = {i: replace(r, uploaded_at=r.uploaded_at - 47.5 * 3600) for i, r in files._records.items()}
    search.search("anything")
    parts = fake.generates[-1]["contents"][0]["parts"]
    assert not any("fileData" in part for part in parts)  # too old to trust server-side
    assert sum("inlineData" in part for part in parts) == 3


def test_a_refreshed_frame_invalidates_its_upload(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    upload_frames(search)
    store.replace(memory_with_id(store, 2), 3.0, 0.0, 0.0, 5000.0, b"jpg-2-new")
    upload_frames(search)
    assert len(fake.uploads) == 7 and fake.uploads[-1] == b"jpg-2-new"
    # The superseded upload is deleted, not left to Gemini's 48 h expiry — at
    # the 10 s refresh cadence a dwelled-on viewpoint would pile files up fast.
    assert any(path.endswith("/files/f2") for path in fake.deletes)


def test_upload_latch_on_unsupported_backend(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    fake.upload_error = GeminiHttpError(404, "no upload passthrough")
    upload_frames(search)
    assert search._files.unsupported and fake.uploads == []
    search._files.maintain()  # latched: no thread is even spawned
    search.search("anything")
    parts = fake.generates[-1]["contents"][0]["parts"]
    assert sum("inlineData" in part for part in parts) == 6  # exactly the pre-files behavior


def test_a_transient_upload_failure_retries_next_round(data_dir):
    search, fake, _ = make_search(data_dir, frames=3)
    fake.upload_error = GeminiHttpError(500, "hiccup")
    upload_frames(search)
    assert not search._files.unsupported and fake.uploads == []
    fake.upload_error = None
    upload_frames(search)
    assert len(fake.uploads) == 3


def test_a_failed_round_backs_off_before_retrying(data_dir):
    # The 1 Hz tick must not re-POST a full frame every second against a
    # broken endpoint — a failed round arms a cooldown that maintain() honors.
    search, fake, _ = make_search(data_dir, frames=3)
    fake.upload_error = GeminiHttpError(500, "hiccup")
    upload_frames(search)  # fails, arms the cooldown
    fake.upload_error = None
    search._files.maintain()  # inside the cooldown: returns before spawning
    assert fake.uploads == []
    search._files._retry_at = 0.0  # cooldown over
    upload_frames(search)
    assert len(fake.uploads) == 3


def test_a_cache_built_from_references_expires_with_its_files(data_dir):
    # A cache embedding fileData must not outlive the uploads it references —
    # its expiry is capped by the oldest file, not the 12 h cache TTL.
    search, fake, _ = make_search(data_dir, frames=6)
    upload_frames(search)
    files = search._files
    with files._lock:
        files._records = {i: replace(r, uploaded_at=r.uploaded_at - 46 * 3600) for i, r in files._records.items()}
    build_cache(search)
    cache = search._cache
    assert cache is not None
    remaining = cache.expires_monotonic - time.monotonic()
    assert 0 < remaining < 3600  # ~1 h of file life left, minus the safety margin


def test_the_cache_builds_from_file_references(data_dir):
    search, fake, _ = make_search(data_dir, frames=6)
    upload_frames(search)
    build_cache(search)
    (create,) = fake.creates
    parts = create["contents"][0]["parts"]
    assert sum("fileData" in part for part in parts) == 6 and not any("inlineData" in part for part in parts)


def test_a_not_yet_uploaded_frame_rides_inline_beside_references(data_dir):
    search, fake, store = make_search(data_dir, frames=3)
    upload_frames(search)
    store.add(30.0, 0.0, 0.0, 2000.0, b"jpg-late")
    search.search("anything")  # before any maintenance round reaches the new frame
    parts = fake.generates[-1]["contents"][0]["parts"]
    assert sum("fileData" in part for part in parts) == 3
    assert sum("inlineData" in part for part in parts) == 1


def test_search_never_waits_for_maintenance(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    fake.upload_gate = threading.Event()
    search._files.maintain()  # the background round is now stuck on the gate
    try:
        verdict = search.search("anything")  # must conclude while the upload hangs
        assert verdict.found
    finally:
        fake.upload_gate.set()
    assert search._files._busy.acquire(timeout=2)  # the round finishes once released
    search._files._busy.release()


def test_labels_and_verdicts_use_stable_store_ids(data_dir):
    search, fake, store = make_search(data_dir, frames=6, verdict={"found": True, "frame": 5, "explanation": "there"})
    store.evict(memory_with_id(store, 1))
    verdict = search.search("anything")
    parts = fake.generates[-1]["contents"][0]["parts"]
    labels = [part["text"] for part in parts if "text" in part][:-1]  # the last text part is the question
    assert labels[0].startswith("Frame 2 ")  # ids, not positions, after the eviction
    assert verdict.found and verdict.memory is not None and verdict.memory.id == 5


def mutate_after_generate(search: MemorySearch, fake: FakeGemini, mutate) -> None:
    """Rewire the search's transport so the store mutates while the answer is in flight."""
    inner = fake.rest.post

    def post_then_mutate(path: str, body: dict) -> dict:
        response = inner(path, body)
        mutate()
        return response

    search._rest = GeminiRest(post=post_then_mutate, delete=fake.rest.delete, upload=fake.rest.upload)


def test_a_map_switch_mid_search_voids_the_verdict(data_dir):
    search, fake, store = make_search(data_dir, frames=2)
    mutate_after_generate(search, fake, lambda: store.switch_map("B.yaml"))
    verdict = search.search("the kitchen")
    assert not verdict.found and "map changed" in verdict.error


def test_an_eviction_mid_search_misses_cleanly(data_dir):
    search, fake, store = make_search(data_dir, frames=2)  # the fake's verdict picks frame 1
    mutate_after_generate(search, fake, lambda: store.evict(memory_with_id(store, 1)))
    verdict = search.search("the kitchen")
    assert not verdict.found and not verdict.error


def remap_in_place(data_dir, store: MemoryStore) -> None:
    """Overwrite the active map under its own name and record a frame that
    reuses id 1 — the collision a name-only guard would hand to the agent."""
    (data_dir / "maps" / "A.pgm").write_bytes(b"remapped-content")
    store.switch_map("A.yaml")
    store.add(9.0, 9.0, 0.0, 2000.0, b"new-map-frame")


def test_a_same_name_remap_mid_search_voids_the_verdict(data_dir):
    search, fake, store = make_search(data_dir, frames=2)  # the fake's verdict picks frame 1
    mutate_after_generate(search, fake, lambda: remap_in_place(data_dir, store))
    verdict = search.search("the kitchen")
    assert not verdict.found and "map changed" in verdict.error


def test_a_same_name_remap_disqualifies_the_cache(data_dir):
    search, fake, store = make_search(data_dir, frames=6)
    build_cache(search)
    remap_in_place(data_dir, store)
    search.search("the kitchen")
    assert "cachedContent" not in fake.generates[-1]  # old-map frames must not vouch for reused ids


def test_a_fault_outliving_the_promotion_keeps_both_sets_recoverable(data_dir, monkeypatch):
    # The landing fails, the in-line restore fails, and the fault persists into
    # recovery. Neither scratch dir may be deleted while the map is absent --
    # they hold the map's only memories -- and once the fault clears, recovery
    # must put the map back.
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(9.0, 9.0, 0.0, 900.0, b"jpg-old")
    store.use_mapping_session(session_started=111.0)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-new")

    root = data_dir / "spatial_memory"
    real_replace = os.replace

    def landing_always_fails(src, dst):
        if Path(dst).name == "A":
            raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", landing_always_fails)
    with pytest.raises(OSError):
        store.promote_mapping_session("A.yaml", mapping_started=111.0)

    assert not (root / "A").is_dir()  # the window: map gone, both sets in scratch

    # A new mapping session runs while the fault persists: its sweep must spare
    # what the map is still owed.
    store.use_mapping_session(session_started=222.0)
    assert (root / ".mapping.displaced").is_dir(), "deleted the map's only surviving memories"

    monkeypatch.undo()  # the disk comes back
    recovered = MemoryStore(data_dir)
    recovered.switch_map("A.yaml")
    assert recovered.snapshot().memories, "recovery never gave the map its memories back"


def test_session_entry_lands_a_stranded_displaced_set_home(data_dir):
    # A failed promotion for A left A's only memories stranded in
    # .mapping.displaced with A's directory gone. The next recovery point
    # gives A its set back -- left in the slot, the stranded set would fail
    # every later displace into it with ENOTEMPTY.
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(9.0, 9.0, 0.0, 900.0, b"jpg-a")

    root = data_dir / "spatial_memory"
    (root / "A").rename(root / ".mapping.displaced")  # the state a failed swap leaves

    store.use_mapping_session(session_started=222.0)
    assert not (root / ".mapping.displaced").exists(), "the stranded set stayed to poison later saves"
    store.add(3.0, 4.0, 0.0, 2000.0, b"jpg-tour-b")
    assert store.promote_mapping_session("B.yaml", mapping_started=222.0) == 1

    recovered = MemoryStore(data_dir)
    recovered.switch_map("A.yaml")
    assert [m.x for m in recovered.snapshot().memories] == [9.0]
    recovered.switch_map("B.yaml")
    assert [m.x for m in recovered.snapshot().memories] == [3.0]


def test_a_stranded_set_never_blocks_a_save_over_an_existing_map(data_dir):
    # Mid-session strand: a failed save-as-A left A's set in the displaced
    # slot with A's directory gone, and the user re-saves the tour as B --
    # whose directory exists. Promotion must land A's set home and use the
    # cleared slot, not fail the displace with ENOTEMPTY on every save.
    store = MemoryStore(data_dir)
    store.switch_map("A.yaml")
    store.add(9.0, 9.0, 0.0, 900.0, b"jpg-a")
    store.switch_map("B.yaml")
    store.add(8.0, 8.0, 0.0, 901.0, b"jpg-b")
    store.use_mapping_session(session_started=222.0)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-tour")

    root = data_dir / "spatial_memory"
    (root / "A").rename(root / ".mapping.displaced")  # the state a failed A-save leaves

    assert store.promote_mapping_session("B.yaml", mapping_started=222.0) == 1
    store.switch_map("B.yaml")
    assert [m.x for m in store.snapshot().memories] == [1.0]
    store.switch_map("A.yaml")
    assert [m.x for m in store.snapshot().memories] == [9.0]


def test_a_failed_promotion_never_restores_another_maps_set(data_dir, monkeypatch):
    # B's memories are stranded in .mapping.displaced and a persistent fault
    # keeps them from landing home. A promotion for A then fails to land, and
    # the restore must not hand B's set to A -- a foreign coordinate frame in
    # A, and B stripped of its only copy.
    store = MemoryStore(data_dir)
    store.switch_map("B.yaml")
    store.add(9.0, 9.0, 0.0, 900.0, b"jpg-b")

    root = data_dir / "spatial_memory"
    (root / "B").rename(root / ".mapping.displaced")  # B stranded by an earlier failure

    real_replace = os.replace

    def replace_with_fault(src, dst):
        # B's landing home and the tour's landing both fail; a restore into A
        # would be free to run, which is exactly what must not happen with
        # B's set sitting there.
        if Path(dst).name in ("A", "B"):
            raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_with_fault)
    store.use_mapping_session(session_started=222.0)
    store.add(1.0, 2.0, 0.5, 1000.0, b"jpg-tour-a")
    with pytest.raises(OSError):
        store.promote_mapping_session("A.yaml", mapping_started=222.0)
    monkeypatch.undo()

    assert not (root / "A").is_dir(), "A was given another map's memories"
    stranded = json.loads((root / ".mapping.displaced" / "index.json").read_text())
    assert stranded["map"] == "B.yaml", "B's set was moved out from under it"
    assert [m["x"] for m in stranded["memories"]] == [9.0]
