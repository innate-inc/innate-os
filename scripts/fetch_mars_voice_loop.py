#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Bake the ``mars_voice`` acceleration loop from the robot's own TTS voice.

Asking TTS for "Brrrrrrr" does not get you a trill -- it gets a consonant and
a short vowel, because a lip trill is not a phoneme any model is trained to
sustain. Measured across ten spellings, nothing held steady past 0.28 s and
none of it flapped.

So the trill is not bought, it is built: this cuts out the longest stretch the
voice does hold steadily -- an "rrr" around 145 Hz -- and ``accel_voices``
flaps it at 19-34 Hz itself. That is also what a real lip trill is, a steady
voiced source chopped by the lips, so the two-part construction is the honest
model rather than a workaround.

The loop is cut to a whole number of pitch periods so the wrap lands in phase;
a short crossfade then only has to hide amplitude, not a pitch step.

Run it only to regenerate the asset. It is not committed -- it lives in
``gs://karmanyaah-public/sounds/motor_voices/`` and ``post_update.sh`` (step
11c) fetches it, so publishing is the second half of regenerating::

    python3 scripts/fetch_mars_voice_loop.py
    gsutil cp config/sounds/motor_voices/mars_voice_brr.wav gs://karmanyaah-public/sounds/motor_voices/

``INNATE_SERVICE_KEY`` must be set (``.env`` is read automatically).
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "ros2_ws/src/cloud/clients/proxy-client"))
sys.path.insert(0, str(REPO / "ros2_ws/src/cloud/clients/auth-client"))

DEFAULT_VOICE = "9fdaae0b-f885-4813-b589-3c07cf9d5fea"
"""The robot's default TTS voice, matching ``BrainConfig.cartesia_voice_id``."""
TRANSCRIPT = "Rrrrrrrrrrrrrr"
"""Scored best of ten spellings: the longest steady voiced run, and the lowest
pitch among those that hold -- which leaves the most headroom before playing
it back fast turns the robot into a chipmunk."""
FETCH_RATE = 44100
ASSET_RATE = 22050
"""The loop is a low buzz played back below 2x, so nothing in it lives near
11 kHz -- half the sample rate keeps the asset small for free."""
OUT = REPO / "config/sounds/motor_voices/mars_voice_brr.wav"

LOOP_SECONDS = 0.22
CROSSFADE_SECONDS = 0.02
PITCH_RANGE = (70.0, 400.0)
"""Wide enough that a real pitch never lands on the search boundary, where an
argmax that has nowhere higher to go reports the edge as if it were a peak."""


