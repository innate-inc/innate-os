"""Actual PCM endpointing and lifecycle routing; fake transcribers, no network/ROS motion."""

import struct
import threading
from types import SimpleNamespace

import pytest

from brain_client.inputs.batch_stt import BatchSttSession
from brain_client.inputs.speech_lifecycle import SpeechLifecycle


@pytest.mark.parametrize("answer", ["confirmed exact order", "", OSError("offline failure")])
def test_pcm_onset_endpoint_pending_and_terminal(answer):
    events, legacy = [], []
    called, release, done = threading.Event(), threading.Event(), threading.Event()

    def transcribe(wav):
        assert wav.startswith(b"RIFF")
        called.set()
        assert release.wait(2)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def emit(event):
        events.append(event)
        if event["stage"] == "finished":
            done.set()

    session = BatchSttSession(
        transcriber=transcribe,
        sample_rate=24000,
        silence_secs=0.2,
        is_voiced=lambda chunk: any(chunk),
        on_transcript=legacy.append,
        on_speech=emit,
        logger=SimpleNamespace(info=lambda *_: None, error=lambda *_: None),
    )
    session.start()
    try:
        for _ in range(5):
            session.feed(struct.pack("<h", 1000) * 2400)  # actual .1s PCM chunks
        assert [e["stage"] for e in events] == ["started"]
        for _ in range(3):
            session.feed(b"\0\0" * 2400)
        assert called.wait(1)
        assert [e["stage"] for e in events] == ["started", "pending"]
        release.set()
        assert done.wait(1)
    finally:
        release.set()
        session.stop()
    assert [e["stage"] for e in events] == ["started", "pending", "finished"]
    assert len({e["utterance_id"] for e in events}) == 1
    assert events[-1]["text"] == (answer if isinstance(answer, str) else "")
    assert legacy == ([answer] if isinstance(answer, str) and answer else [])


def test_closed_or_timed_out_utterance_cannot_publish_late_text():
    events = []
    lifecycle = SpeechLifecycle(events.append)
    first = lifecycle.start()
    lifecycle.finish(first, reason="timeout")
    assert not lifecycle.finish(first, "late command")
    second = lifecycle.start()
    lifecycle.close()
    assert not lifecycle.finish(second, "old session")
    assert lifecycle.start() is None
    assert not any(e.get("text") for e in events)
    assert len([e for e in events if e["stage"] == "finished"]) == 2


# Reuse the actual BrainAgent fixture; no model invocation is needed for routing.
import test_local_brain  # noqa: E402

agent_factory = test_local_brain.agent_factory


def test_policy_default_and_microphone_completion_preserve_request_provenance(agent_factory):
    agent, state = agent_factory()
    state.current_directive = SimpleNamespace(listen_before_acting=lambda: False)
    agent.on_microphone_speech({'utterance_id': 'disabled', 'stage': 'started'})
    assert not agent._incoming_speech
    state.current_directive = SimpleNamespace(listen_before_acting=lambda: True)
    agent.on_microphone_speech({'utterance_id': 'active', 'stage': 'started'})
    assert len(agent._incoming_speech) == 1
    assert not any(e.kind == 'user' for e in agent._events)
    agent.on_microphone_speech({'utterance_id': 'active', 'stage': 'pending'})
    assert len(agent._incoming_speech) == 1
    agent.on_microphone_speech({'utterance_id': 'active', 'stage': 'finished', 'text': 'Exact words'})
    assert not agent._incoming_speech
    assert agent._events[-1].source == 'operator'
    assert 'Exact words' in agent._events[-1].text
    n = len(agent._events)
    agent.on_microphone_speech({'utterance_id': 'active', 'stage': 'finished', 'text': 'duplicate'})
    assert len(agent._events) == n


def test_request_change_discards_old_microphone_completion(agent_factory):
    agent, state = agent_factory()
    state.current_directive = SimpleNamespace(listen_before_acting=lambda: True)
    agent.on_microphone_speech({'utterance_id': 'old', 'stage': 'started'})
    agent._request_generation += 1  # same fence advanced by explicit request cancellation
    agent.on_microphone_speech({'utterance_id': 'old', 'stage': 'finished', 'text': 'resume old request'})
    assert not agent._incoming_speech
    assert not any('resume old request' in e.text for e in agent._events)


