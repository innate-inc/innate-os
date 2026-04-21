"""Unit tests for manipulation.config_validation.

These tests are ROS-free and can be run with ``pytest`` directly::

    cd ros2_ws/src/brain/manipulation
    python -m pytest test/ -q

They cover schema happy paths, every numeric/string/pose edge case the
goal_callback is expected to reject, and a regression check that a real
``metadata.json`` from the ``wave`` skill still parses cleanly with
``extra='ignore'``.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest

# Make the in-tree package importable without rebuilding the colcon workspace.
_MANIPULATION_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_MANIPULATION_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_MANIPULATION_PKG_ROOT))

from manipulation.config_validation import (  # noqa: E402
    BehaviorConfigError,
    LearnedExecCfg,
    PosesExecCfg,
    ReplayExecCfg,
    validate_behavior_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate(cfg, tmp_path, *, check_files=False):
    return validate_behavior_config(cfg, str(tmp_path), check_files_exist=check_files)


def _touch(dir_path: Path, name: str) -> str:
    """Create an empty file so check_files_exist=True can succeed."""
    path = dir_path / name
    path.write_bytes(b"")
    return name


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------


class TestTopLevelShape:
    def test_raw_json_string_decodes(self, tmp_path):
        cfg = json.dumps(
            {"type": "learned", "execution": {"checkpoint": "a.pth"}}
        )
        result = _validate(cfg, tmp_path)
        assert result.behavior_type == "learned"

    def test_malformed_json_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="not valid JSON"):
            _validate("{not json", tmp_path)

    def test_unknown_type_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="type: must be one of"):
            _validate({"type": "bogus", "execution": {}}, tmp_path)

    def test_missing_execution_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="execution"):
            _validate({"type": "learned"}, tmp_path)

    def test_execution_not_object_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="execution"):
            _validate(
                {"type": "learned", "execution": "not a dict"}, tmp_path
            )

    def test_non_string_non_dict_payload_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="must be a JSON"):
            _validate(12345, tmp_path)

    def test_json_array_payload_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="decode to a JSON object"):
            _validate("[1, 2, 3]", tmp_path)


# ---------------------------------------------------------------------------
# Learned config
# ---------------------------------------------------------------------------


class TestLearnedExecCfg:
    def test_minimal_happy_path(self, tmp_path):
        result = _validate(
            {"type": "learned", "execution": {"checkpoint": "ckpt.pth"}},
            tmp_path,
        )
        assert isinstance(result.params, LearnedExecCfg)
        assert result.params.checkpoint == "ckpt.pth"
        assert result.params.duration == 120.0
        assert result.params.action_dim == 10
        assert result.params.progress_threshold == 2.0
        assert result.params.start_pose is None
        assert result.params.n_action_steps is None
        assert result.resolved_path == os.path.join(str(tmp_path), "ckpt.pth")

    def test_full_happy_path(self, tmp_path):
        result = _validate(
            {
                "type": "learned",
                "execution": {
                    "checkpoint": "ckpt.pth",
                    "action_dim": 8,
                    "duration": 30.0,
                    "progress_threshold": 1.5,
                    "start_pose": [0.0] * 6,
                    "end_pose": [1.0] * 6,
                    "start_pose_time": 2.0,
                    "end_pose_time": 2.0,
                    "n_action_steps": 40,
                },
            },
            tmp_path,
        )
        params = result.params
        assert isinstance(params, LearnedExecCfg)
        assert params.action_dim == 8
        assert params.duration == 30.0
        assert params.start_pose == [0.0] * 6
        assert params.n_action_steps == 40

    def test_missing_checkpoint_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="checkpoint"):
            _validate({"type": "learned", "execution": {}}, tmp_path)

    def test_empty_checkpoint_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="checkpoint"):
            _validate(
                {"type": "learned", "execution": {"checkpoint": ""}}, tmp_path
            )

    def test_null_checkpoint_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="checkpoint"):
            _validate(
                {"type": "learned", "execution": {"checkpoint": None}}, tmp_path
            )

    def test_checkpoint_file_missing_on_disk(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="does not exist"):
            validate_behavior_config(
                {"type": "learned", "execution": {"checkpoint": "nope.pth"}},
                str(tmp_path),
                check_files_exist=True,
            )

    def test_checkpoint_file_exists_on_disk(self, tmp_path):
        _touch(tmp_path, "ckpt.pth")
        result = validate_behavior_config(
            {"type": "learned", "execution": {"checkpoint": "ckpt.pth"}},
            str(tmp_path),
            check_files_exist=True,
        )
        assert result.resolved_path == str(tmp_path / "ckpt.pth")

    @pytest.mark.parametrize(
        "bad",
        [0, -1, -0.5, float("nan"), float("inf"), "120", True, "abc"],
    )
    def test_duration_rejected(self, tmp_path, bad):
        with pytest.raises(BehaviorConfigError, match="duration"):
            _validate(
                {
                    "type": "learned",
                    "execution": {"checkpoint": "ckpt.pth", "duration": bad},
                },
                tmp_path,
            )

    @pytest.mark.parametrize("bad", [0, -1, 100, True, "10"])
    def test_action_dim_rejected(self, tmp_path, bad):
        with pytest.raises(BehaviorConfigError, match="action_dim"):
            _validate(
                {
                    "type": "learned",
                    "execution": {
                        "checkpoint": "ckpt.pth",
                        "action_dim": bad,
                    },
                },
                tmp_path,
            )

    def test_negative_progress_threshold_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="progress_threshold"):
            _validate(
                {
                    "type": "learned",
                    "execution": {
                        "checkpoint": "ckpt.pth",
                        "progress_threshold": -0.1,
                    },
                },
                tmp_path,
            )

    @pytest.mark.parametrize(
        "bad_pose",
        [[0.0, 0.0, 0.0], [0.0] * 7, "not-a-list", [0.0, 0.0, 0.0, 0.0, 0.0, "x"]],
    )
    def test_bad_start_pose_rejected(self, tmp_path, bad_pose):
        with pytest.raises(BehaviorConfigError, match="start_pose"):
            _validate(
                {
                    "type": "learned",
                    "execution": {
                        "checkpoint": "ckpt.pth",
                        "start_pose": bad_pose,
                    },
                },
                tmp_path,
            )

    def test_empty_start_pose_coerced_to_none(self, tmp_path):
        # Preserve legacy "empty list means skip this pose" semantics.
        result = _validate(
            {
                "type": "learned",
                "execution": {"checkpoint": "ckpt.pth", "start_pose": []},
            },
            tmp_path,
        )
        assert result.params.start_pose is None

    def test_null_start_pose_accepted(self, tmp_path):
        result = _validate(
            {
                "type": "learned",
                "execution": {"checkpoint": "ckpt.pth", "start_pose": None},
            },
            tmp_path,
        )
        assert result.params.start_pose is None

    @pytest.mark.parametrize("bad", [0, -1, True, "42"])
    def test_n_action_steps_rejected(self, tmp_path, bad):
        with pytest.raises(BehaviorConfigError, match="n_action_steps"):
            _validate(
                {
                    "type": "learned",
                    "execution": {
                        "checkpoint": "ckpt.pth",
                        "n_action_steps": bad,
                    },
                },
                tmp_path,
            )

    def test_n_action_steps_null_accepted(self, tmp_path):
        result = _validate(
            {
                "type": "learned",
                "execution": {
                    "checkpoint": "ckpt.pth",
                    "n_action_steps": None,
                },
            },
            tmp_path,
        )
        assert result.params.n_action_steps is None

    def test_extra_keys_ignored(self, tmp_path):
        # extra='ignore' keeps metadata like 'stats_file', 'downloads',
        # 'model_type' from erroring out.
        result = _validate(
            {
                "type": "learned",
                "execution": {
                    "checkpoint": "ckpt.pth",
                    "stats_file": "stats.pt",
                    "downloads": {"foo": "bar"},
                },
            },
            tmp_path,
        )
        assert result.params.checkpoint == "ckpt.pth"


# ---------------------------------------------------------------------------
# Poses config
# ---------------------------------------------------------------------------


class TestPosesExecCfg:
    def test_happy_path(self, tmp_path):
        result = _validate(
            {
                "type": "poses",
                "execution": {
                    "poses": [[0.0] * 6, [1.0] * 6],
                    "steps": 2.0,
                },
            },
            tmp_path,
        )
        assert isinstance(result.params, PosesExecCfg)
        assert len(result.params.poses) == 2
        assert result.params.steps == 2.0
        assert result.resolved_path is None

    def test_missing_poses_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="poses"):
            _validate({"type": "poses", "execution": {}}, tmp_path)

    def test_empty_poses_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="poses"):
            _validate(
                {"type": "poses", "execution": {"poses": []}}, tmp_path
            )

    def test_inner_pose_wrong_length_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match=r"poses\.0"):
            _validate(
                {"type": "poses", "execution": {"poses": [[0, 0, 0]]}}, tmp_path
            )

    def test_steps_optional(self, tmp_path):
        result = _validate(
            {"type": "poses", "execution": {"poses": [[0.0] * 6]}}, tmp_path
        )
        assert result.params.steps is None

    @pytest.mark.parametrize("bad", [0, -1, True, "2"])
    def test_bad_steps_rejected(self, tmp_path, bad):
        with pytest.raises(BehaviorConfigError, match="steps"):
            _validate(
                {
                    "type": "poses",
                    "execution": {"poses": [[0.0] * 6], "steps": bad},
                },
                tmp_path,
            )


# ---------------------------------------------------------------------------
# Replay config
# ---------------------------------------------------------------------------


class TestReplayExecCfg:
    def test_happy_path(self, tmp_path):
        result = _validate(
            {
                "type": "replay",
                "execution": {
                    "replay_file": "ep.h5",
                    "replay_frequency": 20.0,
                },
            },
            tmp_path,
        )
        assert isinstance(result.params, ReplayExecCfg)
        assert result.params.replay_file == "ep.h5"
        assert result.params.replay_frequency == 20.0
        assert result.resolved_path == os.path.join(str(tmp_path), "ep.h5")

    def test_missing_replay_file_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="replay_file"):
            _validate({"type": "replay", "execution": {}}, tmp_path)

    def test_replay_file_missing_on_disk(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="does not exist"):
            validate_behavior_config(
                {"type": "replay", "execution": {"replay_file": "nope.h5"}},
                str(tmp_path),
                check_files_exist=True,
            )

    def test_replay_file_exists_on_disk(self, tmp_path):
        _touch(tmp_path, "ep.h5")
        result = validate_behavior_config(
            {"type": "replay", "execution": {"replay_file": "ep.h5"}},
            str(tmp_path),
            check_files_exist=True,
        )
        assert result.resolved_path == str(tmp_path / "ep.h5")

    @pytest.mark.parametrize("bad", [0, -1.0, float("nan"), True, "20"])
    def test_bad_replay_frequency_rejected(self, tmp_path, bad):
        with pytest.raises(BehaviorConfigError, match="replay_frequency"):
            _validate(
                {
                    "type": "replay",
                    "execution": {
                        "replay_file": "ep.h5",
                        "replay_frequency": bad,
                    },
                },
                tmp_path,
            )

    def test_start_pose_wrong_length_rejected(self, tmp_path):
        with pytest.raises(BehaviorConfigError, match="start_pose"):
            _validate(
                {
                    "type": "replay",
                    "execution": {
                        "replay_file": "ep.h5",
                        "start_pose": [0, 0, 0],
                    },
                },
                tmp_path,
            )


# ---------------------------------------------------------------------------
# Regression: real skill metadata files must keep parsing.
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[5]
_WAVE_METADATA = _REPO_ROOT / "skills" / "wave" / "metadata.json"


@pytest.mark.skipif(
    not _WAVE_METADATA.exists(),
    reason="wave skill metadata.json not present in this checkout",
)
def test_wave_metadata_parses(tmp_path):
    """Regression check: the hand-authored wave skill (replay type, with
    legacy extra keys like ``duration`` / ``model_type``) must keep parsing
    under ``extra='ignore'``.
    """
    payload = json.loads(_WAVE_METADATA.read_text())
    result = _validate(payload, _WAVE_METADATA.parent)
    assert result.behavior_type == "replay"
    assert isinstance(result.params, ReplayExecCfg)
    assert math.isclose(result.params.replay_frequency, 50.0)