def fetch(voice_id: str) -> np.ndarray:
    """The raw TTS clip as mono floats."""
    from dotenv import load_dotenv

    from innate_proxy import ProxyClient

    load_dotenv(REPO / ".env")
    client = ProxyClient()
    raw = b"".join(
        client.cartesia.tts.bytes_stream(
            model_id="sonic-2",
            transcript=TRANSCRIPT,
            voice={"mode": "id", "id": voice_id},
            output_format={"container": "wav", "encoding": "pcm_s16le", "sample_rate": FETCH_RATE},
        )
    )
    # Cartesia streams a WAV whose length header is a placeholder, so the data
    # chunk is sliced by hand rather than trusted to the wave module.
    start = raw.find(b"data")
    body = raw[start + 8 :] if start >= 0 else raw
    return np.frombuffer(body[: len(body) // 2 * 2], dtype=np.int16).astype(np.float64) / 32768.0


def pitch(window: np.ndarray, rate: int) -> tuple[float, float]:
    """(periodicity 0..1, f0) by autocorrelation over the plausible voice range."""
    window = window - window.mean()
    if np.abs(window).max() < 1e-4:
        return 0.0, 0.0
    corr = np.correlate(window, window, mode="full")[len(window) - 1 :]
    corr /= corr[0] + 1e-12
    lo, hi = int(rate / PITCH_RANGE[1]), int(rate / PITCH_RANGE[0])
    segment = corr[lo:hi]
    peak = float(segment.max())
    # A voice with a strong second harmonic correlates almost as well at half
    # its period, so a plain argmax reports the octave above. Take the longest
    # lag that still correlates nearly as well -- the true fundamental.
    (contenders,) = np.where(segment >= 0.85 * peak)
    best = int(contenders[-1]) if len(contenders) else int(np.argmax(segment))
    return float(segment[best]), rate / (lo + best)


def steadiest(audio: np.ndarray, rate: int, seconds: float) -> tuple[np.ndarray, float]:
    """The most strongly voiced window, with its pitch.

    Scored on periodicity rather than loudness -- the loudest part of a clip is
    usually the plosive at the front, the one stretch that cannot loop. Pitch is
    measured per 40 ms frame and taken as the median over the window: run the
    autocorrelation across the whole window instead and a pitch that drifts even
    slightly smears its own peak away, which reads as an octave error.
    """
    frame, hop = int(0.04 * rate), int(0.01 * rate)
    scores, pitches = [], []
    for start in range(0, max(len(audio) - frame, 1), hop):
        window = audio[start : start + frame]
        periodicity, f0 = pitch(window, rate)
        scores.append(periodicity * float(np.abs(window).mean()))
        pitches.append(f0)

    span = max(int(seconds * rate / hop), 1)
    if len(scores) <= span:
        return audio[: int(seconds * rate)], float(np.median([f for f in pitches if f > 0] or [0.0]))
    totals = np.convolve(np.array(scores), np.ones(span), mode="valid")
    best = int(np.argmax(totals))
    voiced = [f for f in pitches[best : best + span] if f > 0]
    return audio[best * hop : best * hop + int(seconds * rate)], float(np.median(voiced or [0.0]))


def whole_periods(loop: np.ndarray, rate: int, f0: float) -> np.ndarray:
    """Trim to an exact number of pitch periods, so the wrap lands in phase.

    Without this the loop restarts mid-cycle and the ear hears a click at the
    seam that no crossfade short enough to preserve the buzz can hide.
    """
    if f0 <= 0:
        return loop
    period = rate / f0
    periods = int(len(loop) / period)
    return loop[: int(round(periods * period))] if periods >= 4 else loop


def seamless(loop: np.ndarray, rate: int, crossfade: float) -> np.ndarray:
    """Blend the loop's tail into its head so wrapping is inaudible.

    Costs the crossfade's worth of length: the returned loop is shorter than
    the input by exactly that much, because the tail is folded into the head
    rather than played after it.
    """
    fade = int(crossfade * rate)
    if fade * 2 >= len(loop):
        return loop
    body = loop[:-fade].copy()
    ramp = np.linspace(0.0, 1.0, fade)
    body[:fade] = body[:fade] * ramp + loop[-fade:] * (1.0 - ramp)
    return body


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio
    source = np.arange(len(audio)) / source_rate
    target = np.arange(int(len(audio) * target_rate / source_rate)) / target_rate
    return np.interp(target, source, audio)


def save(path: Path, audio: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-id", default=DEFAULT_VOICE, help="Cartesia voice to record")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    clip = fetch(args.voice_id)
    print(f"fetched {len(clip) / FETCH_RATE:.2f}s from Cartesia")

    window, f0 = steadiest(clip, FETCH_RATE, LOOP_SECONDS)
    print(f"steadiest voiced window at {f0:.1f} Hz")
    loop = seamless(whole_periods(window, FETCH_RATE, f0), FETCH_RATE, CROSSFADE_SECONDS)
    loop = resample(loop, FETCH_RATE, ASSET_RATE)
    peak = float(np.abs(loop).max())
    if peak > 1e-6:
        loop = loop / peak * 0.95

    save(args.out, loop, ASSET_RATE)
    seam = float(np.abs(loop[0] - loop[-1]))
    print(f"wrote {args.out} — {len(loop) / ASSET_RATE:.3f}s loop, seam step {seam:.4f}")


if __name__ == "__main__":
    main()
