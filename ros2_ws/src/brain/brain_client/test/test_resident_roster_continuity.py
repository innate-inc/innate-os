# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Focused regressions for resident continuity-hint authority."""

import io
import json
import math
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from brain_client.skills.types import SkillStorage
from brain_client.state.image import MainImage
from brain_client.state.pose import Pose

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "workspace"))

from innate_skills import resident_roster as roster_module  # noqa: E402


def _frame(color: tuple[int, int, int]) -> MainImage:
    image = Image.new("RGB", (96, 72), color)
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG")
    return MainImage.from_jpeg(encoded.getvalue())


def _assert_contract(output, code: str, **expected) -> dict:
    actual_code, encoded = output.message.split(" ", 1)
    payload = json.loads(encoded)
    assert actual_code == code
    assert output.data == payload
    for key, value in expected.items():
        assert payload[key] == value
    return payload


@pytest.fixture
def roster(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    skill = roster_module.ResidentRoster(None)
    skill._storage = SkillStorage(tmp_path / "resident_roster.json")
    skill._gemini_client = object()
    return skill


def _state_with_blake(
    reference: MainImage,
    *,
    active: bool = True,
    identified_at: float | None = None,
    identified_pose: Pose | None = None,
) -> dict:
    state = roster_module.ResidentRoster._fresh_state()
    state["next_sequence"] = 3
    state["encounters"] = [
        {
            "encounter_id": "resident-002",
            "name": "Blake",
            "status": "confirmed",
            "order": "A complete order",
            "reference_images_b64": [str(reference)],
        }
    ]
    if active:
        state["active_encounter_id"] = "resident-002"
        state["active_observation_image_b64"] = str(reference)
        state["active_identified_at"] = time.time() if identified_at is None else identified_at
        state["active_identified_pose"] = roster_module._pose_record(identified_pose)
    return state


def test_continuity_hint_allows_immediate_point_eight_metre_reframe(roster, monkeypatch):
    reference = _frame((130, 40, 40))
    current = _frame((40, 130, 40))
    roster.storage["state"] = _state_with_blake(
        reference,
        identified_pose=Pose(1.0, 2.0, 0.0),
    )
    roster.pose = Pose(1.8, 2.0, math.radians(20.0))
    roster._fresh_upward_frame = lambda: (current, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "uncertain",
                "encounter_id": None,
                "plausible_encounter_ids": [],
                "confidence": 0.55,
                "view_quality": "good",
                "reason": "same local approach but visual evidence is inconclusive",
            }
        ),
    )

    output = roster._identify("resident-002")
    saved = roster._load_state()

    _assert_contract(output, "UNCERTAIN_PERSON", encounter_id="resident-002", name="Blake")
    assert output.image == current.jpeg
    assert saved["active_encounter_id"] == "resident-002"
    assert saved["active_identified_pose"] == roster_module._pose_record(roster.pose)


def test_continuity_hint_preserves_sole_plausible_self(roster, monkeypatch):
    reference = _frame((130, 40, 40))
    current = _frame((40, 130, 40))
    roster.storage["state"] = _state_with_blake(
        reference,
        identified_pose=Pose(1.0, 2.0, 0.0),
    )
    roster.pose = Pose(1.8, 2.0, math.radians(20.0))
    roster._fresh_upward_frame = lambda: (current, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "uncertain",
                "encounter_id": None,
                "plausible_encounter_ids": ["resident-002"],
                "confidence": 0.61,
                "view_quality": "good",
                "reason": "resident-002 is the only plausible profile",
            }
        ),
    )

    output = roster._identify("resident-002")
    saved = roster._load_state()

    _assert_contract(output, "UNCERTAIN_PERSON", encounter_id="resident-002", name="Blake")
    assert output.image == current.jpeg
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-002"]
    assert saved["pending"] == []
    assert saved["active_encounter_id"] == "resident-002"