def test_orphan_timeout_retires_microphone_mapping(agent_factory):
    agent, state = agent_factory()
    state.current_directive = SimpleNamespace(listen_before_acting=lambda: True)
    agent.on_microphone_speech({'utterance_id': 'orphan', 'stage': 'started'})
    _, token = agent._microphone_tokens['orphan']
    agent.finish_incoming_speech(token, '')  # same callback used by the orphan watchdog
    assert not agent._incoming_speech and not agent._microphone_tokens and not agent._incoming_timers


@pytest.fixture
def realtime_mic(monkeypatch):
    import pathlib

    from brain_client.inputs.batch_stt import Endpointer
    monkeypatch.syspath_prepend(str(pathlib.Path(__file__).resolve().parents[5] / 'workspace'))
    from inputs.micro_input import MicroInput
    mic = MicroInput()
    mic._listening_enabled = True
    mic.is_active = lambda: True
    mic.logger = SimpleNamespace(info=lambda *_: None, error=lambda *_: None)
    mic._endpointer = Endpointer(sample_rate=24000, is_voiced=lambda chunk: any(chunk), silence_secs=.2)
    events, sent, reconnects = [], [], []
    mic.send_data = lambda event, **kwargs: events.append(event)
    mic._on_transcript = lambda *args, **kwargs: None  # legacy duplicate is a separate route
    mic._schedule_reconnect = lambda: reconnects.append(True)
    mic._send_scribe_audio = lambda pcm, commit: sent.append((pcm, commit)) or True
    yield mic, events, sent, reconnects
    mic._stop_evt.set()
    mic._speech.close()


def test_realtime_consecutive_pcm_retained_until_prior_commit_completes(realtime_mic):
    import json
    mic, events, sent, reconnects = realtime_mic
    def utterance(level):
        for _ in range(5):
            mic._send_chunk(struct.pack('<h', level) * 2400)
        for _ in range(3):
            mic._send_chunk(b'\0\0' * 2400)
    utterance(1000)
    utterance(2000)
    assert sum(commit for _, commit in sent) == 1
    assert len(mic._speech_queue) == 1 and not reconnects
    assert [e['stage'] for e in events] == ['started', 'pending', 'started', 'pending']
    session = mic._speech
    mic._on_elevenlabs_message(None, json.dumps({'message_type': 'committed_transcript', 'text': 'first'}), session=session)
    assert sum(commit for _, commit in sent) == 2
    mic._on_elevenlabs_message(None, json.dumps({'message_type': 'committed_transcript', 'text': 'second'}), session=session)
    assert [e['text'] for e in events if e['stage'] == 'finished'] == ['first', 'second']
    assert not mic._speech_queue and mic._speech_pending is None and not reconnects
    n = len(sent)
    mic._keep_warm(4800)
    assert len(sent) == n  # silence cannot create unmatched commits


def test_old_realtime_callback_validated_after_acquiring_session_lock(realtime_mic):
    import json
    mic, events, _, _ = realtime_mic
    old = mic._speech
    with mic._endpoint_lock:
        thread = threading.Thread(target=mic._on_elevenlabs_message, args=(None, json.dumps({
            'message_type': 'committed_transcript', 'text': 'old session'})), kwargs={'session': old})
        thread.start()
        mic._speech = SpeechLifecycle(mic._emit_speech)
    thread.join(1)
    assert not thread.is_alive() and not events
    old.close()


