# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Batch speech-to-text: local endpointing, one blocking API call per utterance.

:class:`Endpointer` runs a voiced/unvoiced detector — Silero VAD
(``brain_client.inputs.vad``) or the energy fallback — over the PCM stream and
closes an utterance after enough silence. It also drives the realtime Scribe
backend's manual commits (``workspace/inputs/micro_input.py``); here,
:class:`BatchSttSession` ships each closed utterance whole through a vendor
transcriber — ElevenLabs Scribe batch or Gemini ``generateContent``. Both bias
toward the ``keyterms`` vocabulary: Scribe takes a parameter, Gemini gets the
words in its prompt.
"""

from __future__ import annotations

import array
import base64
import io
import json
import math
import queue
import threading
import wave
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from brain_client.brain.transport import GENERATE_PATH
from brain_client.inputs.speech_lifecycle import SpeechLifecycle
from brain_client.inputs.vad import MIC_SAMPLE_RATE, pcm16_to_f32, resample_24k_to_16k

if TYPE_CHECKING:
    from brain_client.brain.transport import GeminiRest
    from brain_client.common.logging import UniversalLogger
    from innate_proxy import ProxyClient

# Runtime aliases, not TYPE_CHECKING-only: workspace/inputs/micro_input.py
# annotates against them and has no `from __future__ import annotations`.
Transcriber = Callable[[bytes], str]
"""WAV bytes -> transcript text; "" when nothing was said."""

Keyterms = Sequence[str] | Callable[[], Sequence[str]]
"""A fixed bias vocabulary, or a supplier re-read per utterance — so a robot
rename reaches the next transcription without restarting the session."""


def _resolve_keyterms(keyterms: Keyterms) -> Sequence[str]:
    return keyterms() if callable(keyterms) else keyterms


class VoicedDetector(Protocol):
    """One PCM chunk -> does it contain speech?

    The three attributes are not decoration: MicroInput publishes them as the
    webapp's VAD telemetry, so a detector that only implements __call__ breaks
    the voice panel at runtime.
    """

    threshold: float
    level: float
    voiced: bool

    def __call__(self, chunk: bytes) -> bool: ...


PRE_ROLL_SECS = 0.4
MAX_UTTERANCE_SECS = 30.0
MIN_VOICED_SECS = 0.25


def rms_level(chunk: bytes) -> float:
    """Normalized RMS (0..1) of a 16-bit mono PCM chunk.

    A trailing odd byte is not a sample: raw capture pipes can hand over a
    partial frame, and int16 parsing raises on it rather than ignoring it.
    """
    samples = array.array("h", chunk[: len(chunk) - len(chunk) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


class EnergyDetector:
    """Voiced = RMS above a fixed threshold; `level` is the last chunk's RMS.

    ``gain`` reports an upstream software AGC's current boost; dividing it back
    out keeps boosted room tone under a threshold tuned on the raw mic.
    """

    def __init__(self, threshold: float, gain: Callable[[], float] | None = None):
        self.threshold = threshold
        self.level = 0.0
        self.voiced = False
        self._gain = gain

    def __call__(self, chunk: bytes) -> bool:
        self.level = rms_level(chunk) / (self._gain() if self._gain else 1.0)
        self.voiced = self.level >= self.threshold
        return self.voiced


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class Endpointer:
    """Cuts a continuous PCM stream into utterances by a voiced/unvoiced detector.

    Time is measured in audio fed, not wall clock — ducking pauses the stream,
    and a paused stream must not count as silence.
    """

    def __init__(self, *, sample_rate: int, is_voiced: VoicedDetector, silence_secs: float):
        self._is_voiced = is_voiced
        self._silence_secs = silence_secs
        self._bytes_per_sec = sample_rate * 2
        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_bytes = 0
        self._utterance = bytearray()
        self._in_speech = False
        self._silence_bytes = 0
        self._voiced_bytes = 0
        # True right after a close that discarded its clip (under MIN_VOICED_SECS)
        # — a realtime caller has already streamed that audio and must still
        # commit it out of the vendor buffer.
        self.closed_discarded = False

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def utterance_secs(self) -> float:
        return len(self._utterance) / self._bytes_per_sec

    def feed(self, chunk: bytes) -> bytes | None:
        """Consume one chunk; returns the finished utterance's PCM when one closes."""
        self.closed_discarded = False
        voiced = self._is_voiced(chunk)

        if not self._in_speech:
            self._buffer_pre_roll(chunk)
            if not voiced:
                return None
            self._in_speech = True
            self._utterance = bytearray(b"".join(self._pre_roll))
            self._silence_bytes = 0
            self._voiced_bytes = len(chunk)
            return None

        self._utterance.extend(chunk)
        if voiced:
            self._silence_bytes = 0
            self._voiced_bytes += len(chunk)
        else:
            self._silence_bytes += len(chunk)

        trailing_silence = self._silence_bytes / self._bytes_per_sec
        utterance_secs = len(self._utterance) / self._bytes_per_sec
        if trailing_silence < self._silence_secs and utterance_secs < MAX_UTTERANCE_SECS:
            return None
        return self._close()

    def _buffer_pre_roll(self, chunk: bytes) -> None:
        self._pre_roll.append(chunk)
        self._pre_roll_bytes += len(chunk)
        while self._pre_roll and self._pre_roll_bytes > PRE_ROLL_SECS * self._bytes_per_sec:
            self._pre_roll_bytes -= len(self._pre_roll.popleft())

    def flush(self) -> bytes | None:
        """Force-close an open utterance (duck boundary), same length gate as
        a natural close; None when nothing is open or the clip was too short."""
        if not self._in_speech:
            return None
        return self._close()

    def _close(self) -> bytes | None:
        utterance = bytes(self._utterance)
        long_enough = self._voiced_bytes / self._bytes_per_sec >= MIN_VOICED_SECS
        self._in_speech = False
        self._utterance = bytearray()
        self._pre_roll.clear()
        self._pre_roll_bytes = 0
        self._voiced_bytes = 0
        self._silence_bytes = 0
        self.closed_discarded = not long_enough
        return utterance if long_enough else None


