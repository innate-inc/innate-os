"""The converter turns recorder HDF5 into the same dataset the live client would record."""

from __future__ import annotations

import json
from pathlib import Path

import h5py
import numpy as np
import pytest
import torch
from lerobot.datasets.lerobot_dataset import LeRobotDataset

from lerobot_robot_mars.convert import EXPORT_KEY, SkillRecording, convert_skill
from lerobot_robot_mars.dataset_meta import read_sidecar
from lerobot_robot_mars.schema import ACTION_NAMES, CAMERA_SHAPE, STATE_NAMES, dataset_features

T = 8


def write_episode(path: Path, episode_id: int, *, with_images: bool = True, head_deg: float | None = None) -> None:
    rng = np.random.default_rng(episode_id)
    arm = rng.uniform(-1, 1, size=(T, 6))
    base = np.tile([[0.2 * episode_id, -0.1]], (T, 1))
    progress = np.linspace(0, 1, T)[:, None]
    termination = np.zeros((T, 1))
    action = np.hstack([arm, base, progress, termination])
    with h5py.File(path, "w") as h5:
        h5["/action"] = action
        h5["/observations/qpos"] = arm + 0.01
        h5["/observations/qvel"] = np.zeros((T, 6))
        h5["/timestamps/arm"] = np.arange(T) / 30
        if head_deg is not None:
            h5["/head_command"] = np.full(T, head_deg)
        if with_images:
            head = np.zeros((T, *CAMERA_SHAPE), dtype=np.uint8)
            head[..., 0] = 255  # pure blue in the recorder's BGR
            wrist = np.zeros((T, *CAMERA_SHAPE), dtype=np.uint8)
            wrist[..., 1] = 255
            h5["/observations/images/camera_1"] = head
            h5["/observations/images/camera_2"] = wrist


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "pick_cube"
    data = skill / "data"
    data.mkdir(parents=True)
    (skill / "metadata.json").write_text(json.dumps({"name": "pick_cube", "guidelines": "Pick up the cube."}))
    write_episode(data / "episode_0.h5", 0)
    write_episode(data / "episode_1.h5", 1)
    write_episode(data / "episode_2.h5", 2)
    (data / "dataset_metadata.json").write_text(
        json.dumps(
            {
                "data_frequency": 30,
                "dataset_type": "h5",
                "number_of_episodes": 3,
                "episodes": [
                    {"episode_id": 0, "file_name": "episode_0.h5", "source": "teleop", "outcome": "success"},
                    {"episode_id": 1, "file_name": "episode_1.h5", "source": "rollout", "outcome": "failure"},
                    {"episode_id": 2, "file_name": "episode_2.h5", "source": "teleop"},
                ],
            }
        )
    )
    return skill


def test_converted_dataset_matches_the_live_schema(skill_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "out"
    convert_skill(skill_dir, repo_id="innate/mars-test", root=root, vcodec="h264", log=lambda _m: None)

    dataset = LeRobotDataset("innate/mars-test", root=root)
    assert dataset.num_episodes == 2  # the failure is skipped by default
    assert dataset.num_frames == 2 * T
    assert dataset.fps == 30
    assert dataset.meta.robot_type == "mars"
    expected = dataset_features()
    for key, feature in expected.items():
        assert dataset.features[key]["dtype"] == feature["dtype"], key
        assert tuple(dataset.features[key]["shape"]) == tuple(feature["shape"]), key
    assert dataset.features["observation.state"]["names"] == list(STATE_NAMES)
    assert dataset.features["action"]["names"] == list(ACTION_NAMES)

    first = dataset[0]
    with h5py.File(skill_dir / "data" / "episode_0.h5") as h5:
        raw_action = np.asarray(h5["/action"])[0]
        raw_state = np.asarray(h5["/observations/qpos"])[0]
    assert first["action"].numpy() == pytest.approx(np.r_[raw_action[:6], raw_action[6:8]], abs=1e-6)
    assert first["observation.state"].numpy() == pytest.approx(raw_state, abs=1e-6)
    assert first["task"] == "Pick up the cube."
    head = first["observation.images.head"]
    assert isinstance(head, torch.Tensor) and head.shape == (3, 480, 640)
    assert head.mean(dim=(1, 2)).argmax().item() == 2  # RGB: blue is channel 2
    assert first["observation.images.wrist"].mean(dim=(1, 2)).argmax().item() == 1

    export = json.loads((skill_dir / "data" / "dataset_metadata.json").read_text())[EXPORT_KEY]
    assert export["repo_id"] == "innate/mars-test" and export["episode_ids"] == [0, 2]
    sidecar = read_sidecar(root)
    assert sidecar is not None and sidecar["head_angle_deg"] == -20.0 and sidecar["head_angle_assumed"] is True


def test_rerun_appends_only_new_episodes(skill_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "out"
    convert_skill(skill_dir, repo_id="innate/mars-test", root=root, vcodec="h264", log=lambda _m: None)
    convert_skill(
        skill_dir, repo_id="innate/mars-test", root=root, vcodec="h264", include_failures=True, log=lambda _m: None
    )

    dataset = LeRobotDataset("innate/mars-test", root=root)
    assert dataset.num_episodes == 3
    assert dataset.num_frames == 3 * T
    export = json.loads((skill_dir / "data" / "dataset_metadata.json").read_text())[EXPORT_KEY]
    assert export["episode_ids"] == [0, 1, 2]


def test_recorded_head_angle_lands_in_the_sidecar(skill_dir: Path, tmp_path: Path) -> None:
    for episode_id in (0, 2):
        write_episode(skill_dir / "data" / f"episode_{episode_id}.h5", episode_id, head_deg=12.0)
    root = tmp_path / "out"
    convert_skill(skill_dir, repo_id="innate/mars-test", root=root, vcodec="h264", log=lambda _m: None)
    sidecar = read_sidecar(root)
    assert sidecar is not None
    assert sidecar["head_angle_deg"] == 12.0 and sidecar["head_angle_assumed"] is False
    assert sidecar["source"] == "mars2lerobot"


def test_a_lost_local_copy_is_rebuilt_in_full_not_replaced_by_the_new_episodes(skill_dir: Path, tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "out"
    convert_skill(skill_dir, repo_id="innate/mars-test", root=root, vcodec="h264", log=lambda _m: None)
    shutil.rmtree(root)  # someone cleared the robot's cache; dataset_metadata.json still lists 0 and 2 as exported
    convert_skill(
        skill_dir, repo_id="innate/mars-test", root=root, vcodec="h264", include_failures=True, log=lambda _m: None
    )
    assert LeRobotDataset("innate/mars-test", root=root).num_episodes == 3


def test_replay_skill_without_dataset_metadata(tmp_path: Path) -> None:
    skill = tmp_path / "wave"
    skill.mkdir()
    (skill / "metadata.json").write_text(json.dumps({"name": "wave", "type": "replay"}))
    write_episode(skill / "episode_0.h5", 0)
    recording = SkillRecording(skill)
    refs = recording.episodes(include_failures=False)
    assert [ref.episode_id for ref in refs] == [0]
    assert recording.task == "wave"
    assert sum(1 for _ in recording.frames(refs[0])) == T


def test_stripped_episode_without_video_is_a_clear_error(skill_dir: Path) -> None:
    write_episode(skill_dir / "data" / "episode_0.h5", 0, with_images=False)
    recording = SkillRecording(skill_dir)
    ref = recording.episodes(include_failures=False)[0]
    with pytest.raises(FileNotFoundError, match="stripped"):
        next(recording.frames(ref))
