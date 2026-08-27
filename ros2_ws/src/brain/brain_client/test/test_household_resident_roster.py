# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Deterministic tests for household visual identity state and artifacts."""

import io
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rclpy
from builtin_interfaces.msg import Time as TimeMsg
from PIL import Image

from brain_client.skills.types import SkillStorage
from brain_client.state.image import MainImage
from brain_client.state.map import Map
from brain_client.state.pose import Pose

REPO_ROOT = Path(__file__).resolve().parents[5]
APARTMENT_MAP_FIXTURE = Path(__file__).with_name("fixtures") / "sim_apartment.pgm"
sys.path.insert(0, str(REPO_ROOT / "workspace"))

from innate_skills import find_next_person as search_module  # noqa: E402
from innate_skills import mission_run as run_module  # noqa: E402
from innate_skills import navigate_to_position as navigation_module  # noqa: E402
from innate_skills import resident_roster as roster_module  # noqa: E402


def _frame(color: tuple[int, int, int]) -> MainImage:
    image = Image.new("RGB", (96, 72), color)
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG")
    return MainImage.from_jpeg(encoded.getvalue())


def _assert_contract(output, code: str, **expected):
    actual_code, encoded = output.message.split(" ", 1)
    payload = json.loads(encoded)
    assert actual_code == code
    assert output.data == payload
    for key, value in expected.items():
        assert payload[key] == value
    return payload


def _apartment_map_state() -> Map:
    """Load the committed navigation-map fixture available in clean CI."""
    pixels = np.flipud(np.asarray(Image.open(APARTMENT_MAP_FIXTURE).convert("L")))
    cells = np.full(pixels.shape, -1, dtype=np.int8)
    cells[pixels >= 250] = 0
    cells[pixels <= 50] = 100
    return Map(
        resolution=0.05,
        width=cells.shape[1],
        height=cells.shape[0],
        origin_x=-11.0884,
        origin_y=-7.4106,
        raw_source=SimpleNamespace(data=cells.flatten().tolist()),
    )


def _legacy_state(reference: MainImage) -> dict:
    return {
        "version": 1,
        "expected_residents": 3,
        "next_sequence": 2,
        "active_encounter_id": "resident-001",
        "active_observation_image_b64": str(reference),
        "encounters": [
            {
                "encounter_id": "resident-001",
                "name": None,
                "status": "seen",
                "order": None,
                "reference_image_b64": str(reference),
            }
        ],
        "pending": [],
    }


def _authorize_identity(
    state: dict,
    encounter_id: str,
    reference: MainImage,
    *,
    identified_at: float | None = None,
    pose: Pose | None = None,
) -> None:
    state["active_encounter_id"] = encounter_id
    state["active_observation_image_b64"] = str(reference)
    state["active_identified_at"] = time.time() if identified_at is None else identified_at
    state["active_identified_pose"] = roster_module._pose_record(pose)


@pytest.fixture
def roster(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    skill = roster_module.ResidentRoster(None)
    skill._storage = SkillStorage(tmp_path / "workspace" / "skill_storage" / "resident_roster.json")
    skill._gemini_client = object()
    return skill


def _start_mission_run() -> str:
    return run_module.start_run("household_orders_agent")["run_id"]


def _seed_confirmed_resident(roster) -> None:
    state = roster._load_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Alex",
            "status": "confirmed",
            "order": "A complete order",
            "reference_images_b64": [],
            "reference_images_verified": True,
        }
    ]
    roster._save_state(state)


def test_clear_first_sighting_still_creates_a_new_resident(roster, monkeypatch):
    first = _frame((130, 40, 40))
    roster.storage["state"] = roster_module.ResidentRoster._fresh_state()
    roster._fresh_upward_frame = lambda: (first, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "new",
                "encounter_id": None,
                "plausible_encounter_ids": [],
                "confidence": 0.96,
                "view_quality": "good",
                "reason": "one clear new resident",
            }
        ),
    )

    output = roster._identify()
    saved = roster._load_state()

    payload = _assert_contract(output, "NEW_PERSON", encounter_id="resident-001")
    assert payload == {"encounter_id": "resident-001", "resident_status": "seen"}
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-001"]
    assert saved["active_encounter_id"] == "resident-001"
    assert saved["active_observation_image_b64"] == str(first)
    assert saved["active_identified_at"] == pytest.approx(time.time(), abs=2.0)


def test_record_order_requires_explicit_current_identify_encounter(roster):
    casey = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": None,
            "status": "seen",
            "order": None,
            "reference_images_b64": [str(casey)],
        }
    ]
    _authorize_identity(state, "resident-001", casey)
    roster.storage["state"] = state

    missing_id = roster._record_order("Casey", "One burrito", None)
    wrong_id = roster._record_order("Casey", "One burrito", "resident-999")
    saved = roster._load_state()

    _assert_contract(missing_id, "ORDER_NOT_RECORDED", reason="explicit_encounter_id_required")
    _assert_contract(wrong_id, "ORDER_NOT_RECORDED", reason="encounter_not_currently_identified")
    assert saved["encounters"][0]["status"] == "seen"
    assert saved["encounters"][0]["name"] is None


def test_named_identity_conflict_never_clones_stale_casey_frame_as_alex(roster):
    casey = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "confirmed",
            "order": "A veggie burrito",
            "reference_images_b64": [str(casey)],
        }
    ]
    _authorize_identity(state, "resident-001", casey)
    roster.storage["state"] = state

    output = roster._record_order("Alex", "A pepperoni pizza", "resident-001")
    saved = roster._load_state()

    _assert_contract(output, "IDENTITY_CONFLICT", encounter_id="resident-001")
    assert [entry["name"] for entry in saved["encounters"]] == ["Casey"]
    assert saved["encounters"][0]["reference_images_b64"] == [str(casey)]
    assert saved["next_sequence"] == 2
    assert saved["pending"] == []
    assert saved["active_encounter_id"] is None
    assert saved["active_observation_image_b64"] is None


def test_ambiguous_multi_profile_sighting_does_not_allocate_an_id(roster, monkeypatch):
    casey = _frame((130, 40, 40))
    alex = _frame((40, 130, 40))
    ambiguous = _frame((40, 40, 130))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["active_encounter_id"] = "resident-001"
    state["active_observation_image_b64"] = str(casey)
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "confirmed",
            "order": "A veggie burrito",
            "reference_images_b64": [str(casey)],
        },
        {
            "encounter_id": "resident-002",
            "name": "Alex",
            "status": "confirmed",
            "order": "A pepperoni pizza",
            "reference_images_b64": [str(alex)],
        },
    ]
    roster.storage["state"] = state
    roster._fresh_upward_frame = lambda: (ambiguous, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "uncertain",
                "encounter_id": None,
                "confidence": 0.91,
                "view_quality": "good",
                "reason": "identical to both resident-001 and resident-002",
            }
        ),
    )

    output = roster._identify()
    saved = roster._load_state()

    _assert_contract(
        output,
        "AMBIGUOUS_PERSON",
        plausible_encounter_ids=["resident-001", "resident-002"],
    )
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-001", "resident-002"]
    assert saved["pending"] == []
    assert saved["next_sequence"] == 3
    assert saved["active_encounter_id"] is None


@pytest.mark.parametrize(
    "reply",
    [
        {
            "decision": "new",
            "encounter_id": None,
            "plausible_encounter_ids": ["resident-001"],
            "confidence": 0.93,
            "view_quality": "good",
            "reason": "could still be the first profile",
        },
        {
            "decision": "match",
            "encounter_id": "resident-001",
            "plausible_encounter_ids": ["resident-002"],
            "confidence": 0.93,
            "view_quality": "good",
            "reason": "resident-001 is best but resident-002 is also plausible",
        },
        {
            "decision": "match",
            "encounter_id": "resident-999",
            "plausible_encounter_ids": ["resident-001"],
            "confidence": 0.93,
            "view_quality": "good",
            "reason": "the claimed ID is invalid but resident-001 remains plausible",
        },
    ],
)
def test_any_existing_plausible_identity_blocks_new_profile_allocation(roster, monkeypatch, reply):
    casey = _frame((130, 40, 40))
    alex = _frame((40, 130, 40))
    ambiguous = _frame((40, 40, 130))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "confirmed",
            "order": "A veggie burrito",
            "reference_images_b64": [str(casey)],
        },
        {
            "encounter_id": "resident-002",
            "name": "Alex",
            "status": "confirmed",
            "order": "A pepperoni pizza",
            "reference_images_b64": [str(alex)],
        },
    ]
    roster.storage["state"] = state
    roster._fresh_upward_frame = lambda: (ambiguous, None)
    monkeypatch.setattr(roster_module.gemlib, "ask_image", lambda *_args, **_kwargs: json.dumps(reply))

    output = roster._identify()
    saved = roster._load_state()

    _assert_contract(output, "AMBIGUOUS_PERSON")
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-001", "resident-002"]
    assert saved["pending"] == []
    assert saved["next_sequence"] == 3
    assert saved["active_encounter_id"] is None


def test_duplicate_reference_frame_has_only_one_profile_owner(roster):
    duplicated = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["active_encounter_id"] = "resident-002"
    state["active_observation_image_b64"] = str(duplicated)
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "confirmed",
            "order": "A veggie burrito",
            "reference_images_b64": [str(duplicated)],
        },
        {
            "encounter_id": "resident-002",
            "name": "Alex",
            "status": "confirmed",
            "order": "A pepperoni pizza",
            "reference_images_b64": [str(duplicated)],
        },
    ]
    roster.storage["state"] = state

    loaded = roster._load_state()

    assert loaded["encounters"][0]["reference_images_b64"] == [str(duplicated)]
    assert loaded["encounters"][1]["reference_images_b64"] == []
    assert loaded["active_encounter_id"] is None
    owners = [
        entry["encounter_id"] for entry in loaded["encounters"] if str(duplicated) in entry["reference_images_b64"]
    ]
    assert owners == ["resident-001"]


def test_false_visual_match_does_not_poison_known_gallery(roster, monkeypatch):
    casey = _frame((130, 40, 40))
    alex = _frame((40, 130, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "confirmed",
            "order": "A veggie burrito",
            "reference_images_b64": [str(casey)],
        }
    ]
    roster.storage["state"] = state
    roster._fresh_upward_frame = lambda: (alex, None)
    replies = iter(
        [
            {
                "decision": "match",
                "encounter_id": "resident-001",
                "plausible_encounter_ids": ["resident-001"],
                "confidence": 0.95,
                "view_quality": "good",
                "reason": "looks like resident-001",
            },
            {
                "decision": "new",
                "encounter_id": None,
                "plausible_encounter_ids": [],
                "confidence": 0.96,
                "view_quality": "good",
                "reason": "clearly different from resident-001",
            },
        ]
    )
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(next(replies)),
    )

    mistaken = roster._identify()
    after_match = roster._load_state()
    conflict = roster._record_order("Alex", "A pepperoni pizza", "resident-001")
    after_conflict = roster._load_state()
    retried = roster._identify()
    after_retry = roster._load_state()

    _assert_contract(mistaken, "KNOWN_PERSON", encounter_id="resident-001")
    assert after_match["encounters"][0]["reference_images_b64"] == [str(casey)]
    assert after_match["active_observation_pending_reference"] is True
    _assert_contract(conflict, "IDENTITY_CONFLICT", encounter_id="resident-001")
    assert after_conflict["encounters"][0]["reference_images_b64"] == [str(casey)]
    assert "last_identity" not in after_conflict["encounters"][0]
    _assert_contract(retried, "NEW_PERSON", encounter_id="resident-002")
    assert after_retry["encounters"][0]["reference_images_b64"] == [str(casey)]
    assert after_retry["encounters"][1]["reference_images_b64"] == [str(alex)]


