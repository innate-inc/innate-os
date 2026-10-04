# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Non-verbal sounds for emotes the robot does not speak over — a gasp, a sigh, a happy chirp.

Pure numpy, no assets: each sound is tones and breath noise under attack/release envelopes, picked
by keyword from the emote's prompt (else from the preset it matched), and kept 12 dB under the
voice so it reads as a reaction, not an announcement.
"""

from __future__ import annotations

import functools
import math
import re

import numpy as np
from numpy.typing import NDArray

from brain_client.common.enums import StrEnum

RATE = 16_000  # the speaker path's rate (transport/tts.py SPEAKER_SAMPLE_RATE)
# Cartesia speech measured at -28 dBFS RMS over its voiced samples (sim recordings, 2026-10).
VOICE_RMS_DBFS = -28.0
BELOW_VOICE_DB = 12.0

Signal = NDArray[np.float64]


class Sound(StrEnum):
    GASP = "gasp"
    SIGH = "sigh"
    CHIRP = "chirp"
    HUM = "hum"
    GRUMBLE = "grumble"
    YAWN = "yawn"
    CHUCKLE = "chuckle"


# First match wins, so the specific (a laugh, a yawn) precede the broad (happy, sad).
KEYWORDS: tuple[tuple[Sound, tuple[str, ...]], ...] = (
    (Sound.CHUCKLE, ("laugh", "chuckl", "giggl", "haha", "joke", "amus", "snicker")),
    (Sound.YAWN, ("yawn", "sleep", "drows", "tired", "exhaust")),
    (Sound.GASP, ("gasp", "surpris", "startl", "shock", "scare", "afraid", "fright", "alarm", "flinch", "wow")),
    (Sound.GRUMBLE, ("angry", "anger", "grumbl", "annoy", "irritat", "furious", "grump", "huff", "growl")),
    (
        Sound.SIGH,
        ("sigh", "sad", "deflat", "disappoint", "gloom", "slump", "droop", "sorrow", "weary", "relie", "bored"),
    ),
    (Sound.CHIRP, ("happy", "excit", "delight", "joy", "cheer", "thrill", "ecstatic", "elat", "proud", "glee", "beam")),
    (Sound.HUM, ("think", "curious", "ponder", "hmm", "puzzl", "confus", "wonder", "thoughtful", "consider")),
)
PRESET_SOUNDS: dict[str, Sound] = {
    "happy": Sound.CHIRP,
    "excited": Sound.CHIRP,
    "proud": Sound.CHIRP,
    "surprised": Sound.GASP,
    "scared": Sound.GASP,
    "sad": Sound.SIGH,
    "bored": Sound.SIGH,
    "relieved": Sound.SIGH,
    "sleepy": Sound.YAWN,
    "angry": Sound.GRUMBLE,
    "disagreeing": Sound.GRUMBLE,
    "thinking": Sound.HUM,
    "curious": Sound.HUM,
    "confused": Sound.HUM,
    "agreeing": Sound.HUM,
    "affectionate": Sound.HUM,
}


def sound_for(prompt: str, preset: str | None = None) -> Sound | None:
    """The sound a prompt's words call for, else its preset's; None for an emote that stays silent."""
    text = prompt.lower()
    for sound, stems in KEYWORDS:
        if any(re.search(rf"\b{stem}", text) for stem in stems):
            return sound
    return PRESET_SOUNDS.get(preset or "")


@functools.lru_cache(maxsize=32)
def synthesize(sound: Sound, seed: int = 0) -> NDArray[np.int16]:
    """``sound`` as 16-bit mono PCM at ``RATE``, ``BELOW_VOICE_DB`` under the voice; cached, so treat
    the array as read-only."""
    rng = np.random.default_rng(seed)
    signal = _SYNTHS[sound](rng)
    voiced = signal[np.abs(signal) > 0.05 * np.abs(signal).max()]
    target = 10 ** ((VOICE_RMS_DBFS - BELOW_VOICE_DB) / 20)
    signal *= target / max(float(np.sqrt(np.mean(voiced**2))), 1e-9)
    return np.round(np.clip(signal, -0.9, 0.9) * 32767).astype(np.int16)


def _gasp(rng: np.random.Generator) -> Signal:
    n = _samples(0.4)
    breath = _bandpass(rng.standard_normal(n), 900, 3200) * _envelope(n, 0.015, 0.3)
    tone = _tone(_sweep(n, 380, 820), (1.0, 0.3)) * _envelope(n, 0.02, 0.2)
    return 0.7 * breath + 0.5 * tone


def _sigh(rng: np.random.Generator) -> Signal:
    n = _samples(1.1)
    breath = _bandpass(rng.standard_normal(n), 250, 1600) * _envelope(n, 0.25, 0.65)
    tone = _tone(_sweep(n, 300, 165), (1.0, 0.35, 0.1)) * _envelope(n, 0.3, 0.6)
    return breath + 0.35 * tone


def _chirp(rng: np.random.Generator) -> Signal:
    first = _note(0.13, 900, 1300, rng)
    second = _note(0.16, 1150, 1750, rng)
    return np.concatenate([first, np.zeros(_samples(0.05)), second])


def _hum(rng: np.random.Generator) -> Signal:
    n = _samples(0.7)
    pitch = np.concatenate([np.full(_samples(0.45), 172.0), np.linspace(172, 210, n - _samples(0.45))])
    voice = _tone(pitch, (1.0, 0.55, 0.3, 0.15)) * _envelope(n, 0.06, 0.18)
    return voice + 0.04 * _bandpass(rng.standard_normal(n), 200, 1200) * _envelope(n, 0.06, 0.18)


def _grumble(rng: np.random.Generator) -> Signal:
    n = _samples(0.7)
    pitch = _sweep(n, 96, 82) * (1 + 0.04 * np.sin(2 * math.pi * 7 * np.arange(n) / RATE))
    buzz = _tone(pitch, tuple(1 / k for k in range(1, 9)))
    flutter = 0.65 + 0.35 * np.sin(2 * math.pi * 11 * np.arange(n) / RATE)
    return (buzz * flutter + 0.15 * _bandpass(rng.standard_normal(n), 80, 600)) * _envelope(n, 0.03, 0.22)


def _yawn(rng: np.random.Generator) -> Signal:
    n = _samples(1.3)
    rise = _samples(0.25)
    pitch = np.concatenate([np.linspace(380, 480, rise), _sweep(n - rise, 480, 190)])
    voice = _tone(pitch, (1.0, 0.4, 0.2)) * _envelope(n, 0.2, 0.5)
    return voice + 0.3 * _bandpass(rng.standard_normal(n), 300, 2000) * _envelope(n, 0.2, 0.5)


def _chuckle(rng: np.random.Generator) -> Signal:
    pulses = []
    for pitch in (340.0, 320.0, 300.0):
        n = _samples(0.08)
        voice = _tone(np.full(n, pitch), (1.0, 0.5, 0.25)) + 0.3 * _bandpass(rng.standard_normal(n), 400, 2500)
        pulses += [voice * _envelope(n, 0.01, 0.06), np.zeros(_samples(0.07))]
    return np.concatenate(pulses)


_SYNTHS = {
    Sound.GASP: _gasp,
    Sound.SIGH: _sigh,
    Sound.CHIRP: _chirp,
    Sound.HUM: _hum,
    Sound.GRUMBLE: _grumble,
    Sound.YAWN: _yawn,
    Sound.CHUCKLE: _chuckle,
}


def _note(seconds: float, start_hz: float, end_hz: float, rng: np.random.Generator) -> Signal:
    n = _samples(seconds)
    vibrato = 1 + 0.012 * np.sin(2 * math.pi * 30 * np.arange(n) / RATE + rng.uniform(0, math.tau))
    return _tone(_sweep(n, start_hz, end_hz) * vibrato, (1.0, 0.25)) * _envelope(n, 0.008, 0.04)


def _samples(seconds: float) -> int:
    return round(seconds * RATE)


def _sweep(n: int, start_hz: float, end_hz: float) -> Signal:
    return start_hz * (end_hz / start_hz) ** np.linspace(0.0, 1.0, n)


def _tone(pitch: Signal, harmonics: tuple[float, ...]) -> Signal:
    phase = 2 * math.pi * np.cumsum(pitch) / RATE
    return sum((weight * np.sin(k * phase) for k, weight in enumerate(harmonics, start=1)), np.zeros_like(phase))


def _envelope(n: int, attack_s: float, release_s: float) -> Signal:
    t = np.arange(n) / RATE
    attack = np.clip(t / attack_s, 0.0, 1.0)
    release = np.clip((t[-1] - t) / release_s, 0.0, 1.0)
    return attack * attack * release


def _bandpass(noise: Signal, low_hz: float, high_hz: float) -> Signal:
    """Breath: noise kept between two frequencies, with soft one-octave skirts."""
    spectrum = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(len(noise), 1 / RATE)
    gain = np.clip(freqs / low_hz, 0.0, 1.0) ** 2 / (1 + (freqs / high_hz) ** 4)
    band = np.fft.irfft(spectrum * gain, len(noise))
    return band / max(float(np.abs(band).max()), 1e-9)
