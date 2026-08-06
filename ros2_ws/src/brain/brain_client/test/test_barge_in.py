# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the barge-in detector on synthetic acoustics.

Simulates the real feeding pattern: the ref stream leads the mic echo by a
TTFB-sized gap, the echo is a band-filtered, delayed, reverberant copy of the
ref (with the measured ~33 dB echo-over-ambient severity), and an optional
interrupter voice is mixed in. No hardware, no ROS.
"""

import numpy as np
import pytest

from brain_client.audio.barge_in import MIC_RATE, BargeInDetector

CHUNK = int(MIC_RATE * 0.02)  # 20 ms, matches ArecordStreamer
RNG = np.random.default_rng(1234)


def speech_like(duration_s: float, mod_hz: float, band=(150, 6000), level=0.3) -> np.ndarray:
    """Speech-shaped modulated noise, float in [-1, 1]."""
    n = int(duration_s * MIC_RATE)
    spec = np.fft.rfft(RNG.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / MIC_RATE)
    shape = np.zeros_like(freqs)
    m = (freqs > band[0]) & (freqs < band[1])
    shape[m] = np.where(freqs[m] < 500, 1.0, 500 / freqs[m])
    sig = np.fft.irfft(spec * shape)
    sig /= np.max(np.abs(sig))
    t = np.arange(len(sig)) / MIC_RATE
    # syllabic modulation with hard gaps (TTS-like inter-word pauses)
    mod = 0.5 + 0.5 * np.sin(2 * np.pi * mod_hz * t)
    mod = np.where(mod < 0.15, 0.0, mod)
    return sig * mod * level


def room_filter(sig: np.ndarray) -> np.ndarray:
    """Crude speaker+room coupling: bandpass emphasis 250-2000 Hz + reverb tail."""
    spec = np.fft.rfft(sig)
    freqs = np.fft.rfftfreq(len(sig), 1 / MIC_RATE)
    gain = 1.0 / (1.0 + (freqs / 2000.0) ** 2) * (freqs / (freqs + 150.0))
    out = np.fft.irfft(spec * gain)
    # exponential reverb tail, RT60 ~ 0.4 s
    ir_len = int(0.4 * MIC_RATE)
    ir = RNG.standard_normal(ir_len) * np.exp(-6.9 * np.arange(ir_len) / ir_len)
    ir[0] = 3.0
    reverbed = np.convolve(out, ir)[: len(out)]
    return reverbed / np.max(np.abs(reverbed)) * np.max(np.abs(sig))


class Harness:
    """Feeds ref + mic to a detector the way the live system does."""

    def __init__(self, ttfb_s=0.8, ref_lead_s=0.4, echo_gain=0.55, ambient=0.004, **det_kw):
        self.triggers: list[dict] = []
        # The synthetic room's residuals run hotter than a real one (its
        # convolved noise reverb is less predictable than measured speech
        # echo), so these tests pin their own threshold instead of riding the
        # shipped default — which is set from on-robot measurements.
        det_kw.setdefault("threshold_db", 10.0)
        det_kw.setdefault("min_ms", 250)
        det_kw.setdefault("reverb_decay", 0.92)
        self.det = BargeInDetector(on_trigger=self.triggers.append, **det_kw)
        self.ttfb_s = ttfb_s
        self.ref_lead_s = ref_lead_s
        self.echo_gain = echo_gain
        self.ambient = ambient

    def run(self, ref: np.ndarray, interrupter: np.ndarray | None = None, clip=False):
        echo = room_filter(ref) * self.echo_gain
        ttfb = int(self.ttfb_s * MIC_RATE)
        mic = RNG.standard_normal(ttfb + len(echo) + CHUNK) * self.ambient
        mic[ttfb : ttfb + len(echo)] += echo
        if interrupter is not None:
            n = min(len(interrupter), len(mic))
            mic[:n] += interrupter[:n]
        mic_i16 = np.clip(mic * 32768, -32768, 32767).astype(np.int16)
        if not clip:
            assert np.sum(np.abs(mic_i16.astype(np.int32)) >= 32000) < 0.01 * len(mic_i16)
        ref_i16 = np.clip(ref * 32768, -32768, 32767).astype(np.int16)

        self.det.on_ref_start(1)
        ref_sent = 0
        lead = int(self.ref_lead_s * MIC_RATE)
        for i in range(0, len(mic_i16) - CHUNK, CHUNK):
            # ref chunks arrive ahead of the echo: mic time i corresponds to
            # ref sample (i - ttfb), and the pipe tee runs `lead` early
            ref_due = min(len(ref_i16), max(0, i - ttfb + lead))
            if ref_due > ref_sent:
                self.det.feed_ref(ref_i16[ref_sent:ref_due].tobytes(), MIC_RATE)
                ref_sent = ref_due
            self.det.feed_mic(mic_i16[i : i + CHUNK].tobytes())
        self.det.on_ref_end(1)
        return self.triggers


def test_echo_only_no_trigger():
    ref = speech_like(8.0, mod_hz=3.1)
    triggers = Harness().run(ref)
    assert triggers == []


def test_loud_interrupter_triggers():
    ref = speech_like(8.0, mod_hz=3.1)
    # interrupter at roughly echo level (someone close / raised voice),
    # different syllable rate and narrower band, starting at t=4s
    dur, start = 3.0, 4.0
    voice = speech_like(dur, mod_hz=5.3, band=(300, 3000), level=0.35)
    inter = np.zeros(int(11.0 * MIC_RATE))
    s = int(start * MIC_RATE)
    inter[s : s + len(voice)] = voice
    triggers = Harness().run(ref, interrupter=inter)
    assert len(triggers) == 1
    # fired after the interrupter started, not before, and within ~1.5s
    at_s = triggers[0]["at_ms"] / 1000.0
    assert start < at_s < start + 1.5 + 0.5


def test_quiet_interrupter_in_tts_pause_triggers():
    # long deliberate pause in the TTS from 4s to 5.5s
    ref = speech_like(8.0, mod_hz=3.1)
    ref[int(4.0 * MIC_RATE) : int(5.5 * MIC_RATE)] = 0.0
    voice = speech_like(1.4, mod_hz=4.7, band=(300, 3000), level=0.08)
    inter = np.zeros(int(11.0 * MIC_RATE))
    s = int((0.8 + 4.1) * MIC_RATE)  # ttfb + pause position
    inter[s : s + len(voice)] = voice
    triggers = Harness().run(ref, interrupter=inter)
    assert len(triggers) == 1


def test_clipped_echo_no_false_trigger():
    ref = speech_like(8.0, mod_hz=3.1)
    triggers = Harness(echo_gain=1.6).run(ref, clip=True)
    assert triggers == []


def test_resample_path():
    # 44.1k ref through the ratecv path must still align and stay quiet
    ref24 = speech_like(6.0, mod_hz=3.1)
    x_old = np.arange(len(ref24)) / MIC_RATE
    n44 = int(len(ref24) * 44100 / MIC_RATE)
    ref44 = np.interp(np.arange(n44) / 44100, x_old, ref24)
    ref44_i16 = np.clip(ref44 * 32768, -32768, 32767).astype(np.int16)

    h = Harness()
    echo = room_filter(ref24) * h.echo_gain
    ttfb = int(h.ttfb_s * MIC_RATE)
    mic = RNG.standard_normal(ttfb + len(echo) + CHUNK) * h.ambient
    mic[ttfb : ttfb + len(echo)] += echo
    mic_i16 = np.clip(mic * 32768, -32768, 32767).astype(np.int16)

    h.det.on_ref_start(2)
    ref_sent = 0
    lead = int(h.ref_lead_s * 44100)
    for i in range(0, len(mic_i16) - CHUNK, CHUNK):
        i44 = int(i * 44100 / MIC_RATE)
        ref_due = min(len(ref44_i16), max(0, i44 - int(ttfb * 44100 / MIC_RATE) + lead))
        if ref_due > ref_sent:
            h.det.feed_ref(ref44_i16[ref_sent:ref_due].tobytes(), 44100)
            ref_sent = ref_due
        h.det.feed_mic(mic_i16[i : i + CHUNK].tobytes())
    h.det.on_ref_end(2)
    assert h.triggers == []


def test_underpredicting_model_no_false_trigger():
    # a learned model that badly under-predicts (e.g. AGC drift) must not
    # cause false triggers: the adaptive-gain floor covers the echo
    ref = speech_like(8.0, mod_hz=3.1)
    h = Harness()
    h.det.set_echo_predictor(lambda ctx: np.zeros(ctx.shape[1], dtype=np.float32))
    assert h.run(ref) == []


def test_odd_byte_ref_chunks():
    # network chunks split mid-sample; feed_ref must re-align, not drop them
    det = BargeInDetector()
    det.on_ref_start(1)
    pcm = (RNG.standard_normal(44100) * 8000).astype(np.int16).tobytes()
    for i in range(0, len(pcm), 999):  # odd chunk size
        det.feed_ref(pcm[i : i + 999], 44100)
    det.feed_mic(np.zeros(CHUNK, dtype=np.int16).tobytes())
    # all samples (minus <1 chunk of resampler latency) must have arrived
    got = det._ref.n_frames
    assert got >= (44100 // 44100 * 24000 - 2048) // 240 - 3


def test_no_ref_no_activity():
    # mic audio with no utterance active must be ignored entirely
    det = BargeInDetector(on_trigger=lambda i: pytest.fail("must not trigger"))
    chunk = (RNG.standard_normal(CHUNK) * 3000).astype(np.int16).tobytes()
    for _ in range(100):
        det.feed_mic(chunk)
