# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Small deterministic checks for simulator speech timing and queue policy."""

import io
import threading
import wave
from collections import deque
from types import SimpleNamespace

import pytest

from brain_client.transport.tts import TTSHandler, _survives_flush, _Utterance, _wav_duration_s


def _utterance(reply_id=None, protected=False):
    return _Utterance("some words", None, None, reply_id, protected)


def test_flush_keeps_rest_of_the_reply_being_spoken():
    # Reply-1's first sentence is playing; its second sentence must not be
    # dropped by a newer reply's flush — the reply holds the floor.
    assert _survives_flush(_utterance(reply_id="reply-1"), "reply-1")


def test_flush_drops_replies_that_never_started():
    assert not _survives_flush(_utterance(reply_id="reply-2"), "reply-1")


def test_flush_drops_untagged_backlog():
    assert not _survives_flush(_utterance(), None)
    assert not _survives_flush(_utterance(), "reply-1")


def test_flush_spares_protected_environment_speech():
    assert _survives_flush(_utterance(protected=True), "reply-1")
    assert _survives_flush(_utterance(protected=True), None)


def test_wav_duration_matches_pcm_frames():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 8_000)

    assert _wav_duration_s(buf.getvalue()) == pytest.approx(0.5)


def test_wav_duration_rejects_invalid_audio():
    assert _wav_duration_s(b"not a wav") == 0.0


def test_failed_reply_no_longer_holds_the_floor(monkeypatch):
    handler = TTSHandler.__new__(TTSHandler)
    handler._speech_queue = deque([_utterance(reply_id="failed-reply"), None])
    handler._speech_queue_maxlen = 16
    handler._speech_cv = threading.Condition()
    handler._playing_reply_id = None
    handler.logger = SimpleNamespace(info=lambda _message: None, error=lambda _message: None)
    handler.speak_text = lambda _text, _voice: False
    monkeypatch.setattr("brain_client.transport.tts.time.sleep", lambda _seconds: None)

    handler._speech_loop()

    assert handler._playing_reply_id is None


def test_new_reply_during_retry_drops_failed_reply_siblings(monkeypatch):
    handler = TTSHandler.__new__(TTSHandler)
    handler._speech_queue = deque(
        [
            _Utterance("failed first", None, None, "failed-reply", False),
            _Utterance("stale sibling", None, None, "failed-reply", False),
        ]
    )
    handler._speech_queue_maxlen = 16
    handler._speech_cv = threading.Condition()
    handler._playing_reply_id = None
    handler.logger = SimpleNamespace(
        debug=lambda _message: None,
        info=lambda _message: None,
        warning=lambda _message: None,
        error=lambda _message: None,
    )
    handler.is_available = lambda: True
    spoken = []

    def speak(text, _voice):
        spoken.append(text)
        return text == "new reply"

    def enqueue_new_reply(_seconds):
        assert handler._playing_reply_id is None
        assert handler.speak_text_async("new reply", replace_pending=True, reply_id="new-reply")
        handler._speech_queue.append(None)

    handler.speak_text = speak
    monkeypatch.setattr("brain_client.transport.tts.time.sleep", enqueue_new_reply)

    handler._speech_loop()

    assert spoken == ["failed first", "failed first", "new reply"]
    assert handler._playing_reply_id == "new-reply"