def test_same_name_pending_sighting_still_merges_into_existing_profile(roster):
    first = _frame((130, 40, 40))
    second = _frame((40, 130, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "seen",
            "order": None,
            "reference_images_b64": [str(first)],
        }
    ]
    state["pending"] = [
        {
            "encounter_id": "resident-002",
            "status": "seen",
            "reference_images_b64": [str(second)],
        }
    ]
    _authorize_identity(state, "resident-002", second)
    roster.storage["state"] = state

    output = roster._record_order("Casey", "A veggie burrito", "resident-002")
    saved = roster._load_state()

    _assert_contract(output, "ORDER_RECORDED", encounter_id="resident-001")
    assert len(saved["encounters"]) == 1
    assert saved["pending"] == []
    assert saved["encounters"][0]["reference_images_b64"] == [str(first), str(second)]


def test_confirmation_clears_active_identity_evidence(roster):
    casey = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "asked",
            "order": "A veggie burrito",
            "reference_images_b64": [str(casey)],
        }
    ]
    _authorize_identity(state, "resident-001", casey)
    roster.storage["state"] = state

    missing_name = roster._confirm(None, "resident-001")
    output = roster._confirm("Casey", "resident-001")
    saved = roster._load_state()

    _assert_contract(missing_name, "PERSON_NOT_CONFIRMED", reason="explicit_name_required")
    _assert_contract(output, "PERSON_CONFIRMED", encounter_id="resident-001")
    assert saved["encounters"][0]["status"] == "confirmed"
    assert saved["active_encounter_id"] is None
    assert saved["active_observation_image_b64"] is None
    assert saved["active_identified_at"] is None
    assert saved["active_identified_pose"] is None


def test_v2_migration_preserves_roster_but_drops_stale_identity_authority(roster):
    casey = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["version"] = 2
    state["next_sequence"] = 2
    state["active_encounter_id"] = "resident-001"
    state["active_observation_image_b64"] = str(casey)
    state.pop("active_identified_at")
    state.pop("active_identified_pose")
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "confirmed",
            "order": "A veggie burrito",
            "reference_images_b64": [str(casey)],
        }
    ]
    roster.storage["state"] = state

    loaded = roster._load_state()

    assert loaded["version"] == roster_module.STATE_VERSION
    assert loaded["encounters"][0]["name"] == "Casey"
    assert loaded["encounters"][0]["order"] == "A veggie burrito"
    assert loaded["active_encounter_id"] is None
    assert loaded["active_observation_image_b64"] is None


def test_record_order_rejects_identity_after_robot_moves(roster):
    casey = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": None,
            "status": "seen",
            "order": None,
            "reference_images_b64": [str(casey)],
        }
    ]
    _authorize_identity(state, "resident-001", casey, pose=Pose(1.0, 2.0, 0.0))
    roster.storage["state"] = state
    roster.pose = Pose(1.5, 2.0, 0.0)

    output = roster._record_order("Casey", "A veggie burrito", "resident-001")
    saved = roster._load_state()

    _assert_contract(output, "ORDER_NOT_RECORDED", reason="robot_moved_since_identify")
    assert saved["encounters"][0]["status"] == "seen"
    assert saved["active_encounter_id"] is None


def test_record_order_rejects_expired_identity_binding(roster):
    casey = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": None,
            "status": "seen",
            "order": None,
            "reference_images_b64": [str(casey)],
        }
    ]
    _authorize_identity(
        state,
        "resident-001",
        casey,
        identified_at=time.time() - roster_module.IDENTITY_BINDING_TTL_S - 1.0,
    )
    roster.storage["state"] = state

    output = roster._record_order("Casey", "A veggie burrito", "resident-001")
    saved = roster._load_state()

    _assert_contract(output, "ORDER_NOT_RECORDED", reason="identity_context_expired")
    assert saved["encounters"][0]["status"] == "seen"
    assert saved["active_encounter_id"] is None


def test_record_order_accepts_recent_identity_without_meaningful_motion(roster):
    casey = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": None,
            "status": "seen",
            "order": None,
            "reference_images_b64": [str(casey)],
        }
    ]
    _authorize_identity(state, "resident-001", casey, pose=Pose(1.0, 2.0, 0.0))
    roster.storage["state"] = state
    roster.pose = Pose(1.1, 2.1, 0.1)

    output = roster._record_order("Casey", "A veggie burrito", "resident-001")
    saved = roster._load_state()

    _assert_contract(output, "ORDER_RECORDED", encounter_id="resident-001")
    assert saved["encounters"][0]["status"] == "asked"


def test_confirmation_rejects_identity_after_robot_moves(roster):
    casey = _frame((130, 40, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 2
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": None,
            "status": "seen",
            "order": None,
            "reference_images_b64": [str(casey)],
        }
    ]
    _authorize_identity(state, "resident-001", casey, pose=Pose(1.0, 2.0, 0.0))
    roster.storage["state"] = state
    roster.pose = Pose(1.0, 2.0, 0.0)
    recorded = roster._record_order("Casey", "A veggie burrito", "resident-001")
    roster.pose = Pose(1.5, 2.0, 0.0)

    output = roster._confirm("Casey", "resident-001")
    saved = roster._load_state()

    _assert_contract(recorded, "ORDER_RECORDED", encounter_id="resident-001")
    _assert_contract(output, "PERSON_NOT_CONFIRMED", reason="robot_moved_since_identify")
    assert saved["encounters"][0]["status"] == "asked"
    assert saved["active_encounter_id"] is None


def test_confirmation_cannot_retarget_the_active_encounter_by_name(roster):
    casey = _frame((130, 40, 40))
    alex = _frame((40, 130, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "asked",
            "order": "A veggie burrito",
            "reference_images_b64": [str(casey)],
        },
        {
            "encounter_id": "resident-002",
            "name": "Alex",
            "status": "asked",
            "order": "A pepperoni pizza",
            "reference_images_b64": [str(alex)],
        },
    ]
    _authorize_identity(state, "resident-001", casey)
    roster.storage["state"] = state

    output = roster._confirm("Alex", "resident-001")
    saved = roster._load_state()

    _assert_contract(output, "PERSON_NOT_CONFIRMED", reason="name_mismatch", encounter_id="resident-001")
    assert [entry["status"] for entry in saved["encounters"]] == ["asked", "asked"]


def test_legacy_reference_is_replaced_by_first_verified_continuity_view(roster, monkeypatch):
    first = _frame((130, 40, 40))
    second = _frame((40, 130, 40))
    roster.storage["state"] = _legacy_state(first)
    state = roster._load_state()
    identified_pose = Pose(1.0, 2.0, 0.0)
    _authorize_identity(state, "resident-001", first, pose=identified_pose)
    roster.storage["state"] = state
    roster.pose = Pose(1.8, 2.0, math.radians(20.0))
    roster._fresh_upward_frame = lambda: (second, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "new",
                "encounter_id": None,
                "confidence": 0.96,
                "view_quality": "good",
                "reason": "different angle",
            }
        ),
    )

    output = roster._identify("resident-001")
    before_dialogue = roster._load_state()
    recorded = roster._record_order("Casey", "A veggie burrito", "resident-001")
    state = roster._load_state()

    _assert_contract(output, "UNCERTAIN_PERSON", encounter_id="resident-001")
    assert before_dialogue["encounters"][0]["reference_images_b64"] == [str(first)]
    assert before_dialogue["active_observation_pending_reference"] is True
    _assert_contract(recorded, "ORDER_RECORDED", encounter_id="resident-001")
    assert state["version"] == roster_module.STATE_VERSION
    assert len(state["encounters"]) == 1
    assert state["encounters"][0]["reference_images_b64"] == [str(second)]
    assert state["encounters"][0]["reference_images_verified"] is True
    assert (run_module.artifact_root() / "resident_roster.png").is_file()


def test_unusable_view_never_creates_or_overwrites_profile(roster, monkeypatch):
    first = _frame((130, 40, 40))
    poor = _frame((20, 20, 20))
    roster.storage["state"] = _legacy_state(first)
    roster._fresh_upward_frame = lambda: (poor, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "new",
                "encounter_id": None,
                "confidence": 0.99,
                "view_quality": "unusable",
                "reason": "only a cropped leg",
            }
        ),
    )

    output = roster._identify()
    state = roster._load_state()

    _assert_contract(output, "IDENTITY_UNAVAILABLE", reason="insufficient_identity_view")
    assert len(state["encounters"]) == 1
    assert state["encounters"][0]["reference_images_b64"] == [str(first)]
    assert state["pending"] == []


def test_limited_high_confidence_match_never_merges_or_skips_profile(roster, monkeypatch):
    first = _frame((130, 40, 40))
    duplicate = _frame((40, 130, 40))
    poor = _frame((20, 20, 20))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Alex",
            "status": "confirmed",
            "order": "A complete order",
            "reference_images_b64": [str(first)],
        }
    ]
    state["pending"] = [
        {
            "encounter_id": "resident-002",
            "status": "seen",
            "reference_images_b64": [str(duplicate)],
        }
    ]
    identified_pose = Pose(1.0, 2.0, 0.0)
    _authorize_identity(state, "resident-002", duplicate, pose=identified_pose)
    roster.storage["state"] = state
    roster.pose = Pose(1.8, 2.0, math.radians(20.0))
    roster._fresh_upward_frame = lambda: (poor, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "match",
                "encounter_id": "resident-001",
                "confidence": 0.99,
                "view_quality": "limited",
                "reason": "face mostly hidden by the crop",
            }
        ),
    )

    output = roster._identify("resident-002")
    state = roster._load_state()

    _assert_contract(output, "IDENTITY_UNAVAILABLE", reason="insufficient_identity_view")
    assert len(state["encounters"]) == 1
    assert state["encounters"][0]["reference_images_b64"] == [str(first)]
    assert state["active_encounter_id"] is None
    assert [entry["encounter_id"] for entry in state["pending"]] == ["resident-002"]
    assert state["pending"][0]["reference_images_b64"] == [str(duplicate)]


def test_match_id_must_have_been_rendered_in_current_contact_sheet(roster, monkeypatch):
    visible = _frame((130, 40, 40))
    current = _frame((40, 130, 40))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Alex",
            "status": "confirmed",
            "order": "A complete order",
            "reference_images_b64": [str(visible)],
        },
        {
            "encounter_id": "resident-002",
            "name": "Blake",
            "status": "confirmed",
            "order": "Another complete order",
            "reference_images_b64": ["not-valid-base64"],
        },
    ]
    roster.storage["state"] = state
    roster._fresh_upward_frame = lambda: (current, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "match",
                "encounter_id": "resident-002",
                "confidence": 0.99,
                "view_quality": "good",
                "reason": "claimed match to an omitted tile",
            }
        ),
    )

    output = roster._identify()
    saved = roster.storage.get("state")

    _assert_contract(output, "UNCERTAIN_PERSON", encounter_id="resident-003")
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-001", "resident-002"]
    assert [entry["encounter_id"] for entry in saved["pending"]] == ["resident-003"]
    assert saved["active_encounter_id"] == "resident-003"