# Tighter than the transports' shared defaults (60 s proxy / 120 s direct) —
# a transcript this late is stale anyway, and the worker should move on to
# newer speech. Both vendor transcribers pass it per call.
TRANSCRIBE_TIMEOUT_SECS = 30.0


# ---------- Keyterms ----------

# The robot's own name and the phrases it is actually given. Biasing costs +20%
# on every ElevenLabs transcription, so the list stays short; an empty
# stt_keyterms turns it (and the surcharge) off.
DEFAULT_KEYTERMS: tuple[str, ...] = (
    "MARS",
    "hey MARS",
    "go forwards",
    "go forward",
    "go backwards",
    "go backward",
    "go straight",
    "turn left",
    "turn right",
    "turn around",
    "stop",
    "come here",
    "follow me",
    "pick up",
    "put it down",
    "drop it",
    "open the gripper",
    "close the gripper",
    "raise the arm",
    "lower the arm",
    "take a picture",
    "what do you see",
    "go to the kitchen",
    "go back to the charger",
    "explore the room",
    "wave",
)

_KEYTERM_FORBIDDEN_CHARS = frozenset("<>{}[]\\")


def sanitize_keyterms(terms: Iterable[str] | str) -> list[str]:
    """Vendor-legal, whitespace-normalized, de-duplicated keyterms, in input order.

    ElevenLabs' limits are under 50 characters, at most 5 words, 1000 terms. It
    rejects the whole request over one illegal term, so a settings.yaml typo has
    to cost its own term rather than all transcription.
    """
    kept: list[str] = []
    seen: set[str] = set()
    # A bare string would otherwise be iterated into single-letter keyterms.
    for raw in [terms] if isinstance(terms, str) else terms:
        term = " ".join(str(raw).split())
        if not term or term in seen or len(term) >= 50 or len(term.split()) > 5:
            continue
        if _KEYTERM_FORBIDDEN_CHARS & set(term):
            continue
        seen.add(term)
        kept.append(term)
    return kept[:1000]


def keyterms_with_name(terms: Sequence[str], name: str | None) -> list[str]:
    """The vocabulary with the robot's live name biased first. An empty
    vocabulary stays empty — it means biasing (and its surcharge) is off."""
    if not terms or not name:
        return list(terms)
    return sanitize_keyterms([name, f"hey {name}", *terms])


