"""The live client finds the dataset lerobot is writing and labels it with the head angle."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lerobot_robot_mars import dataset_meta
from lerobot_robot_mars.dataset_meta import read_sidecar, recording_root, write_sidecar


def make_dataset(root: Path, age_s: float = 0.0) -> Path:
    info = root / "meta" / "info.json"
    info.parent.mkdir(parents=True)
    info.write_text("{}")
    stamp = time.time() - age_s
    os.utime(info, (stamp, stamp))
    return root


def test_explicit_root_wins(tmp_path: Path) -> None:
    root = make_dataset(tmp_path / "rec", age_s=9999)
    assert recording_root(["lerobot-record", f"--dataset.root={root}", "--dataset.repo_id=me/x"]) == root
    assert recording_root(["lerobot-record", "--dataset.root", str(tmp_path / "missing")]) is None


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


def test_sidecar_round_trip(tmp_path: Path) -> None:
    root = make_dataset(tmp_path / "rec")
    assert read_sidecar(root) is None
    write_sidecar(root, head_angle_deg=-20.0, source="lerobot")
    sidecar = read_sidecar(root)
    assert sidecar is not None
    assert sidecar["head_angle_deg"] == -20.0 and sidecar["head_angle_assumed"] is False and sidecar["robot"] == "mars"