def test_high_confidence_match_merges_unresolved_continuity(roster, monkeypatch):
    known = _frame((130, 40, 40))
    duplicate = _frame((40, 130, 40))
    current = _frame((40, 40, 130))
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Blake",
            "status": "confirmed",
            "order": "A complete order",
            "reference_images_b64": [str(known)],
        },
        {
            "encounter_id": "resident-002",
            "name": None,
            "status": "seen",
            "order": None,
            "reference_images_b64": [str(duplicate)],
        },
    ]
    identified_pose = Pose(1.0, 2.0, 0.0)
    _authorize_identity(state, "resident-002", duplicate, pose=identified_pose)
    roster.storage["state"] = state
    roster.pose = Pose(1.8, 2.0, math.radians(20.0))
    roster._fresh_upward_frame = lambda: (current, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "match",
                "encounter_id": "resident-001",
                "confidence": 0.94,
                "view_quality": "good",
                "reason": "same face from another angle",
            }
        ),
    )

    output = roster._identify("resident-002")
    after_identify = roster._load_state()
    recorded = roster._record_order("Blake", "A complete order", "resident-001")
    saved = roster._load_state()

    _assert_contract(output, "KNOWN_PERSON", encounter_id="resident-001", order="A complete order")
    assert [entry["encounter_id"] for entry in after_identify["encounters"]] == [
        "resident-001",
        "resident-002",
    ]
    assert after_identify["encounters"][0]["reference_images_b64"] == [str(known)]
    assert after_identify["active_continuity_id"] == "resident-002"
    _assert_contract(recorded, "ORDER_RECORDED", encounter_id="resident-001")
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-001"]
    assert saved["encounters"][0]["reference_images_b64"] == [str(known), str(current)]
    assert saved["encounters"][0]["last_identity"] == {
        "decision": "match",
        "confidence": 0.94,
        "view_quality": "good",
        "reason": "same face from another angle",
    }


def test_contact_sheet_budget_gives_each_profile_a_first_view_before_seconds():
    state = roster_module.ResidentRoster._fresh_state()
    state["encounters"] = [
        {
            "encounter_id": f"resident-{index:03d}",
            "name": None,
            "status": "seen",
            "order": None,
            "reference_images_b64": [f"{index}-front", f"{index}-side"],
        }
        for index in range(1, 5)
    ]

    selected = roster_module._contact_references(state)

    assert [item[0] for item in selected[:4]] == [
        "resident-001",
        "resident-002",
        "resident-003",
        "resident-004",
    ]
    assert [item[2] for item in selected[4:]] == ["1-side", "2-side"]


def test_roster_status_contract_contains_state_not_policy(roster):
    state = roster_module.ResidentRoster._fresh_state()
    state["encounters"] = [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "confirmed",
            "order": "A veggie burrito",
            "reference_images_b64": [str(_frame((130, 40, 40)))],
            "last_identity": {
                "decision": "match",
                "confidence": 0.99,
                "view_quality": "good",
                "reason": "internal diagnostic",
            },
        }
    ]
    roster.storage["state"] = state

    output = roster._status()
    payload = _assert_contract(output, "ROSTER_STATUS", seen=1, asked=1, confirmed=1)

    assert set(payload) == {
        "expected_residents",
        "seen",
        "asked",
        "confirmed",
        "residents",
        "uncertain_observations",
    }
    assert payload["residents"] == [
        {
            "encounter_id": "resident-001",
            "name": "Casey",
            "status": "confirmed",
            "order": "A veggie burrito",
        }
    ]
    assert output.image is None


def test_roster_begin_contract_is_only_initialization_state(roster):
    _start_mission_run()
    output = roster._begin()

    payload = _assert_contract(output, "ROSTER_INITIALIZED", expected_residents=3)
    assert payload == {"expected_residents": 3}
    assert output.image is None


def test_roster_begin_is_idempotent_within_one_agent_run(roster, tmp_path):
    run_id = _start_mission_run()
    _assert_contract(roster._begin(), "ROSTER_INITIALIZED", expected_residents=3)
    _seed_confirmed_resident(roster)

    restarted = roster_module.ResidentRoster(None)
    restarted._storage = SkillStorage(tmp_path / "workspace" / "skill_storage" / "resident_roster.json")
    output = restarted._begin()
    saved = restarted._load_state()

    payload = _assert_contract(output, "ROSTER_ALREADY_INITIALIZED", expected_residents=3)
    assert payload == {"expected_residents": 3}
    assert saved["run_id"] == run_id
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-001"]


def test_roster_begin_resets_when_agent_starts_a_new_run(roster):
    _start_mission_run()
    roster._begin()
    _seed_confirmed_resident(roster)
    next_run_id = _start_mission_run()

    output = roster._begin()
    saved = roster._load_state()

    _assert_contract(output, "ROSTER_INITIALIZED", expected_residents=3)
    assert saved["run_id"] == next_run_id
    assert saved["next_sequence"] == 1
    assert saved["encounters"] == []


def test_roster_begin_requires_agent_run_without_destroying_state(roster):
    _start_mission_run()
    roster._begin()
    _seed_confirmed_resident(roster)
    (run_module.artifact_root() / run_module.ACTIVE_RUN_FILENAME).unlink()

    output = roster._begin()
    saved = roster._load_state()

    _assert_contract(output, "RUN_UNINITIALIZED")
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-001"]


