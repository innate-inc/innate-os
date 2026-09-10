"""Offline checks on the backends, including the one that needs a key.

WHY THE GEMINI CHECK EXISTS. GeminiBackend was rewritten to read
Observation.image_path and then never run, because running it needs a key. An
untested request-builder is an untested claim, and the moment a key does arrive
is the worst time to discover the image never made it into the payload. The
network call is stubbed; everything up to it is real.

  sim/.venv/bin/python -m pytest sim/bench/test_backends.py -q
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import base64
import json
import urllib.request
from pathlib import Path

import backends as B
import pytest
import registry
from brain_agent import ACTIONS, Observation

# A tiny but real JPEG: two bytes of SOI plus filler is enough, since nothing
# here decodes it -- what is under test is that the BYTES travel.
FRAME_BYTES = b"\xff\xd8\xff\xe0" + b"benchmark-test-frame" * 4
FAKE_KEY = "test-key-not-real"


# --- the registry, and that the control still exists -------------------------


def test_blind_control_backend_is_registered() -> None:
    assert "codex-blind" in registry.names()


def test_codex_sees() -> None:
    assert registry.resolve("codex").wants_image is True


def test_codex_blind_does_not_see() -> None:
    assert registry.resolve("codex-blind").wants_image is False


def test_control_is_the_same_model_minus_the_camera() -> None:
    assert registry.resolve("codex-blind").__bases__[0] is B.CodexBackend


# --- gemini refuses to run blind under a vision label -------------------------


def test_gemini_refuses_to_construct_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        B.GeminiBackend()


# --- gemini actually puts the frame in the request ----------------------------


@pytest.fixture
def gemini(monkeypatch: pytest.MonkeyPatch) -> tuple[B.GeminiBackend, dict]:
    """A GeminiBackend under a fake key with urlopen stubbed; the dict captures
    the URL and JSON body of the one request it makes."""
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": '{"action":"forward","args":"{\\"metres\\":0.5}"}'}]}}]}
            ).encode()

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return B.GeminiBackend(), captured


@pytest.fixture
def frame(tmp_path: Path) -> Path:
    path = tmp_path / "frame.jpg"
    path.write_bytes(FRAME_BYTES)
    return path


@pytest.fixture
def framed_decision(gemini, frame: Path) -> tuple[dict, dict]:
    """(decision, captured request) for one turn that had a camera frame."""
    backend, captured = gemini
    decision = backend.decide(Observation(brief="test", elapsed_s=1.0, turns_left=5, image_path=str(frame)), ACTIONS)
    return decision, captured


def _inline_part(captured: dict) -> dict | None:
    parts = captured.get("body", {}).get("contents", [{}])[0].get("parts", [])
    return next((p["inline_data"] for p in parts if "inline_data" in p), None)


def test_the_frame_is_attached_to_the_request(framed_decision) -> None:
    _, captured = framed_decision
    assert _inline_part(captured) is not None


def test_the_attached_bytes_are_the_frames_own(framed_decision) -> None:
    _, captured = framed_decision
    assert _inline_part(captured)["data"] == base64.b64encode(FRAME_BYTES).decode()


def test_the_frame_is_declared_as_jpeg(framed_decision) -> None:
    _, captured = framed_decision
    assert _inline_part(captured)["mime_type"] == "image/jpeg"


def test_the_reply_is_parsed_into_an_action(framed_decision) -> None:
    decision, _ = framed_decision
    assert decision == {"action": "forward", "args": {"metres": 0.5}}


def test_the_key_goes_on_the_url(framed_decision) -> None:
    _, captured = framed_decision
    assert FAKE_KEY in captured.get("url", "")


# --- an observation with no frame must not fabricate one ----------------------


def test_no_frame_means_no_image_part(gemini) -> None:
    backend, captured = gemini
    backend.decide(Observation(brief="t", elapsed_s=0.0, turns_left=1, image_path=None), ACTIONS)
    parts = captured["body"]["contents"][0]["parts"]
    assert all("inline_data" not in p for p in parts)