# ---------- ElevenLabs Scribe batch ----------

ELEVENLABS_PROXY_ENDPOINT = "v1/speech-to-text"


SCRIBE_UPLOAD_RATE = 16_000


def _wav_to_scribe_pcm(wav: bytes) -> bytes:
    """WAV -> raw 16 kHz mono PCM, Scribe's documented low-latency upload form
    (file_format=pcm_s16le_16). Measured 2026-08: the raw upload plus skipping
    timestamps and audio-event tagging takes the round trip from ~1.1 s to
    ~0.45 s on the same route."""
    with wave.open(io.BytesIO(wav), "rb") as reader:
        pcm, rate = reader.readframes(reader.getnframes()), reader.getframerate()
    if rate == SCRIBE_UPLOAD_RATE:
        return pcm
    if rate != MIC_SAMPLE_RATE:
        raise ValueError(f"no resampler for {rate} Hz -> Scribe upload (mic captures at {MIC_SAMPLE_RATE} Hz)")
    samples = resample_24k_to_16k(pcm16_to_f32(pcm))
    return np.clip(samples * 32768.0, -32768, 32767).astype(np.int16).tobytes()


def elevenlabs_proxy_transcriber(proxy: ProxyClient, model: str, language: str, keyterms: Keyterms = ()) -> Transcriber:
    base_form: dict[str, Any] = {
        "model_id": model,
        "language_code": language,
        "file_format": "pcm_s16le_16",
        "timestamps_granularity": "none",
        "tag_audio_events": "false",
    }

    def transcribe(wav: bytes) -> str:
        # Copied per call, not mutated: a rename can empty the vocabulary, and a
        # keyterms key left over from an earlier utterance would outlive it.
        form = dict(base_form)
        if terms := _resolve_keyterms(keyterms):
            # One repeated form field per term — the vendor SDK passes keyterms as a
            # raw list, unlike its other list params, which it JSON-encodes.
            form["keyterms"] = list(terms)
        files = {"file": ("utterance.raw", _wav_to_scribe_pcm(wav), "application/octet-stream")}
        with proxy.request_stream(
            "elevenlabs", ELEVENLABS_PROXY_ENDPOINT, files=files, form=form, timeout=TRANSCRIBE_TIMEOUT_SECS
        ) as resp:
            payload = resp.read()
            if resp.status_code != 200:
                raise RuntimeError(f"elevenlabs via proxy: HTTP {resp.status_code}: {payload[:200]!r}")
            return str(json.loads(payload).get("text", "")).strip()

    return transcribe


# ---------- Gemini ----------

# The model must have an unambiguous way to say "nothing was said" — an empty
# reply can't be distinguished from a refusal or a formatting quirk.
NO_SPEECH = "NO_SPEECH"

_GEMINI_PROMPT = (
    "Transcribe the speech in this audio verbatim. Reply with only the "
    "transcript text - no quotes, labels, or commentary. The speaker's "
    "language is most likely {language}.{keyterms} If the audio contains no "
    f"intelligible human speech, reply with exactly {NO_SPEECH}."
)


def _says_no_speech(text: str) -> bool:
    """The sentinel survives light model decoration ('"NO_SPEECH".') but never
    matches inside longer text — that would eat a real transcript."""
    return text.strip("'\".,!? \t") == NO_SPEECH