def test_agent_run_exists_without_challenge_state(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    run = run_module.start_run("standalone_agent")
    assert run_module.active_run_id() == run["run_id"]
    assert not (tmp_path / "workspace" / "challenges.json").exists()


def test_artifacts_keep_latest_and_immutable_per_run_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    first_run = run_module.start_run("standalone_agent")["run_id"]
    run_module.write_artifact_set("example", {"example.json": b'{"step":1}'})
    run_module.write_artifact_set("example", {"example.json": b'{"step":2}'})

    root = run_module.artifact_root()
    assert (root / "example.json").read_bytes() == b'{"step":2}'
    assert (root / "runs" / first_run / "latest" / "example.json").read_bytes() == b'{"step":2}'
    first_snapshots = sorted((root / "runs" / first_run / "snapshots" / "example").glob("*/example.json"))
    assert [path.read_bytes() for path in first_snapshots] == [b'{"step":1}', b'{"step":2}']

    second_run = run_module.start_run("standalone_agent")["run_id"]
    run_module.write_artifact_set("example", {"example.json": b'{"step":3}'})
    second_snapshots = sorted((root / "runs" / second_run / "snapshots" / "example").glob("*/example.json"))
    assert [path.read_bytes() for path in second_snapshots] == [b'{"step":3}']
    assert [path.read_bytes() for path in first_snapshots] == [b'{"step":1}', b'{"step":2}']


def test_search_reset_contract_contains_no_behavior_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    _start_mission_run()
    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(tmp_path / "find_next_person.json")

    output = skill.execute(reset=True)

    _assert_contract(output, "SEARCH_RESET")
    assert output.data == {}
    assert output.image is None


def test_search_reset_is_idempotent_within_one_agent_run(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    run_id = _start_mission_run()
    storage_path = tmp_path / "workspace" / "skill_storage" / "find_next_person.json"
    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(storage_path)
    _assert_contract(skill.execute(reset=True), "SEARCH_RESET")
    state = skill.storage["state"]
    state["observations"] = [{"x": 1.0, "y": 2.0, "theta": 0.5}]
    state["recovery_anchor"] = {"x": 0.5, "y": 1.5, "theta": 0.0}
    skill.storage["state"] = state

    restarted = search_module.FindNextPerson(None)
    restarted._storage = SkillStorage(storage_path)
    output = restarted.execute(reset=True)

    payload = _assert_contract(output, "SEARCH_ALREADY_INITIALIZED")
    assert payload == {"observations": 1, "unreachable": 0}
    assert restarted.storage["state"]["run_id"] == run_id
    assert restarted.storage["state"]["observations"] == state["observations"]
    assert restarted.storage["state"]["recovery_anchor"] == state["recovery_anchor"]


def test_search_reset_clears_state_when_agent_starts_a_new_run(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    _start_mission_run()
    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(tmp_path / "find_next_person.json")
    skill.execute(reset=True)
    state = skill.storage["state"]
    state["observations"] = [{"x": 1.0, "y": 2.0, "theta": 0.5}]
    state["recovery_anchor"] = {"x": 0.5, "y": 1.5, "theta": 0.0}
    skill.storage["state"] = state
    next_run_id = _start_mission_run()

    output = skill.execute(reset=True)

    _assert_contract(output, "SEARCH_RESET")
    assert skill.storage["state"]["run_id"] == next_run_id
    assert skill.storage["state"]["observations"] == []
    assert skill.storage["state"]["recovery_anchor"] is None


def test_search_reset_requires_agent_run_without_destroying_state(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    _start_mission_run()
    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(tmp_path / "find_next_person.json")
    skill.execute(reset=True)
    state = skill.storage["state"]
    state["observations"] = [{"x": 1.0, "y": 2.0, "theta": 0.5}]
    skill.storage["state"] = state
    (run_module.artifact_root() / run_module.ACTIVE_RUN_FILENAME).unlink()

    output = skill.execute(reset=True)

    _assert_contract(output, "RUN_UNINITIALIZED")
    assert skill.storage["state"]["observations"] == [{"x": 1.0, "y": 2.0, "theta": 0.5}]


def _execute_search_with_navigation_error(tmp_path, monkeypatch, error):
    cells = np.zeros((8, 8), dtype=np.int8)
    map_state = Map(
        resolution=0.25,
        width=cells.shape[1],
        height=cells.shape[0],
        origin_x=0.0,
        origin_y=0.0,
        raw_source=SimpleNamespace(data=cells.flatten().tolist()),
    )
    traversable = np.ones(cells.shape, dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=traversable,
        blocked=~traversable,
        safe=traversable,
        reachable=traversable,
        navigable=traversable,
    )
    view = search_module._View(
        row=2,
        col=6,
        x=1.625,
        y=0.625,
        theta=math.pi / 2.0,
        visible=frozenset(),
        gain=10,
        score=1.0,
    )

    class FailingNavigation:
        def approach_viewpoint(self, **_kwargs):
            raise error

    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(tmp_path / "find_next_person.json")
    skill.map = map_state
    skill.pose = Pose(0.125, 0.125, 0.0)
    skill.image = _frame((80, 90, 100))
    skill.wait_for = lambda read, **_kwargs: read()
    skill.check_cancelled = lambda: None
    skill.navigate = FailingNavigation()
    monkeypatch.setattr(search_module, "_build_planning_grid", lambda *_args: plan)
    monkeypatch.setattr(search_module, "_choose_view", lambda *_args, **_kwargs: (view, set()))
    monkeypatch.setattr(skill, "_refresh_visualization", lambda *_args, **_kwargs: None)

    return skill, view, skill.execute()


def test_temporary_navigation_failure_does_not_blacklist_viewpoint(tmp_path, monkeypatch):
    skill, _view, output = _execute_search_with_navigation_error(
        tmp_path,
        monkeypatch,
        navigation_module.NavigationInfrastructureError("planner timeout"),
    )

    payload = _assert_contract(output, "SEARCH_INFRASTRUCTURE_FAILURE")
    assert payload == {"component": "navigation", "reason": "planner timeout"}
    assert skill.storage["state"]["unreachable"] == []
    assert skill.storage["state"]["navigation_failures"] == []


def test_indeterminate_preflight_requires_confirmation_before_blacklisting(tmp_path, monkeypatch):
    skill, view, first = _execute_search_with_navigation_error(
        tmp_path,
        monkeypatch,
        navigation_module.NavigationPathIndeterminateError("planner aborted"),
    )

    _assert_contract(first, "SEARCH_UNREACHABLE")
    assert skill.storage["state"]["unreachable"] == []
    assert len(skill.storage["state"]["navigation_failures"]) == 1
    first_failure = skill.storage["state"]["navigation_failures"][0]
    assert {key: first_failure[key] for key in ("x", "y", "theta", "attempts")} == {
        "x": view.x,
        "y": view.y,
        "theta": round(view.theta, 5),
        "attempts": 1,
    }
    assert first_failure["retry_after"] > time.time()

    second = skill.execute()

    _assert_contract(second, "SEARCH_UNREACHABLE")
    assert skill.storage["state"]["unreachable"] == [{"x": view.x, "y": view.y, "theta": round(view.theta, 5)}]
    assert skill.storage["state"]["navigation_failures"] == []


def test_navigation_execution_failure_temporarily_defers_viewpoint(tmp_path, monkeypatch):
    skill, view, output = _execute_search_with_navigation_error(
        tmp_path,
        monkeypatch,
        navigation_module.NavigationNoProgressError("Nav2 made no route progress"),
    )

    _assert_contract(output, "SEARCH_UNREACHABLE", temporary=True)
    assert skill.storage["state"]["unreachable"] == []
    failure = skill.storage["state"]["navigation_failures"][0]
    assert {key: failure[key] for key in ("x", "y", "theta", "attempts")} == {
        "x": view.x,
        "y": view.y,
        "theta": round(view.theta, 5),
        "attempts": 1,
    }
    assert failure["retry_after"] > time.time()


def test_generic_navigation_failure_is_reported_without_blacklisting(tmp_path, monkeypatch):
    skill, _view, output = _execute_search_with_navigation_error(
        tmp_path,
        monkeypatch,
        search_module.SkillFailed("pose unavailable after visual standoff"),
    )

    payload = _assert_contract(output, "SEARCH_INFRASTRUCTURE_FAILURE")
    assert payload == {"component": "navigation", "reason": "pose unavailable after visual standoff"}
    assert skill.storage["state"]["unreachable"] == []
    assert skill.storage["state"]["navigation_failures"] == []


def test_failed_route_that_moved_records_its_planner_valid_source_for_recovery(tmp_path, monkeypatch):
    cells = np.zeros((8, 8), dtype=np.int8)
    map_state = Map(
        resolution=0.25,
        width=cells.shape[1],
        height=cells.shape[0],
        origin_x=0.0,
        origin_y=0.0,
        raw_source=SimpleNamespace(data=cells.flatten().tolist()),
    )
    traversable = np.ones(cells.shape, dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=traversable,
        blocked=~traversable,
        safe=traversable,
        reachable=traversable,
        navigable=traversable,
    )
    view = search_module._View(2, 6, 1.625, 0.625, math.pi / 2.0, frozenset(), 10, 1.0)
    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(tmp_path / "find_next_person.json")
    source_pose = Pose(0.625, 0.625, 0.0, stamp=1.0)
    skill.map = map_state
    skill.pose = source_pose
    skill.image = _frame((80, 90, 100))
    skill.wait_for = lambda read, **_kwargs: read()
    skill.check_cancelled = lambda: None
    monkeypatch.setattr(search_module, "_build_planning_grid", lambda *_args: plan)
    monkeypatch.setattr(search_module, "_choose_view", lambda *_args, **_kwargs: (view, set()))
    monkeypatch.setattr(skill, "_refresh_visualization", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(skill, "_fresh_upward_frame", lambda: None)

    class MovedThenFailedNavigation:
        def approach_viewpoint(self, **_kwargs):
            skill.pose = Pose(1.125, 0.625, 0.0, stamp=2.0)
            raise navigation_module.NavigationNoProgressError("route stalled")

    skill.navigate = MovedThenFailedNavigation()

    output = skill.execute()

    _assert_contract(output, "SEARCH_UNREACHABLE", temporary=True)
    assert skill.storage["state"]["recovery_anchor"] == {
        "x": source_pose.x,
        "y": source_pose.y,
        "theta": source_pose.theta,
    }


def _search_with_pending_recovery(tmp_path, monkeypatch, *, choose_view):
    cells = np.zeros((8, 8), dtype=np.int8)
    map_state = Map(
        resolution=0.25,
        width=cells.shape[1],
        height=cells.shape[0],
        origin_x=0.0,
        origin_y=0.0,
        raw_source=SimpleNamespace(data=cells.flatten().tolist()),
    )
    traversable = np.ones(cells.shape, dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=traversable,
        blocked=~traversable,
        safe=traversable,
        reachable=traversable,
        navigable=traversable,
    )
    current_pose = Pose(1.625, 1.625, math.pi / 2.0, stamp=2.0)
    anchor = {"x": 0.625, "y": 1.625, "theta": 0.0}
    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(tmp_path / "find_next_person.json")
    skill.map = map_state
    skill.pose = current_pose
    skill.image = _frame((80, 90, 100))
    skill.wait_for = lambda read, **_kwargs: read()
    skill.check_cancelled = lambda: None
    state = search_module._empty_state()
    state["map_fingerprint"] = search_module._map_fingerprint(map_state, cells)
    state["observations"] = [anchor]
    state["recovery_anchor"] = anchor
    skill.storage["state"] = state
    monkeypatch.setattr(search_module, "_build_planning_grid", lambda *_args: plan)
    monkeypatch.setattr(search_module, "_choose_view", choose_view)
    monkeypatch.setattr(skill, "_refresh_visualization", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(skill, "_fresh_upward_frame", lambda: _frame((10, 20, 30)))

    class RecoveringNavigation:
        def __init__(self):
            self.relative_calls = []

        def approach_viewpoint(self, **_kwargs):
            raise navigation_module.NavigationPathIndeterminateError("planner aborted")

        def navigate_relative(self, **kwargs):
            self.relative_calls.append(kwargs)
            skill.pose = Pose(anchor["x"], anchor["y"], anchor["theta"], stamp=3.0)

    navigation = RecoveringNavigation()
    skill.navigate = navigation
    return skill, navigation


def test_indeterminate_route_recovers_to_reached_viewpoint_without_blacklisting(tmp_path, monkeypatch):
    view = search_module._View(2, 6, 1.625, 0.625, math.pi / 2.0, frozenset(), 10, 1.0)
    skill, navigation = _search_with_pending_recovery(
        tmp_path,
        monkeypatch,
        choose_view=lambda *_args, **_kwargs: (view, set()),
    )

    output = skill.execute()

    payload = _assert_contract(output, "SEARCH_OBSERVATION")
    assert payload == {"coverage_recorded": False, "navigation": "recovered"}
    assert navigation.relative_calls == [
        {"x": pytest.approx(0.0), "y": pytest.approx(1.0), "theta_degrees": pytest.approx(-90.0)}
    ]
    assert skill.storage["state"]["recovery_anchor"] is None
    assert skill.storage["state"]["unreachable"] == []
    assert output.image is not None


def test_pending_recovery_prevents_false_low_coverage_exhaustion(tmp_path, monkeypatch):
    skill, navigation = _search_with_pending_recovery(
        tmp_path,
        monkeypatch,
        choose_view=lambda *_args, **_kwargs: (None, set()),
    )

    output = skill.execute()

    _assert_contract(output, "SEARCH_OBSERVATION", coverage_recorded=False, navigation="recovered")
    assert len(navigation.relative_calls) == 1
    assert skill.storage["state"]["recovery_anchor"] is None


def test_failed_recovery_anchor_is_replaced_by_an_untried_reached_pose(tmp_path, monkeypatch):
    skill, navigation = _search_with_pending_recovery(
        tmp_path,
        monkeypatch,
        choose_view=lambda *_args, **_kwargs: (None, set()),
    )
    failed_anchor = skill.storage["state"]["recovery_anchor"]
    alternate = {"x": 1.625, "y": 0.625, "theta": math.pi}
    state = skill.storage["state"]
    state["observations"] = [alternate, failed_anchor]
    skill.storage["state"] = state

    def fail_once_then_recover(**kwargs):
        navigation.relative_calls.append(kwargs)
        if len(navigation.relative_calls) == 1:
            raise navigation_module.NavigationNoProgressError("local recovery stalled")
        skill.pose = Pose(alternate["x"], alternate["y"], alternate["theta"], stamp=3.0)

    navigation.navigate_relative = fail_once_then_recover

    first = skill.execute()

    payload = _assert_contract(first, "SEARCH_INFRASTRUCTURE_FAILURE")
    assert "local recovery stalled" in payload["reason"]
    state = skill.storage["state"]
    assert state["recovery_anchor"] == {
        "x": alternate["x"],
        "y": alternate["y"],
        "theta": pytest.approx(alternate["theta"]),
    }
    assert state["recovery_rejections"] == [failed_anchor]

    second = skill.execute()

    _assert_contract(second, "SEARCH_OBSERVATION", coverage_recorded=False, navigation="recovered")
    assert len(navigation.relative_calls) == 2
    assert skill.storage["state"]["recovery_anchor"] is None
    assert skill.storage["state"]["recovery_rejections"] == []


def test_recovery_target_uses_nearest_safe_reached_pose():
    safe = np.ones((8, 8), dtype=bool)
    safe[2, 2] = False
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=np.ones_like(safe),
        blocked=np.zeros_like(safe),
        safe=safe,
        reachable=np.ones_like(safe),
        navigable=safe,
    )
    state = search_module._empty_state()
    state["recovery_anchor"] = {"x": 0.625, "y": 0.625, "theta": 0.0}
    state["observations"] = [
        {"x": 1.625, "y": 1.625, "theta": math.pi},
        {"x": 1.125, "y": 1.125, "theta": math.pi / 2.0},
    ]

    target = search_module._recovery_target(plan, state, Pose(1.875, 1.125, 0.0))

    assert target == pytest.approx((1.625, 1.625, math.pi))


def test_recovery_target_does_not_retry_a_rejected_position():
    safe = np.ones((8, 8), dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=safe,
        blocked=~safe,
        safe=safe,
        reachable=safe,
        navigable=safe,
    )
    rejected = {"x": 1.125, "y": 1.125, "theta": math.pi / 2.0}
    alternate = {"x": 0.625, "y": 1.625, "theta": math.pi}
    state = search_module._empty_state()
    state["recovery_anchor"] = rejected
    state["recovery_rejections"] = [rejected]
    state["observations"] = [alternate, rejected]

    target = search_module._recovery_target(plan, state, Pose(1.625, 1.625, 0.0))

    assert target == pytest.approx((alternate["x"], alternate["y"], alternate["theta"]))


def test_exploration_artifact_separates_covered_uncovered_and_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    traversable = np.zeros((7, 8), dtype=bool)
    traversable[1:6, 1:7] = True
    reachable = traversable.copy()
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=traversable,
        blocked=~traversable,
        safe=traversable,
        reachable=reachable,
        navigable=reachable,
    )
    covered = {row * plan.width + col for row in range(1, 4) for col in range(1, 4)}
    state = {
        "version": 1,
        "map_fingerprint": "test-map",
        "observations": [{"x": 0.5, "y": 0.5, "theta": 0.0}],
        "unreachable": [{"x": 1.5, "y": 1.0, "theta": 1.57}],
    }
    target = search_module._View(
        row=4,
        col=5,
        x=1.375,
        y=1.125,
        theta=3.14,
        visible=frozenset(),
        gain=7,
        score=1.0,
    )

    jpeg = search_module._coverage_artifact(
        plan,
        state,
        covered,
        Pose(0.5, 0.5, 0.0),
        status="test preview",
        target=target,
    )
    artifact_dir = tmp_path / search_module.ARTIFACT_RELATIVE_DIR
    metadata = json.loads((artifact_dir / "exploration_coverage.json").read_text())

    assert jpeg
    assert (artifact_dir / "exploration_coverage.png").is_file()
    assert metadata["coverage_fraction"] == pytest.approx(9 / 30)
    assert metadata["covered_count"] == 9
    assert len(metadata["uncovered_cells"]) == 21
    assert metadata["target"]["new_cells"] == 7


def test_clearance_filters_viewpoints_without_fragmenting_reachable_rooms():
    cells = np.full((15, 25), 100, dtype=np.int8)
    cells[2:13, 2:9] = 0
    cells[2:13, 13:23] = 0
    cells[7, 9:13] = 0
    map_state = Map(
        resolution=0.25,
        width=cells.shape[1],
        height=cells.shape[0],
        origin_x=0.0,
        origin_y=0.0,
        raw_source=SimpleNamespace(data=cells.flatten().tolist()),
    )

    plan = search_module._build_planning_grid(map_state, Pose(1.0, 1.875, 0.0))

    assert plan is not None
    assert plan.reachable[7, 17]
    assert not plan.navigable[7, 10]
    assert plan.navigable[7, 17]
    assert plan.navigable[7, 5]
    viewpoints = search_module._sample_viewpoints(plan)
    assert any(col < 9 for _row, col in viewpoints)
    assert any(col > 13 for _row, col in viewpoints)
    distances = search_module._grid_travel_distances(plan, Pose(1.0, 1.875, 0.0))
    assert math.isfinite(distances[7, 5])
    assert math.isfinite(distances[7, 17])


def test_viewpoint_sampling_is_anchored_to_the_map():
    navigable = np.zeros((9, 11), dtype=bool)
    navigable[1:8, 2:10] = True
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=-2.0,
        origin_y=-3.0,
        origin_theta=0.0,
        traversable=navigable,
        blocked=~navigable,
        safe=navigable,
        reachable=navigable,
        navigable=navigable,
    )

    viewpoints = search_module._sample_viewpoints(plan)

    assert viewpoints[0] == (4, 5)
    assert viewpoints == search_module._sample_viewpoints(plan)


def test_route_distance_follows_free_space_around_a_wall():
    reachable = np.ones((9, 9), dtype=bool)
    reachable[:8, 4] = False
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=reachable,
        blocked=~reachable,
        safe=reachable,
        reachable=reachable,
        navigable=reachable,
    )
    pose = Pose(0.625, 0.375, 0.0)  # row 1, column 2
    target = (1, 6)

    distances = search_module._grid_travel_distances(plan, pose)
    direct_distance = math.hypot(6 - 2, 1 - 1) * plan.resolution

    assert distances[target] > direct_distance * 2.5


def test_route_distance_does_not_cut_through_a_blocked_corner():
    reachable = np.array([[True, False], [True, True]], dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=1.0,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=reachable,
        blocked=~reachable,
        safe=reachable,
        reachable=reachable,
        navigable=reachable,
    )

    distances = search_module._grid_travel_distances(plan, Pose(0.5, 0.5, 0.0))

    assert distances[1, 1] == 2.0


def test_failed_goal_suppression_covers_one_viewpoint_spacing():
    failed = [{"x": -0.7134, "y": 3.2144}]

    assert search_module._too_close_to_unreachable(-0.2134, 2.9644, failed)
    assert not search_module._too_close_to_unreachable(-0.0633, 3.2144, failed)


def test_same_position_viewpoint_uses_dedicated_rotation_instead_of_path_planning():
    view = search_module._View(
        row=0,
        col=0,
        x=-4.2134,
        y=4.2144,
        theta=math.radians(45.0),
        visible=frozenset(),
        gain=10,
        score=1.0,
    )

    rotation = search_module._rotation_at_reached_viewpoint(view, Pose(-4.08, 4.27, math.radians(172.0)))

    assert rotation == -127.0


def test_viewpoint_inside_visual_standoff_uses_rotation_instead_of_noop_navigation():
    view = search_module._View(
        row=0,
        col=0,
        x=0.4,
        y=0.0,
        theta=math.pi,
        visible=frozenset(),
        gain=10,
        score=1.0,
    )

    rotation = search_module._rotation_at_reached_viewpoint(
        view,
        Pose(0.0, 0.0, 0.0),
        allow_visual_standoff=True,
    )

    assert rotation == 180.0


def test_exploration_standoff_matches_accepted_observation_geometry():
    assert search_module.VIEWPOINT_STANDOFF_M == search_module.ARRIVAL_POSITION_TOLERANCE_M == 0.5


def test_distant_viewpoint_still_requires_path_planning():
    view = search_module._View(
        row=0,
        col=0,
        x=-0.9634,
        y=-1.2856,
        theta=-math.pi / 2.0,
        visible=frozenset(),
        gain=10,
        score=1.0,
    )

    rotation = search_module._rotation_at_reached_viewpoint(view, Pose(-0.2636, 2.1983, 0.0))

    assert rotation is None


def test_visual_standoff_faces_the_blocked_target_instead_of_the_unreached_heading():
    view = search_module._View(
        row=0,
        col=0,
        x=1.0,
        y=0.0,
        theta=math.pi,
        visible=frozenset(),
        gain=10,
        score=1.0,
    )

    heading = search_module._standoff_heading(view, Pose(0.0, 0.0, math.pi / 2.0))

    assert heading == pytest.approx(0.0)


def test_visual_standoff_uses_target_bearing_inside_arrival_tolerance():
    view = search_module._View(
        row=0,
        col=0,
        x=1.0,
        y=0.0,
        theta=math.pi,
        visible=frozenset(),
        gain=10,
        score=1.0,
    )

    heading = search_module._standoff_heading(view, Pose(0.8, 0.0, math.pi / 2.0))

    assert heading == pytest.approx(0.0)


def test_dedicated_rotation_uses_root_nav2_spin_behavior():
    navigator = _VisualApproachNavigator(feedback_distances=())
    navigator.complete(navigation_module.GoalStatus.STATUS_SUCCEEDED)
    controller = _visual_approach_controller(navigator)

    controller.rotate_in_place(math.pi / 2.0)

    assert len(navigator.spin_goals) == 1
    assert navigator.spin_goals[0].target_yaw == math.pi / 2.0
    assert navigator.spin_goals[0].time_allowance.sec == 10


class _DoneFuture:
    def __init__(self, result):
        self._result = result

    def done(self):
        return True

    def result(self):
        return self._result

    def cancel(self):
        pass


class _ActionResultFuture:
    def __init__(self):
        self.status = None
        self.cancelled = False

    def done(self):
        return self.status is not None

    def result(self):
        return SimpleNamespace(status=self.status)

    def cancel(self):
        self.cancelled = True


class _GoalHandle:
    accepted = True

    def __init__(self, navigator):
        self.navigator = navigator

    def get_result_async(self):
        return self.navigator.action_result

    def cancel_goal_async(self):
        self.navigator.cancelled = True
        if self.navigator.complete_on_cancel:
            self.navigator.complete(navigation_module.GoalStatus.STATUS_CANCELED)
        return _DoneFuture(SimpleNamespace(goals_canceling=[object()]))


class _ActionClient:
    def __init__(self, navigator, goal_store):
        self.navigator = navigator
        self.goal_store = goal_store

    def wait_for_server(self, **_kwargs):
        return True

    def send_goal_async(self, goal, feedback_callback, *, goal_uuid):
        self.goal_store.append(goal)
        self.navigator.submitted_goal_uuids.append(goal_uuid)
        self.navigator.feedback_callback = feedback_callback
        return _DoneFuture(_GoalHandle(self.navigator))


class _CancelServiceClient:
    def __init__(self, navigator):
        self.navigator = navigator

    def wait_for_service(self, **_kwargs):
        return True

    def call_async(self, request):
        self.navigator.cancel_requests.append(request.goal_info.goal_id)
        if (
            self.navigator.cancel_unknown_until_send_done
            and self.navigator.pending_send_future is not None
            and not self.navigator.pending_send_future.done()
        ):
            code = navigation_module.CancelGoal.Response.ERROR_UNKNOWN_GOAL_ID
        else:
            code = (
                self.navigator.cancel_response_codes.pop(0)
                if self.navigator.cancel_response_codes
                else self.navigator.cancel_response_code
            )
        canceling = []
        if code == navigation_module.CancelGoal.Response.ERROR_NONE:
            self.navigator.cancelled = True
            if self.navigator.cancel_matches:
                canceling.append(
                    SimpleNamespace(goal_id=self.navigator.cancel_ack_goal_id or request.goal_info.goal_id)
                )
            if self.navigator.complete_on_cancel:
                self.navigator.complete(navigation_module.GoalStatus.STATUS_CANCELED)
        return _DoneFuture(SimpleNamespace(return_code=code, goals_canceling=canceling))


class _ResultServiceClient:
    def __init__(self, navigator):
        self.navigator = navigator

    def wait_for_service(self, **_kwargs):
        return True

    def call_async(self, request):
        self.navigator.result_requests.append(request.goal_id)
        return self.navigator.action_result


class _VisualApproachNavigator:
    def __init__(self, *, complete_on_cancel=True, feedback_distances=(0.75,)):
        self.cancelled = False
        self.complete_on_cancel = complete_on_cancel
        self.goals = []
        self.spin_goals = []
        self.feedback_distances = iter(feedback_distances)
        self.feedback_reads = 0
        self.feedback = None
        self.goal_handle = None
        self.result_future = None
        self.status = None
        self.action_result = _ActionResultFuture()
        self.submitted_goal_uuids = []
        self.cancel_requests = []
        self.result_requests = []
        self.cancel_response_code = navigation_module.CancelGoal.Response.ERROR_NONE
        self.cancel_response_codes = []
        self.cancel_matches = True
        self.cancel_ack_goal_id = None
        self.cancel_unknown_until_send_done = False
        self.pending_send_future = None
        self.nav_to_pose_client = _ActionClient(self, self.goals)
        self.spin_client = _ActionClient(self, self.spin_goals)
        self.cancel_service = _CancelServiceClient(self)
        self.result_service = _ResultServiceClient(self)

    def get_clock(self):
        return SimpleNamespace(now=lambda: SimpleNamespace(to_msg=TimeMsg))

    def getPath(self, *_args, **_kwargs):
        return object()

    def getFeedback(self):
        self.feedback_reads += 1
        return SimpleNamespace(distance_remaining=next(self.feedback_distances), number_of_recoveries=0)

    def _feedbackCallback(self, feedback):
        self.feedback = feedback

    def complete(self, status):
        self.action_result.status = status


class _StaleFeedbackNavigator(_VisualApproachNavigator):
    def __init__(self):
        super().__init__()
        self.feedback = SimpleNamespace(distance_remaining=0.75, number_of_recoveries=0)
        self.feedback_reads = 0

    def isTaskComplete(self):
        return self.cancelled or self.feedback_reads >= 2

    def getFeedback(self):
        self.feedback_reads += 1
        if self.feedback_reads == 1:
            return self.feedback
        self.feedback = SimpleNamespace(distance_remaining=3.0, number_of_recoveries=0)
        self.complete(navigation_module.GoalStatus.STATUS_SUCCEEDED)
        return self.feedback


class _Publisher:
    def __init__(self):
        self.messages = []

    def publish(self, message):
        self.messages.append(message)


def _install_executor_lifecycle_fakes(monkeypatch):
    events = []

    class FakeNavigator:
        instances = []

        def __init__(self, namespace):
            self.namespace = namespace
            self.context = self.__class__.context
            self._executor = None
            self.destroyed = False
            self.assisted_teleop_client = SimpleNamespace(destroy=self._destroy_assisted_teleop)
            self.__class__.instances.append(self)

        def _destroy_assisted_teleop(self):
            events.append(("client_destroy", self))

        def create_publisher(self, *_args, **_kwargs):
            return _Publisher()

        def create_client(self, service_type, service_name):
            return SimpleNamespace(service_type=service_type, service_name=service_name)

        @property
        def executor(self):
            return self._executor

        @executor.setter
        def executor(self, new_executor):
            current_executor = self._executor
            if current_executor is new_executor:
                return
            if current_executor is not None:
                current_executor.remove_node(self)
            if new_executor is not None:
                new_executor.add_node(self)
            self._executor = new_executor

        def destroy_node(self):
            self.destroyed = True
            events.append(("node_destroy", self))

    FakeNavigator.context = object()

    class FakeExecutor:
        instances = []

        def __init__(self, *, context=None):
            self.context = context
            self.added = []
            self.nodes = []
            self.removed = []
            self.spin_timeouts = []
            self.shutdown_count = 0
            self.__class__.instances.append(self)

        def add_node(self, node):
            if node in self.nodes:
                return False
            self.nodes.append(node)
            self.added.append(node)
            events.append(("executor_add", self, node))
            node.executor = self
            return True

        def spin_once(self, timeout_sec=0.0):
            self.spin_timeouts.append(timeout_sec)
            events.append(("executor_spin", self))

        def shutdown(self, timeout_sec=None):
            assert timeout_sec == navigation_module.NAVIGATION_EXECUTOR_SHUTDOWN_TIMEOUT_S
            self.shutdown_count += 1
            events.append(("executor_shutdown", self))
            self.nodes = []
            return True

        def remove_node(self, node):
            if node not in self.nodes:
                return
            self.nodes.remove(node)
            self.removed.append(node)
            events.append(("executor_remove", self, node))

    monkeypatch.setattr(navigation_module, "BasicNavigator", FakeNavigator)
    monkeypatch.setattr(navigation_module, "SingleThreadedExecutor", FakeExecutor)
    return events, FakeNavigator, FakeExecutor


def _executor_test_skill():
    return SimpleNamespace(
        logger=SimpleNamespace(
            info=lambda _message: None,
            warning=lambda _message: None,
            error=lambda _message: None,
        )
    )


def _visual_approach_controller(navigator, sleep=lambda _seconds: None, spin=None):
    controller = navigation_module.Nav2Controller.__new__(navigation_module.Nav2Controller)
    controller.navigator = navigator
    controller.navigator_mapfree = SimpleNamespace(getPath=lambda *_args, **_kwargs: object())
    controller.navigator_navigation = SimpleNamespace(getPath=lambda *_args, **_kwargs: object())
    controller._commanded_goal_pub = _Publisher()
    brakes = []
    controller.logger = SimpleNamespace(
        info=lambda _message: None,
        warning=lambda _message: None,
        error=lambda _message: None,
    )
    controller.skill = SimpleNamespace(
        sleep=sleep,
        mobility=SimpleNamespace(stop=lambda: brakes.append(True)),
    )
    controller._path_exists = lambda _navigator, _goal: True
    controller._spin_once = spin or (lambda _navigator, timeout_sec=0.0: sleep(timeout_sec))
    controller._navigate_cancel_client = navigator.cancel_service
    controller._navigate_result_client = navigator.result_service
    controller._spin_cancel_client = navigator.cancel_service
    controller._spin_result_client = navigator.result_service
    controller._active_goal_uuid = None
    controller._active_cancel_client = None
    controller._active_result_client = None
    controller._active_result_service = None
    controller._active_send_future = None
    controller._cancel_guardian = None
    controller._cancel_guardian_done = navigation_module.threading.Event()
    controller._guardian_lock = navigation_module.threading.Lock()
    controller._destroy_deferred = False
    controller._destroy_lock = navigation_module.threading.Lock()
    controller._navigators_destroyed = False
    controller.brakes = brakes
    return controller


def test_private_executors_isolate_controller_and_guardian_spins(monkeypatch):
    _events, _navigator_type, executor_type = _install_executor_lifecycle_fakes(monkeypatch)
    global_spins = []
    monkeypatch.setattr(rclpy, "spin_once", lambda *_args, **_kwargs: global_spins.append(True))

    foreground = navigation_module.Nav2Controller(_executor_test_skill())
    guarded = navigation_module.Nav2Controller(_executor_test_skill())

    assert foreground._executor is not guarded._executor
    assert foreground._executor.context is foreground.navigator.context
    assert guarded._executor.context is guarded.navigator.context
    assert foreground._executor.added == list(foreground._navigator_nodes)
    assert guarded._executor.added == list(guarded._navigator_nodes)
    assert len(executor_type.instances) == 2

    foreground._spin_once(foreground.navigator, timeout_sec=0.01)
    guarded._active_goal_uuid = navigation_module.Nav2Controller._new_goal_uuid()

    def finish_guarded_cancel(_timeout_message):
        guarded._spin_once(guarded.navigator, timeout_sec=0.02)
        guarded._clear_active_action()

    guarded._cancel_navigation_exact = finish_guarded_cancel
    guarded._start_cancel_guardian()
    assert guarded._cancel_guardian_done.wait(1.0)
    guarded._cancel_guardian.join(timeout=1.0)

    assert foreground._executor.spin_timeouts == [0.01]
    assert guarded._executor.spin_timeouts == [0.02]
    assert global_spins == []

    foreground.destroy()
    guarded.destroy()


def test_executor_shutdown_waits_for_cancellation_guardian(monkeypatch):
    events, _navigator_type, _executor_type = _install_executor_lifecycle_fakes(monkeypatch)
    controller = navigation_module.Nav2Controller(_executor_test_skill())
    executor = controller._executor
    guardian_entered = navigation_module.threading.Event()
    release_guardian = navigation_module.threading.Event()
    controller._active_goal_uuid = navigation_module.Nav2Controller._new_goal_uuid()

    def finish_after_release(_timeout_message):
        controller._spin_once(controller.navigator, timeout_sec=0.03)
        controller._clear_active_action()
        guardian_entered.set()
        release_guardian.wait(1.0)

    controller._cancel_navigation_exact = finish_after_release
    controller._start_cancel_guardian()
    assert guardian_entered.wait(1.0)

    controller.destroy()

    assert controller._destroy_deferred is True
    assert executor.shutdown_count == 0
    assert executor.removed == []
    assert not any(navigator.destroyed for navigator in controller._navigator_nodes)

    release_guardian.set()
    assert controller._cancel_guardian_done.wait(1.0)
    controller._cancel_guardian.join(timeout=1.0)

    assert not controller._cancel_guardian.is_alive()
    assert executor.shutdown_count == 1
    assert executor.removed == list(reversed(controller._navigator_nodes))
    assert all(navigator.destroyed for navigator in controller._navigator_nodes)

    event_names = [event[0] for event in events]
    shutdown_index = event_names.index("executor_shutdown")
    remove_indexes = [index for index, name in enumerate(event_names) if name == "executor_remove"]
    destroy_indexes = [index for index, name in enumerate(event_names) if name == "node_destroy"]
    assert max(remove_indexes) < shutdown_index
    assert shutdown_index < min(destroy_indexes)


def test_visual_approach_stops_before_nav2_recovery():
    navigator = _VisualApproachNavigator(feedback_distances=(1.31, 1.29))
    controller = _visual_approach_controller(navigator)

    stopped_short = controller.go_to_position(1.0, 2.0, 0.0, False, stop_within_m=1.3)

    assert stopped_short is True
    assert navigator.cancelled is True
    assert navigator.feedback_reads == 2
    assert len(navigator.goals) == 1
    assert len(controller._commanded_goal_pub.messages) == 1


def test_visual_approach_cancel_is_bounded(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation_module.time, "monotonic", lambda: now[0])
    navigator = _VisualApproachNavigator(complete_on_cancel=False)
    controller = _visual_approach_controller(navigator, lambda seconds: now.__setitem__(0, now[0] + seconds))

    with pytest.raises(navigation_module.SkillFailed, match="did not acknowledge"):
        controller.go_to_position(1.0, 2.0, 0.0, False, stop_within_m=0.8)

    assert navigator.cancelled is True
    navigator.complete_on_cancel = True
    assert controller._cancel_guardian_done.wait(1.0)


@pytest.mark.parametrize("acknowledgement", ["empty", "mismatched"])
def test_invalid_cancel_acknowledgement_fails_and_brakes(acknowledgement):
    navigator = _VisualApproachNavigator()
    if acknowledgement == "empty":
        navigator.cancel_matches = False
    else:
        navigator.cancel_ack_goal_id = navigation_module.Nav2Controller._new_goal_uuid()
    controller = _visual_approach_controller(navigator)

    with pytest.raises(navigation_module.NavigationInfrastructureError, match="empty or mismatched"):
        controller.go_to_position(1.0, 2.0, 0.0, False, stop_within_m=0.8)

    assert navigator.cancelled is True
    navigator.cancel_matches = True
    navigator.cancel_ack_goal_id = None
    assert controller._cancel_guardian_done.wait(1.0)
    assert controller.brakes
    assert controller._active_goal_uuid is None
    controller._cancel_guardian.join(timeout=1.0)
    assert not controller._cancel_guardian.is_alive()


def test_unknown_goal_cancel_retries_same_uuid_until_acknowledged(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation_module.time, "monotonic", lambda: now[0])
    navigator = _VisualApproachNavigator()
    navigator.cancel_response_codes = [
        navigation_module.CancelGoal.Response.ERROR_UNKNOWN_GOAL_ID,
        navigation_module.CancelGoal.Response.ERROR_UNKNOWN_GOAL_ID,
        navigation_module.CancelGoal.Response.ERROR_NONE,
    ]
    controller = _visual_approach_controller(navigator, lambda seconds: now.__setitem__(0, now[0] + seconds))

    stopped_short = controller.go_to_position(1.0, 2.0, 0.0, False, stop_within_m=0.8)

    assert stopped_short is True
    assert len(navigator.cancel_requests) == 3
    assert all(
        bytes(goal_uuid.uuid) == bytes(navigator.submitted_goal_uuids[0].uuid)
        for goal_uuid in navigator.cancel_requests
    )
    assert len(controller.brakes) == 2


def test_stop_is_not_reported_cancelled_when_nav2_cancel_is_unconfirmed(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation_module.time, "monotonic", lambda: now[0])
    navigator = _VisualApproachNavigator()
    navigator.cancel_matches = False
    cancel_latched = [False]

    def cancel_on_first_sleep(seconds):
        if seconds > 0.0 and not cancel_latched[0]:
            cancel_latched[0] = True
            raise navigation_module.SkillCancelled("stop requested")
        now[0] += seconds

    controller = _visual_approach_controller(
        navigator,
        cancel_on_first_sleep,
        spin=lambda _navigator, timeout_sec=0.0: now.__setitem__(0, now[0] + timeout_sec),
    )

    with pytest.raises(navigation_module.NavigationInfrastructureError, match="empty or mismatched"):
        controller.go_to_position(1.0, 2.0, 0.0, False)

    navigator.cancel_matches = True
    assert controller._cancel_guardian_done.wait(1.0)
    assert controller.brakes


def test_goal_response_timeout_resolves_and_cancels_a_late_acceptance(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(navigation_module, "NAVIGATION_GOAL_RESPONSE_TIMEOUT_S", 0.1)
    navigator = _VisualApproachNavigator()
    late_future = SimpleNamespace(cancelled=False)
    late_future.done = lambda: now[0] >= 0.15
    late_future.result = lambda: _GoalHandle(navigator)
    late_future.cancel = lambda: setattr(late_future, "cancelled", True)

    def delayed_send(_goal, _feedback, *, goal_uuid):
        navigator.submitted_goal_uuids.append(goal_uuid)
        return late_future

    navigator.nav_to_pose_client.send_goal_async = delayed_send
    controller = _visual_approach_controller(navigator, lambda seconds: now.__setitem__(0, now[0] + seconds))

    with pytest.raises(navigation_module.NavigationInfrastructureError, match="did not accept"):
        controller.go_to_position(1.0, 2.0, 0.0, False)

    assert late_future.cancelled is False
    assert navigator.cancelled is True
    assert len(navigator.submitted_goal_uuids) == 1
    assert bytes(navigator.cancel_requests[0].uuid) == bytes(navigator.submitted_goal_uuids[0].uuid)
    assert bytes(navigator.result_requests[0].uuid) == bytes(navigator.submitted_goal_uuids[0].uuid)


def test_guardian_cancels_goal_accepted_after_foreground_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(navigation_module, "NAVIGATION_GOAL_RESPONSE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(navigation_module, "NAVIGATION_CANCEL_TIMEOUT_S", 0.2)
    navigator = _VisualApproachNavigator()
    late_future = SimpleNamespace(cancelled=False)
    late_future.done = lambda: now[0] >= 0.55
    late_future.result = lambda: _GoalHandle(navigator)
    late_future.cancel = lambda: setattr(late_future, "cancelled", True)
    navigator.pending_send_future = late_future
    navigator.cancel_unknown_until_send_done = True

    def delayed_send(_goal, _feedback, *, goal_uuid):
        navigator.submitted_goal_uuids.append(goal_uuid)
        return late_future

    navigator.nav_to_pose_client.send_goal_async = delayed_send
    controller = _visual_approach_controller(navigator, lambda seconds: now.__setitem__(0, now[0] + seconds))

    with pytest.raises(navigation_module.NavigationInfrastructureError, match="timed-out-goal cancellation"):
        controller.go_to_position(1.0, 2.0, 0.0, False)

    assert controller._cancel_guardian_done.wait(1.0)
    assert controller._active_goal_uuid is None
    assert navigator.cancelled is True
    assert now[0] >= 0.55
    assert len(navigator.cancel_requests) > 1
    assert all(
        bytes(goal_uuid.uuid) == bytes(navigator.submitted_goal_uuids[0].uuid)
        for goal_uuid in navigator.cancel_requests
    )


def test_visual_approach_ignores_previous_goal_feedback():
    navigator = _StaleFeedbackNavigator()
    controller = _visual_approach_controller(navigator)

    stopped_short = controller.go_to_position(4.0, 2.0, 0.0, False, stop_within_m=0.8)

    assert stopped_short is False
    assert navigator.feedback_reads == 2
    assert navigator.cancelled is False


def test_navigation_without_progress_is_cancelled_quickly(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(navigation_module, "NAVIGATION_STALL_TIMEOUT_S", 0.3)

    class StalledNavigator(_VisualApproachNavigator):
        def __init__(self):
            super().__init__()

        def isTaskComplete(self):
            return self.cancelled

        def getFeedback(self):
            self.feedback_reads += 1
            return SimpleNamespace(distance_remaining=3.0, number_of_recoveries=1)

    navigator = StalledNavigator()
    controller = _visual_approach_controller(navigator, lambda seconds: now.__setitem__(0, now[0] + seconds))

    with pytest.raises(navigation_module.SkillFailed, match="no route progress"):
        controller.go_to_position(4.0, 2.0, 0.0, False)

    assert navigator.cancelled is True


def test_path_preflight_timeout_is_bounded(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(navigation_module, "PATH_RESULT_TIMEOUT_S", 0.1)

    class PendingFuture:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    future = PendingFuture()
    navigator = SimpleNamespace(
        compute_path_to_pose_client=SimpleNamespace(
            wait_for_server=lambda **_kwargs: True,
            send_goal_async=lambda _goal: future,
        )
    )
    controller = navigation_module.Nav2Controller.__new__(navigation_module.Nav2Controller)
    controller.logger = SimpleNamespace(info=lambda _message: None)
    controller.skill = SimpleNamespace(sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    controller._spin_once = lambda _navigator, timeout_sec=0.0: None

    with pytest.raises(navigation_module.NavigationInfrastructureError, match="did not respond within"):
        controller._path_exists(navigator, navigation_module.PoseStamped())

    assert future.cancelled is True


def test_path_result_timeout_cancels_the_accepted_goal(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(navigation_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(navigation_module, "PATH_RESULT_TIMEOUT_S", 0.1)

    class DoneFuture:
        def __init__(self, result):
            self._result = result

        def done(self):
            return True

        def result(self):
            return self._result

    class PendingFuture:
        def done(self):
            return False

    result_future = PendingFuture()
    cancelled = []
    goal_handle = SimpleNamespace(
        accepted=True,
        get_result_async=lambda: result_future,
        cancel_goal_async=lambda: cancelled.append(True),
    )
    navigator = SimpleNamespace(
        compute_path_to_pose_client=SimpleNamespace(
            wait_for_server=lambda **_kwargs: True,
            send_goal_async=lambda _goal: DoneFuture(goal_handle),
        )
    )
    controller = navigation_module.Nav2Controller.__new__(navigation_module.Nav2Controller)
    controller.logger = SimpleNamespace(info=lambda _message: None)
    controller.skill = SimpleNamespace(sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    controller._spin_once = lambda _navigator, timeout_sec=0.0: None

    with pytest.raises(navigation_module.NavigationInfrastructureError, match="did not respond within"):
        controller._path_exists(navigator, navigation_module.PoseStamped())

    assert cancelled == [True]


@pytest.mark.parametrize(
    ("failure_site", "message"),
    [
        ("send_result", "goal request failed"),
        ("get_result_async", "result request failed"),
        ("result_result", "planner result failed"),
    ],
)
def test_path_preflight_wraps_future_exceptions(failure_site, message):
    class DoneFuture:
        def __init__(self, result, *, raises=False):
            self._result = result
            self._raises = raises

        def done(self):
            return True

        def result(self):
            if self._raises:
                raise RuntimeError("transport broke")
            return self._result

    result_future = DoneFuture(
        SimpleNamespace(status=navigation_module.GoalStatus.STATUS_SUCCEEDED),
        raises=failure_site == "result_result",
    )

    class GoalHandle:
        accepted = True

        def get_result_async(self):
            if failure_site == "get_result_async":
                raise RuntimeError("transport broke")
            return result_future

    send_future = DoneFuture(GoalHandle(), raises=failure_site == "send_result")
    navigator = SimpleNamespace(
        compute_path_to_pose_client=SimpleNamespace(
            wait_for_server=lambda **_kwargs: True,
            send_goal_async=lambda _goal: send_future,
        )
    )
    controller = navigation_module.Nav2Controller.__new__(navigation_module.Nav2Controller)
    controller.logger = SimpleNamespace(info=lambda _message: None)
    controller.skill = SimpleNamespace(sleep=lambda _seconds: None)

    with pytest.raises(navigation_module.NavigationInfrastructureError, match=message) as caught:
        controller._path_exists(navigator, navigation_module.PoseStamped())

    assert isinstance(caught.value.__cause__, RuntimeError)


def test_path_preflight_accepts_a_successful_planner_result():
    class DoneFuture:
        def __init__(self, result):
            self._result = result

        def done(self):
            return True

        def result(self):
            return self._result

    result_future = DoneFuture(SimpleNamespace(status=navigation_module.GoalStatus.STATUS_SUCCEEDED))
    goal_handle = SimpleNamespace(accepted=True, get_result_async=lambda: result_future)
    navigator = SimpleNamespace(
        compute_path_to_pose_client=SimpleNamespace(
            wait_for_server=lambda **_kwargs: True,
            send_goal_async=lambda _goal: DoneFuture(goal_handle),
        )
    )
    controller = navigation_module.Nav2Controller.__new__(navigation_module.Nav2Controller)
    controller.logger = SimpleNamespace(info=lambda _message: None)
    controller.skill = SimpleNamespace(sleep=lambda _seconds: None)

    assert controller._path_exists(navigator, navigation_module.PoseStamped()) is True


def test_path_preflight_treats_a_completed_abort_as_indeterminate():
    class DoneFuture:
        def __init__(self, result):
            self._result = result

        def done(self):
            return True

        def result(self):
            return self._result

    result_future = DoneFuture(SimpleNamespace(status=navigation_module.GoalStatus.STATUS_ABORTED))
    goal_handle = SimpleNamespace(accepted=True, get_result_async=lambda: result_future)
    navigator = SimpleNamespace(
        compute_path_to_pose_client=SimpleNamespace(
            wait_for_server=lambda **_kwargs: True,
            send_goal_async=lambda _goal: DoneFuture(goal_handle),
        )
    )
    controller = navigation_module.Nav2Controller.__new__(navigation_module.Nav2Controller)
    controller.logger = SimpleNamespace(info=lambda _message: None)
    controller.skill = SimpleNamespace(sleep=lambda _seconds: None)

    with pytest.raises(navigation_module.NavigationPathIndeterminateError, match="aborted"):
        controller._path_exists(navigator, navigation_module.PoseStamped())


def test_choose_view_penalizes_a_winding_route(monkeypatch):
    reachable = np.ones((6, 7), dtype=bool)
    reachable[:5, 3] = False
    plan = search_module._PlanningGrid(
        resolution=1.0,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=reachable,
        blocked=~reachable,
        safe=reachable,
        reachable=reachable,
        navigable=reachable,
    )
    same_side = (4, 1)
    across_wall = (1, 5)
    monkeypatch.setattr(search_module, "_sample_viewpoints", lambda _plan: [across_wall, same_side])

    def visible(_plan, row, col, _theta):
        if (row, col) == across_wall:
            return frozenset(range(60))
        return frozenset(range(100, 140))

    monkeypatch.setattr(search_module, "_visible_cells", visible)

    view, _covered = search_module._choose_view(plan, Pose(1.5, 1.5, 0.0), [], [])

    assert view is not None
    assert (view.row, view.col) == same_side


def test_sweep_information_utility_prefers_efficient_gain_over_long_detour():
    nearby = search_module._coverage_travel_utility(40, 2.0, sweeping=True)
    distant = search_module._coverage_travel_utility(50, 8.0, sweeping=True)

    assert nearby > distant
    assert search_module._coverage_travel_utility(50, 0.0, sweeping=True) == 50


def test_backtrack_penalty_prefers_continuing_an_exploration_leg():
    observations = [{"x": 0.0, "y": 0.0}, {"x": 2.0, "y": 0.0}]

    assert search_module._backtrack_penalty(4.0, 0.0, observations) == 0.0
    assert search_module._backtrack_penalty(0.0, 0.0, observations) == pytest.approx(
        search_module.SWEEP_BACKTRACK_PENALTY_CELLS
    )
    assert 0.0 < search_module._backtrack_penalty(1.0, 1.0, observations) < search_module.SWEEP_BACKTRACK_PENALTY_CELLS


def test_choose_view_avoids_looking_toward_a_handled_person(monkeypatch):
    reachable = np.ones((10, 10), dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=1.0,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=reachable,
        blocked=~reachable,
        safe=reachable,
        reachable=reachable,
        navigable=reachable,
    )
    toward_person = (2, 2)
    away_from_person = (2, 4)
    monkeypatch.setattr(search_module, "_sample_viewpoints", lambda _plan: [toward_person, away_from_person])
    monkeypatch.setattr(search_module, "_grid_travel_distances", lambda _plan, _pose: np.zeros((10, 10)))

    def visible(_plan, row, col, _theta):
        if (row, col) == toward_person:
            return frozenset(range(60))  # includes handled-person cell 22 and has higher raw gain
        return frozenset(range(40, 95))

    monkeypatch.setattr(search_module, "_visible_cells", visible)

    view, _covered = search_module._choose_view(
        plan,
        Pose(3.5, 2.5, 0.0),
        [],
        [],
        handled_person_anchors=[(2.5, 2.5)],
    )

    assert view is not None
    assert (view.row, view.col) == away_from_person


def test_choose_view_does_not_repeat_a_target_already_observed_from_standoff(monkeypatch):
    traversable = np.ones((6, 6), dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=1.0,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=traversable,
        blocked=~traversable,
        safe=traversable,
        reachable=traversable,
        navigable=traversable,
    )
    stopped_target = (2, 2)
    alternative = (2, 4)
    monkeypatch.setattr(search_module, "_sample_viewpoints", lambda _plan: [stopped_target, alternative])

    def visible(_plan, row, col, _theta):
        if (row, col) == stopped_target:
            return frozenset(range(20))
        if (row, col) == alternative:
            return frozenset(range(100, 110))
        return frozenset()

    monkeypatch.setattr(search_module, "_visible_cells", visible)
    observations = [
        {
            "x": 0.5,
            "y": 0.5,
            "theta": 0.0,
            "target_x": 2.5,
            "target_y": 2.5,
            "target_theta": 0.0,
            "stopped_short": True,
        }
    ]

    view, _covered = search_module._choose_view(plan, Pose(0.5, 0.5, 0.0), observations, [])

    assert view is not None
    assert (view.row, view.col) == alternative


def test_dense_neighbor_does_not_reselect_fake_gain_from_unreached_pose(monkeypatch):
    traversable = np.ones((1, 3), dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=traversable,
        blocked=~traversable,
        safe=traversable,
        reachable=traversable,
        navigable=traversable,
    )
    current = (0, 0)
    nearby_target = (0, 1)
    monkeypatch.setattr(search_module, "_sample_viewpoints", lambda _plan: [nearby_target])
    monkeypatch.setattr(search_module, "_dense_viewpoint_lattice", lambda *_args: [])

    def visible(_plan, row, col, _theta):
        if (row, col) == nearby_target:
            # A neighboring dense target can promise coverage from a pose the
            # robot did not reach. The scorer must instead use the actual pose.
            return frozenset(range(100, 120))
        if (row, col) == current:
            return frozenset(range(6))
        return frozenset()

    monkeypatch.setattr(search_module, "_visible_cells", visible)
    observations = [{"x": 0.125, "y": 0.125, "theta": 0.0}]

    view, covered = search_module._choose_view(plan, Pose(0.125, 0.125, 0.0), observations, [])

    assert covered == set(range(6))
    assert view is None


def test_dense_viewpoint_lattice_is_deterministic_and_bounded(monkeypatch):
    navigable = np.ones((20, 20), dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=navigable,
        blocked=~navigable,
        safe=navigable,
        reachable=navigable,
        navigable=navigable,
    )
    sparse = [(0, 0), (19, 19)]
    monkeypatch.setattr(search_module, "MAX_VIEWPOINTS", 17)

    first = search_module._dense_viewpoint_lattice(plan, sparse)
    second = search_module._dense_viewpoint_lattice(plan, sparse)

    assert first == second
    assert len(first) == 17
    assert len(set(first)) == 17
    assert not set(first).intersection(sparse)
    assert all(plan.navigable[row, col] for row, col in first)


def test_dense_fallback_finds_interstitial_view_when_sparse_anchors_are_suppressed(monkeypatch):
    navigable = np.ones((1, 7), dtype=bool)
    plan = search_module._PlanningGrid(
        resolution=0.25,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        traversable=navigable,
        blocked=~navigable,
        safe=navigable,
        reachable=navigable,
        navigable=navigable,
    )
    sparse = [(0, 0), (0, 6)]
    monkeypatch.setattr(search_module, "_sample_viewpoints", lambda _plan: sparse)
    monkeypatch.setattr(search_module, "_visible_cells", lambda *_args: frozenset(range(10)))
    cancellation_checks = []

    view, _covered = search_module._choose_view(
        plan,
        Pose(0.875, 0.125, 0.0),
        [],
        [
            {"x": 0.125, "y": 0.125},
            {"x": 1.625, "y": 0.125},
        ],
        cancellation_check=lambda: cancellation_checks.append(True),
    )

    assert view is not None
    assert (view.row, view.col) == (0, 3)
    assert (view.x, view.y) == pytest.approx((0.875, 0.125))
    assert len(cancellation_checks) == 2


@pytest.mark.parametrize(
    ("pose_args", "expected"),
    [
        ((-4.40, -0.17, 0.0), (30, 27, -4.2134, 0.2144, 90.0)),
        ((-3.2848, 4.4725, -3.0302), (45, 27, -4.2134, 3.9644, -90.0)),
    ],
)
def test_apartment_initial_sparse_choices_do_not_use_dense_fallback(monkeypatch, pose_args, expected):
    map_state = _apartment_map_state()
    pose = Pose(*pose_args)
    plan = search_module._build_planning_grid(map_state, pose)
    monkeypatch.setattr(
        search_module,
        "_dense_viewpoint_lattice",
        lambda *_args: pytest.fail("dense fallback should not run when a sparse candidate is viable"),
    )

    assert plan is not None
    view, _covered = search_module._choose_view(plan, pose, [], [])

    assert view is not None
    assert (view.row, view.col) == expected[:2]
    assert (view.x, view.y) == pytest.approx(expected[2:4], abs=0.0001)
    assert math.degrees(view.theta) == pytest.approx(expected[4])


def test_apartment_search_continues_after_the_first_corridor_observation():
    map_state = _apartment_map_state()
    pose = Pose(-3.2848, 4.4725, -3.0302)
    plan = search_module._build_planning_grid(map_state, pose)

    assert plan is not None
    assert int(plan.reachable.sum()) == 358
    assert int(plan.navigable.sum()) == 89
    view, covered = search_module._choose_view(
        plan,
        pose,
        [{"x": -3.2848, "y": 4.4725, "theta": -3.0302}],
        [],
    )
    assert search_module._coverage_fraction(plan, covered) == pytest.approx(46 / 358)
    assert view is not None


def test_viewpoint_sampling_is_stable_on_the_apartment_map():
    map_state = _apartment_map_state()
    first_plan = search_module._build_planning_grid(map_state, Pose(-4.40, -0.17, 0.0))
    distant_plan = search_module._build_planning_grid(map_state, Pose(-3.2848, 4.4725, -3.0302))

    assert first_plan is not None
    assert distant_plan is not None
    viewpoints = search_module._sample_viewpoints(first_plan)
    assert viewpoints == search_module._sample_viewpoints(distant_plan)
    assert len(viewpoints) == 15
    assert viewpoints[0] == (41, 34)

    for exterior_goal in ((-5.9634, -3.7856), (-5.2134, -3.7856), (-4.93, -1.35)):
        assert not first_plan.traversable[search_module._world_to_cell(first_plan, *exterior_goal)]
    for resident in ((-4.59, 4.34), (-0.74, -2.76), (-0.64, 2.89)):
        assert first_plan.reachable[search_module._world_to_cell(first_plan, *resident)]

    minimum_spacing = min(
        math.hypot(row_a - row_b, col_a - col_b) * first_plan.resolution
        for index, (row_a, col_a) in enumerate(viewpoints)
        for row_b, col_b in viewpoints[index + 1 :]
    )
    maximum_anchor_distance = max(
        min(math.hypot(row - anchor_row, col - anchor_col) for anchor_row, anchor_col in viewpoints)
        * first_plan.resolution
        for row, col in np.argwhere(first_plan.navigable)
    )
    assert minimum_spacing >= 0.75
    assert maximum_anchor_distance <= 0.71


def test_apartment_route_cost_exposes_the_long_path_hidden_by_euclidean_distance():
    map_state = _apartment_map_state()
    pose = Pose(-0.2636, 2.1983, 0.0)
    target_x, target_y = -0.9634, -1.2856
    plan = search_module._build_planning_grid(map_state, pose)

    assert plan is not None
    target = search_module._world_to_cell(plan, target_x, target_y)
    route_distance = search_module._grid_travel_distances(plan, pose)[target]
    direct_distance = math.hypot(target_x - pose.x, target_y - pose.y)

    assert direct_distance == pytest.approx(3.55, abs=0.02)
    assert route_distance == pytest.approx(10.81, abs=0.15)
    assert route_distance > direct_distance * 3.0


def test_apartment_search_prefers_efficient_unseen_view_after_two_stops():
    map_state = _apartment_map_state()
    pose = Pose(-1.93, 2.71, math.radians(-8.0))
    observations = [
        {"x": -4.3819, "y": -0.1873, "theta": 1.56705},
        {"x": -4.4095, "y": 2.6999, "theta": -0.06654},
    ]
    plan = search_module._build_planning_grid(map_state, pose)

    assert plan is not None
    view, _covered = search_module._choose_view(plan, pose, observations, [])
    assert view is not None
    route_distances = search_module._grid_travel_distances(plan, pose)
    old_far_target = search_module._world_to_cell(plan, -0.2134, -2.7856)
    assert route_distances[view.row, view.col] < route_distances[old_far_target]
    assert view.gain >= search_module.MIN_NEW_CELLS


def test_visualize_saves_map_without_camera_or_safe_viewpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    cells = np.zeros((2, 2), dtype=np.int8)
    map_state = Map(
        resolution=0.25,
        width=2,
        height=2,
        origin_x=0.0,
        origin_y=0.0,
        raw_source=SimpleNamespace(data=cells.flatten().tolist()),
    )
    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(tmp_path / "workspace" / "skill_storage" / "find_next_person.json")
    skill.map = map_state
    skill.pose = Pose(0.125, 0.125, 0.0)
    skill.image = None
    skill.wait_for = lambda read, **_kwargs: read()

    output = skill.execute(visualize=True)

    artifact_dir = tmp_path / search_module.ARTIFACT_RELATIVE_DIR
    metadata = json.loads((artifact_dir / "exploration_coverage.json").read_text())
    payload = _assert_contract(output, "SEARCH_VISUALIZATION", artifact_available=True)
    assert payload["coverage_fraction"] == 0.0
    assert metadata["status"] == "preview; no safe reachable viewpoint"
    assert (artifact_dir / "exploration_coverage.png").is_file()


def test_visualize_reports_artifact_failure_truthfully(tmp_path, monkeypatch):
    cells = np.zeros((2, 2), dtype=np.int8)
    map_state = Map(
        resolution=0.25,
        width=2,
        height=2,
        origin_x=0.0,
        origin_y=0.0,
        raw_source=SimpleNamespace(data=cells.flatten().tolist()),
    )
    skill = search_module.FindNextPerson(None)
    skill._storage = SkillStorage(tmp_path / "find_next_person.json")
    skill.map = map_state
    skill.pose = Pose(0.125, 0.125, 0.0)
    skill.image = None
    skill.wait_for = lambda read, **_kwargs: read()
    monkeypatch.setattr(skill, "_refresh_visualization", lambda *_args, **_kwargs: None)

    output = skill.execute(visualize=True)

    _assert_contract(output, "SEARCH_VISUALIZATION", artifact_available=False)
    assert output.image is None
