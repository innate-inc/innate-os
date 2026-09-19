"""The live client finds the dataset lerobot is writing and labels it with the head angle."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from lerobot_robot_mars import dataset_meta
from lerobot_robot_mars.dataset_meta import head_angle_for_run, recording_root, write_sidecar


def make_dataset(root: Path, age_s: float = 0.0) -> Path:
    info = root / "meta" / "info.json"
    info.parent.mkdir(parents=True)
    info.write_text("{}")
    stamp = time.time() - age_s
    os.utime(info, (stamp, stamp))
    return root


def test_repo_id_resolves_to_the_fresh_stamped_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dataset_meta, "HF_LEROBOT_HOME", tmp_path)
    make_dataset(tmp_path / "me" / "pick_20260101_000000", age_s=9999)
    fresh = make_dataset(tmp_path / "me" / "pick_20260917_201500")
    make_dataset(tmp_path / "me" / "pickles")
    assert recording_root(["lerobot-record", "--dataset.repo_id=me/pick"]) == fresh


def test_an_old_dataset_being_replayed_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dataset_meta, "HF_LEROBOT_HOME", tmp_path)
    make_dataset(tmp_path / "me" / "pick", age_s=9999)
    assert recording_root(["lerobot-replay", "--dataset.repo_id=me/pick"]) is None
    assert recording_root(["lerobot-record", "--dataset.repo_id=me/pick", "--resume=true"]) == tmp_path / "me" / "pick"
    assert recording_root(["lerobot-teleoperate"]) is None


def test_head_angle_follows_the_dataset_behind_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dataset_meta, "HF_LEROBOT_HOME", tmp_path / "cache")
    monkeypatch.setattr(dataset_meta, "_from_hub", lambda *_args: None)  # offline
    recorded = make_dataset(tmp_path / "cache" / "me" / "pick-tv", age_s=9999)
    write_sidecar(recorded, head_angle_deg=0.0, source="lerobot")

    # replaying or resuming names the dataset
    assert head_angle_for_run(["lerobot-replay", "--dataset.repo_id=me/pick-tv"]) == (0.0, "me/pick-tv")
    assert head_angle_for_run(["lerobot-record", f"--dataset.root={recorded}", "--resume=true"]) == (0.0, str(recorded))

    # a rollout names only the policy; its checkpoint remembers the dataset it learned from
    checkpoint = tmp_path / "outputs" / "pretrained_model"
    checkpoint.mkdir(parents=True)
    (checkpoint / "train_config.json").write_text(json.dumps({"dataset": {"repo_id": "me/pick-tv", "root": None}}))
    assert head_angle_for_run(["lerobot-rollout", f"--policy.path={checkpoint}"]) == (0.0, "me/pick-tv")

    # nothing to go on: the caller falls back to the default
    assert head_angle_for_run(["lerobot-teleoperate"]) is None
    assert head_angle_for_run(["lerobot-rollout", "--policy.path=someone/unknown-policy"]) is None
