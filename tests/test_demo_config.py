"""Endpoint overrides must never inherit credentials from the default server."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workspace"))
from innate_skills.demo_config import load_demo_config


def test_missing_and_tracked_defaults(tmp_path):
    path = tmp_path / "runtime.json"
    assert load_demo_config(path) == {}
    path.write_text('{"api_url":"http://default", "token_file":"old-token"}')
    assert load_demo_config(path)["token_file"] == "old-token"


def test_local_replaces_endpoint_and_credentials(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text('{"api_url":"http://default", "token_file":"old-token"}')
    local = tmp_path / "runtime.local.json"
    local.write_text('{"api_url":"http://local"}')
    assert load_demo_config(path) == {"api_url": "http://local"}
    local.write_text('{}')
    assert load_demo_config(path) == {}


@pytest.mark.parametrize("content", ["broken json", "[]", "null", '"url"'])
def test_invalid_override_does_not_fall_back(tmp_path, content):
    path = tmp_path / "sock_detector.json"
    path.write_text('{"url":"http://default"}')
    (tmp_path / "sock_detector.local.json").write_text(content)
    with pytest.raises(ValueError):
        load_demo_config(path)


def test_feature_observer_uses_local_configuration(tmp_path, monkeypatch):
    from innate_skills.box_features import FeatureObserver
    monkeypatch.delenv("BASKET_POSE_API_URL", raising=False)
    monkeypatch.delenv("BASKET_POSE_API_TOKEN_FILE", raising=False)
    assets = ROOT / "workspace/config/basket_features"
    (tmp_path / "calibration.json").write_bytes((assets / "calibration.json").read_bytes())
    (tmp_path / "runtime.json").write_text(json.dumps({"api_url": "http://default", "token_file": "missing-token"}))
    (tmp_path / "runtime.local.json").write_text('{"api_url":"http://local:9071"}')
    observer = FeatureObserver(tmp_path)
    try:
        assert observer.api_url == "http://local:9071"
        assert observer.api_token is None
    finally:
        observer.close()