@pytest.mark.parametrize(
    "plausible_ids",
    [
        ["resident-001"],
        ["resident-002", "resident-001"],
    ],
)
def test_continuity_hint_does_not_override_another_plausible_profile(roster, monkeypatch, plausible_ids):
    alex = _frame((40, 40, 130))
    blake = _frame((130, 40, 40))
    current = _frame((40, 130, 40))
    state = _state_with_blake(blake, identified_pose=Pose(1.0, 2.0, 0.0))
    state["encounters"].insert(
        0,
        {
            "encounter_id": "resident-001",
            "name": "Alex",
            "status": "confirmed",
            "order": "Another complete order",
            "reference_images_b64": [str(alex)],
        },
    )
    roster.storage["state"] = state
    roster.pose = Pose(1.8, 2.0, math.radians(20.0))
    roster._fresh_upward_frame = lambda: (current, None)
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: json.dumps(
            {
                "decision": "uncertain",
                "encounter_id": None,
                "plausible_encounter_ids": plausible_ids,
                "confidence": 0.61,
                "view_quality": "good",
                "reason": "another saved profile remains plausible",
            }
        ),
    )

    output = roster._identify("resident-002")
    saved = roster._load_state()

    _assert_contract(output, "AMBIGUOUS_PERSON", plausible_encounter_ids=plausible_ids)
    assert [entry["encounter_id"] for entry in saved["encounters"]] == ["resident-001", "resident-002"]
    assert saved["pending"] == []
    assert saved["active_encounter_id"] is None


@pytest.mark.parametrize(
    ("case", "active", "identified_at", "identified_pose", "current_pose"),
    [
        ("cleared", False, None, None, Pose(1.0, 2.0, 0.0)),
        (
            "stale",
            True,
            time.time() - roster_module.IDENTITY_BINDING_TTL_S - 1.0,
            Pose(1.0, 2.0, 0.0),
            Pose(1.8, 2.0, 0.0),
        ),
        ("multi-metre", True, None, Pose(1.0, 2.0, 0.0), Pose(3.5, 2.0, 0.0)),
        ("turned-away", True, None, Pose(1.0, 2.0, 0.0), Pose(1.2, 2.0, math.radians(90.0))),
    ],
)
def test_invalid_continuity_hint_is_rejected_before_camera_or_model(
    roster,
    monkeypatch,
    case,
    active,
    identified_at,
    identified_pose,
    current_pose,
):
    reference = _frame((130, 40, 40))
    roster.storage["state"] = _state_with_blake(
        reference,
        active=active,
        identified_at=identified_at,
        identified_pose=identified_pose,
    )
    roster.pose = current_pose
    roster._fresh_upward_frame = lambda: pytest.fail(f"{case} hint read the camera")
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: pytest.fail(f"{case} hint called the identity model"),
    )

    output = roster._identify("resident-002")
    saved = roster._load_state()

    assert _assert_contract(
        output,
        "IDENTITY_UNAVAILABLE",
        reason="continuity_context_unavailable",
        encounter_id="resident-002",
    ) == {
        "reason": "continuity_context_unavailable",
        "encounter_id": "resident-002",
    }
    assert output.image is None
    assert saved["active_encounter_id"] is None
    assert saved["active_identified_at"] is None
    assert [entry["name"] for entry in saved["encounters"]] == ["Blake"]
    assert saved["pending"] == []


def test_continuity_hint_must_match_the_immediately_active_encounter(roster, monkeypatch):
    blake = _frame((130, 40, 40))
    alex = _frame((40, 130, 40))
    state = _state_with_blake(blake, active=False)
    state["encounters"].insert(
        0,
        {
            "encounter_id": "resident-001",
            "name": "Alex",
            "status": "confirmed",
            "order": "Another complete order",
            "reference_images_b64": [str(alex)],
        },
    )
    state["active_encounter_id"] = "resident-001"
    state["active_observation_image_b64"] = str(alex)
    state["active_identified_at"] = time.time()
    state["active_identified_pose"] = roster_module._pose_record(Pose(1.0, 2.0, 0.0))
    roster.storage["state"] = state
    roster.pose = Pose(1.2, 2.0, 0.0)
    roster._fresh_upward_frame = lambda: pytest.fail("mismatched hint read the camera")
    monkeypatch.setattr(
        roster_module.gemlib,
        "ask_image",
        lambda *_args, **_kwargs: pytest.fail("mismatched hint called the identity model"),
    )

    output = roster._identify("resident-002")
    saved = roster._load_state()

    _assert_contract(
        output,
        "IDENTITY_UNAVAILABLE",
        reason="continuity_context_unavailable",
        encounter_id="resident-002",
    )
    assert output.image is None
    assert saved["active_encounter_id"] is None
    assert [entry["name"] for entry in saved["encounters"]] == ["Alex", "Blake"]
