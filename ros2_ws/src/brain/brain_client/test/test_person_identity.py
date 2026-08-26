# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Focused tests for the local, explicitly written person gallery."""

import hashlib
import io
import json
import sys
import time
from pathlib import Path

import pytest
from brain_client.skills.types import SkillStorage
from brain_client.state.image import MainImage
from brain_client.state.pose import Pose
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "workspace"))

from innate_skills import mission_run as run_module
from innate_skills import person_identity as identity_module
from innate_skills import person_identity_embeddings as embeddings_module


def test_body_reid_model_is_the_reviewed_export():
    digest = hashlib.sha256(embeddings_module.MODEL_PATH.read_bytes()).hexdigest()
    assert digest == "7e49cb6b5a9b3fe3701a975900d5a98b80f5c3a5754208e46652d6bbcf29ce08"


def _frame(color: tuple[int, int, int]) -> MainImage:
    image = Image.new("RGB", (96, 72), color)
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG")
    return MainImage.from_jpeg(encoded.getvalue())


def _contract(output, code: str) -> dict:
    actual_code, encoded = output.message.split(" ", 1)
    assert actual_code == code
    assert output.data == json.loads(encoded)
    return output.data


class FakeEncoder:
    available = True

    def __init__(self, embeddings):
        self.diagnostics = {"body_available": True, "face_available": True}
        self.embeddings = iter(embeddings)

    def encode(self, _jpeg):
        return next(self.embeddings)


@pytest.fixture
def identity(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    run_module.start_run("test_agent")
    skill = identity_module.PersonIdentity(None)
    skill._storage = SkillStorage(
        tmp_path / "workspace" / "skill_storage" / "person_identity.json"
    )
    skill.pose = Pose(1.0, 2.0, 0.0)
    skill._fresh_upward_frame = lambda: (_frame((80, 100, 120)), None)
    return skill


def test_identify_does_not_enroll_until_remember(identity, monkeypatch):
    identity.storage["state"] = {"legacy": "gemini gallery"}
    monkeypatch.setattr(
        identity_module,
        "shared_encoder",
        lambda: FakeEncoder([{"body": [1, 0], "face": None}]),
    )

    assert _contract(identity._begin(), "IDENTITY_INITIALIZED")["people"] == []
    assert "state" not in identity.storage
    identified = _contract(identity._identify(), "UNKNOWN_PERSON")
    assert "timing" in identified
    assert identity._load_gallery()["profiles"] == []

    remembered = _contract(identity._remember(), "PERSON_REMEMBERED")
    assert remembered["encounter_id"] == "resident-001"
    assert remembered["channels"] == ["body"]
    assert identity._load_gallery()["profiles"][0]["encounter_id"] == "resident-001"


def test_local_body_match_recognizes_profile_without_mutating_it(identity, monkeypatch):
    identity._begin()
    encoder = FakeEncoder(
        [
            {"body": [1.0, 0.0, 0.0], "face": None},
            {"body": [0.98, 0.08, 0.0], "face": None},
        ]
    )
    monkeypatch.setattr(identity_module, "shared_encoder", lambda: encoder)
    _contract(identity._identify(), "UNKNOWN_PERSON")
    _contract(identity._remember(), "PERSON_REMEMBERED")

    known = _contract(identity._identify(), "KNOWN_PERSON")

    assert known["encounter_id"] == "resident-001"
    assert known["evidence"]["body_score"] > 0.99
    assert len(identity._load_gallery()["profiles"][0]["views"]) == 1


def test_different_body_stays_unknown(identity, monkeypatch):
    identity._begin()
    encoder = FakeEncoder(
        [
            {"body": [1.0, 0.0], "face": None},
            {"body": [0.0, 1.0], "face": None},
        ]
    )
    monkeypatch.setattr(identity_module, "shared_encoder", lambda: encoder)
    identity._identify()
    identity._remember()

    _contract(identity._identify(), "UNKNOWN_PERSON")


def test_conflicting_face_and_body_matches_are_ambiguous(identity, monkeypatch):
    identity._begin()
    encoder = FakeEncoder(
        [
            {"body": [1.0, 0.0], "face": [1.0, 0.0]},
            {"body": [0.0, 1.0], "face": [0.0, 1.0]},
            {"body": [0.99, 0.01], "face": [0.01, 0.99]},
        ]
    )
    monkeypatch.setattr(identity_module, "shared_encoder", lambda: encoder)
    identity._identify()
    identity._remember()
    identity._identify()
    identity._remember()

    ambiguous = _contract(identity._identify(), "IDENTITY_AMBIGUOUS")

    assert ambiguous["plausible_encounter_ids"] == ["resident-001", "resident-002"]


def test_remember_rejects_stale_or_moved_observation(identity):
    state = identity._fresh_gallery(run_module.active_run_id())
    state["pending_observation"] = {
        "image_b64": str(_frame((20, 40, 60))),
        "body": [1.0, 0.0],
        "face": None,
        "captured_at": time.time(),
        "pose": {"x": 1.0, "y": 2.0, "theta": 0.0},
    }
    identity._save_gallery(state)
    identity.pose = Pose(1.5, 2.0, 0.0)

    rejected = _contract(identity._remember(), "IDENTITY_NOT_STORED")

    assert rejected["reason"] == "robot_moved_since_identify"
    assert identity._load_gallery()["profiles"] == []


def test_add_view_requires_visual_compatibility(identity):
    state = identity._fresh_gallery(run_module.active_run_id())
    state["profiles"] = [
        {
            "encounter_id": "resident-001",
            "views": [
                {"image_b64": str(_frame((1, 2, 3))), "body": [1.0, 0.0], "face": None}
            ],
        }
    ]
    state["pending_observation"] = {
        "image_b64": str(_frame((4, 5, 6))),
        "body": [0.0, 1.0],
        "face": None,
        "captured_at": time.time(),
        "pose": {"x": 1.0, "y": 2.0, "theta": 0.0},
    }
    identity._save_gallery(state)

    rejected = _contract(identity._add_view("resident-001"), "IDENTITY_NOT_STORED")

    assert rejected["reason"] == "view_conflicts_with_profile"
    assert len(identity._load_gallery()["profiles"][0]["views"]) == 1
