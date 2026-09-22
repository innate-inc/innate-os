import base64
import json
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest
from benchmark import RTMPose, freeze, model_timestamp, summarize
from serve import save_recording


def payload():
    return {
        "metadata": {
            "session": str(uuid.uuid4()),
            "recording_id": str(uuid.uuid4()),
            "clip": "empty",
            "split": "calibration",
            "hand": "right",
            "protocol_version": 1,
            "duration_ms": 8000,
            "complete": True,
            "mime": "video/webm",
        },
        "video_base64": base64.b64encode(b"\x1aE\xdf\xa3fixture-only").decode(),
    }


def test_atomic_save_idempotent_retry_and_collision(tmp_path):
    body = payload()
    first = save_recording(tmp_path, body)
    assert save_recording(tmp_path, body) == first
    body["metadata"]["clip"] = "left"
    with pytest.raises(ValueError, match="different content"):
        save_recording(tmp_path, body)
    stored = json.loads((Path(first["saved"]) / "metadata.json").read_text())
    assert stored["clip"] == "empty"
    assert not list(tmp_path.glob("*/.upload-*"))


def test_no_path_traversal_or_invalid_video(tmp_path):
    body = payload()
    body["metadata"]["session"] = "../../escape"
    with pytest.raises(ValueError):
        save_recording(tmp_path, body)
    body = payload()
    body["video_base64"] = base64.b64encode(b"not video").decode()
    with pytest.raises(ValueError, match="container"):
        save_recording(tmp_path, body)


def test_freeze_preserves_sessions_and_refuses_overwrite(tmp_path):
    body = payload()
    save_recording(tmp_path, body)
    freeze(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["clips"][0]["session"] == body["metadata"]["session"]
    assert manifest["clips"][0]["split"] == "calibration"
    with pytest.raises(ValueError, match="already frozen"):
        freeze(tmp_path)


def test_freeze_rejects_tampered_video(tmp_path):
    saved = save_recording(tmp_path, payload())
    (Path(saved["saved"]) / "video.webm").write_bytes(b"altered")
    with pytest.raises(ValueError, match="hash mismatch"):
        freeze(tmp_path)


def test_freeze_does_not_score_interrupted_takes(tmp_path):
    body = payload()
    body["metadata"]["complete"] = False
    save_recording(tmp_path, body)
    with pytest.raises(ValueError, match="No completed"):
        freeze(tmp_path)


def test_no_detector_box_does_not_invoke_pose_fallback():
    model = RTMPose.__new__(RTMPose)
    model.tracker = Mock()
    model.tracker.det_model.return_value = []
    assert model.predict(None, 0) == []
    model.tracker.pose_model.assert_not_called()


def test_coverage_is_not_reported_as_accuracy():
    result = summarize([{"latency_ms": 10, "hands": []}, {"latency_ms": 20, "hands": [{"xy": []}]}])
    assert result["detection_coverage_not_recall"] == 0.5
    assert result["accuracy"] is None
    assert result["latency_ms_p50"] == 15


def test_timestamp_ties_are_preserved_but_api_gets_increasing_integers():
    assert model_timestamp(3014.0, 3014.0, 3014) == 3015
    assert model_timestamp(3046.0, 3014.0, 3015) == 3046
    with pytest.raises(ValueError, match="decreasing"):
        model_timestamp(3013.0, 3014.0, 3014)
    with pytest.raises(ValueError):
        model_timestamp(float("nan"), 3014.0, 3014)