def test_timeout_serializes_with_audio_and_response(realtime_mic):
    import json

    mic, events, sent, reconnects = realtime_mic
    session = mic._speech
    token = session.start()
    mic._speech_pending = token
    # The timer cannot retire session state while audio owns its lock. After
    # timeout wins, both a late response and new audio must stay fenced.
    entered = threading.Event()
    def expire():
        entered.set()
        session.finish(token, reason='timeout')
    with mic._endpoint_lock:
        timer = threading.Thread(target=expire)
        timer.start()
        assert entered.wait(1)
        assert mic._speech_pending == token
    timer.join(1)
    assert not timer.is_alive()
    mic._on_elevenlabs_message(None, json.dumps({
        'message_type': 'committed_transcript', 'text': 'late'}), session=session)
    mic._send_chunk(struct.pack('<h', 1000) * 2400)
    assert mic._speech_ambiguous and mic._speech_pending is None
    assert not sent and len(reconnects) == 1
    assert [e.get('text') for e in events if e['stage'] == 'finished'] == ['']


@pytest.mark.parametrize('text', ['... Exact confirmed words', '...'])
def test_pcm_manager_node_brain_delivery_once(agent_factory, realtime_mic, text):
    pytest.importorskip('rclpy')
    from brain_client.inputs.manager import InputDeviceManager
    from brain_client.nodes.brain_client_node import BrainClientNode

    mic, _, _, _ = realtime_mic
    agent, state = agent_factory()
    state.current_directive = SimpleNamespace(listen_before_acting=lambda: True)
    errors = []
    logger = SimpleNamespace(info=lambda *_: None, debug=lambda *_: None,
                             warn=errors.append, error=errors.append)
    node = SimpleNamespace(brain=agent, state=state, chat=SimpleNamespace(history=[]),
                           get_logger=lambda: logger)
    manager = InputDeviceManager.__new__(InputDeviceManager)
    manager._logger = logger
    manager._chat_in_pub = SimpleNamespace(publish=lambda msg: BrainClientNode._on_chat_in(node, msg))
    mic.send_data = lambda data, data_type: manager._handle_device_data('micro', data, data_type)
    mic._on_transcript = type(mic)._on_transcript.__get__(mic)
    mic._send_vad_status = lambda: None  # unrelated UI telemetry, not the transcript route
    for _ in range(5):
        mic._send_chunk(struct.pack('<h', 1000) * 2400)
    assert agent._incoming_speech
    for _ in range(3):
        mic._send_chunk(b'\0\0' * 2400)
    assert agent._incoming_speech and not any(e.kind == 'user' for e in agent._events)
    import json
    mic._on_elevenlabs_message(None, json.dumps({
        'message_type': 'committed_transcript', 'text': text}), session=mic._speech)
    assert not agent._incoming_speech
    users = [e for e in agent._events if e.kind == 'user']
    assert len(users) == (1 if 'Exact' in text else 0)
    if users:
        assert users[0].source == 'operator' and 'Exact confirmed words' in users[0].text
        assert '...' not in users[0].text
    # A flagged legacy duplicate crossing a policy switch cannot become new input.
    state.current_directive = SimpleNamespace(listen_before_acting=lambda: False)
    manager._handle_device_data('micro', {'text': 'stale duplicate', 'speech_lifecycle': True}, 'chat_in')
    assert len([e for e in agent._events if e.kind == 'user']) == len(users)
    manager._handle_device_data('micro', 'normal legacy input', 'chat_in')
    assert agent._events[-1].source == 'operator' and 'normal legacy input' in agent._events[-1].text
    assert not errors


def test_realtime_noise_and_own_tts_flush_retire_or_preserve_hold(realtime_mic):
    mic, events, sent, _ = realtime_mic
    mic._send_chunk(struct.pack('<h', 1000) * 2400)  # too short: cough/click
    for _ in range(3):
        mic._send_chunk(b'\0\0' * 2400)
    assert [e['stage'] for e in events] == ['started', 'finished']
    assert events[-1]['reason'] == 'discarded' and not sent
    for _ in range(5):
        mic._send_chunk(struct.pack('<h', 1000) * 2400)
    mic._commit_before_duck()
    assert events[-1]['stage'] == 'pending'
    assert not mic._endpointer.in_speech and sum(commit for _, commit in sent) == 1
    mic._stop_evt.set()
    mic._invalidate_speech()
    assert events[-1]['stage'] == 'finished' and events[-1]['text'] == ''