def gemini_transcriber(rest: GeminiRest, model: str, language: str, keyterms: Keyterms = ()) -> Transcriber:
    def transcribe(wav: bytes) -> str:
        terms = _resolve_keyterms(keyterms)
        hint = f" These words are likely, so prefer them over similar-sounding ones: {', '.join(terms)}."
        prompt = _GEMINI_PROMPT.format(language=language, keyterms=hint if terms else "")
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav).decode()}},
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "thinkingConfig": {"thinkingLevel": "minimal"}},
        }
        response = rest.post(GENERATE_PATH.format(model=model), body, timeout=TRANSCRIBE_TIMEOUT_SECS)
        # candidates can be [] outright (blocked or empty response), not just absent.
        parts = (response.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return "" if _says_no_speech(text) else text

    return transcribe


# ---------- Session ----------

# Bounds the transcription backlog when the network stalls: newest speech
# wins, and speech from minutes ago must never surface as a fresh command.
MAX_PENDING_UTTERANCES = 4


class BatchSttSession:
    """Feeds mic chunks to the endpointer and transcribes closed utterances.

    Lifecycle-compatible with the realtime WebSocket clients (start / stop /
    wait_until_connected) so MicroInput drives every backend the same way.
    Transcription is blocking HTTP, so it runs on its own worker thread —
    the mic feed must never stall behind a slow call.
    """

    def __init__(
        self,
        *,
        transcriber: Transcriber,
        sample_rate: int,
        is_voiced: VoicedDetector,
        silence_secs: float,
        on_transcript: Callable[[str], None],
        logger: UniversalLogger,
        on_speech: Callable[[dict], None] | None = None,
    ):
        self._speech = SpeechLifecycle(on_speech or (lambda event: None))
        self._utterance_id = None
        self._feed_lock = threading.RLock()
        self._transcriber = transcriber
        self._sample_rate = sample_rate
        self._on_transcript = on_transcript
        self._logger = logger
        self._endpointer = Endpointer(sample_rate=sample_rate, is_voiced=is_voiced, silence_secs=silence_secs)
        self._utterances: queue.Queue[tuple[str | None, bytes] | None] = queue.Queue(maxsize=MAX_PENDING_UTTERANCES)
        self._worker: threading.Thread | None = None
        self._stopped = False
        self.utterance_count = 0
        self.failure_count = 0

    def start(self) -> None:
        self._worker = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._worker.start()

    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        return True

    def feed(self, chunk: bytes) -> None:
        with self._feed_lock:
            was_open = self._endpointer.in_speech
            utterance = self._endpointer.feed(chunk)
            if not was_open and self._endpointer.in_speech:
                self._utterance_id = self._speech.start()
            if was_open and not self._endpointer.in_speech:
                self._closed_utterance(utterance)

    def flush(self) -> None:
        """Close pre-duck audio; never endpoint on synthetic silence."""
        with self._feed_lock:
            if self._endpointer.in_speech:
                self._closed_utterance(self._endpointer.flush())

    def _closed_utterance(self, utterance: bytes | None) -> None:
        token, self._utterance_id = self._utterance_id, None
        if utterance is None:
            self._speech.finish(token, reason='discarded')
            return
        self._speech.ended(token)
        self.utterance_count += 1
        self._enqueue((token, utterance))

    def _enqueue(self, item: tuple[str | None, bytes] | None) -> None:
        while True:
            try:
                self._utterances.put_nowait(item)
                return
            except queue.Full:
                try:
                    dropped = self._utterances.get_nowait()
                    if dropped is not None:
                        self._speech.finish(dropped[0], reason="backlog_dropped")
                    self._logger.error("❌ Transcription backlog full — dropping oldest utterance")
                except queue.Empty:
                    pass

    def status(self) -> dict[str, Any]:
        return {
            "utterance_open": self._endpointer.in_speech,
            "utterance_secs": round(self._endpointer.utterance_secs, 2),
            "utterances": self.utterance_count,
            "failures": self.failure_count,
        }

    def stop(self) -> None:
        self._stopped = True
        self._speech.close()
        self._enqueue(None)
        if self._worker:
            self._worker.join(timeout=2.0)
            self._worker = None

    def _transcribe_loop(self) -> None:
        while True:
            utterance = self._utterances.get()
            if utterance is None:
                return
            token, pcm = utterance
            try:
                text = self._transcriber(pcm_to_wav(pcm, self._sample_rate))
                # A call that outlives stop() must not publish: by the time it
                # returns, the mic may be active again under a newer session.
                if not self._stopped:
                    self._speech.finish(token, text)
                    if text:
                        self._on_transcript(text)
            except Exception as e:  # noqa: BLE001 — one failed call must not kill the mic
                self._speech.finish(token, reason="transcription_failed")
                self.failure_count += 1
                self._logger.error(f"❌ Batch transcription failed: {e}")
