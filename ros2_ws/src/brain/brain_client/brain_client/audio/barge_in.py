# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Barge-in detection: notice a human speaking while the robot's TTS is playing.

The speaker-to-mic coupling on MARS is severe (echo ~33 dB above ambient, mic
clips at TTS peaks), so plain VAD or energy detection cannot work while the
robot talks. Instead we exploit that the robot knows exactly what it is saying:
the TTS PCM that is piped to aplay is also fed here as a *reference*. The
detector aligns the reference against the mic, models the acoustic coupling
with per-mel-band adaptive gains, and flags sustained speech-band energy the
reference cannot explain.

Pure Python + numpy, no ROS. Runs inside the mic input's audio thread at
~100 frames/s; all state is single-threaded except the ref buffer, which is
appended from the ROS executor thread under a lock.

The linear per-band model is a baseline: `set_echo_predictor()` accepts a
learned model (ref log-mel context -> predicted mic band powers) trained from
robot-recorded pairs, which replaces the gains and handles the speaker's
nonlinearity better. Detection logic is identical either way.
"""

from __future__ import annotations

import audioop  # stdlib in py3.10 (removal in 3.13 doesn't affect this target)
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np


class _SilentLogger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


MIC_RATE = 24_000
FRAME = 512
HOP = 240  # 10 ms -> 100 frames/s
N_MELS = 24
FMIN, FMAX = 100.0, 8000.0
# Bands where the echo is strong and steady — used to track the learned
# model's level drift (good SNR for ratio estimation).
SPEECH_LO, SPEECH_HI = 200.0, 4000.0
# Scoring uses every band above SPEECH_LO *including* 4-8 kHz: coupling is
# 40 dB weaker up there, so an interrupter's consonants show the largest
# excess exactly where the echo masks least (confirmed on live voice traces).
SCORE_TOP_K = 5

CLIP_PEAK = 32000  # mic saturates at TTS peaks; clipped frames are unusable
ALIGN_SEARCH_FRAMES = 350  # ref can lead the mic by up to 3.5 s (TTFB + pipe)
ALIGN_WINDOW_FRAMES = 300
ALIGN_MIN_CORR = 0.55
MAX_UTTERANCE_FRAMES = 12_000  # 2 min; safety cap on per-utterance buffers


def _mel_filterbank(n_fft: int, rate: int, n_mels: int, fmin: float, fmax: float) -> np.ndarray:
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (np.asarray(m) / 2595.0) - 1.0)

    mel_pts = np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2)
    hz_pts = mel_to_hz(mel_pts)
    bins = np.floor((n_fft // 2 + 1) * hz_pts / (rate / 2)).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = bins[i], bins[i + 1], bins[i + 2]
        if mid > lo:
            fb[i, lo:mid] = np.linspace(0, 1, mid - lo, endpoint=False)
        if hi > mid:
            fb[i, mid:hi] = np.linspace(1, 0, hi - mid, endpoint=False)
    return fb


class _FramedStream:
    """Accumulates PCM samples and yields mel-band power frames."""

    def __init__(self, filterbank: np.ndarray):
        self._fb = filterbank
        self._window = np.hanning(FRAME).astype(np.float32)
        self._buf = np.zeros(0, dtype=np.float32)
        self.band_power: list[np.ndarray] = []  # one (N_MELS,) vector per frame
        self.peak: list[float] = []  # per-frame abs peak, for clip detection

    def append(self, samples: np.ndarray) -> int:
        """Add samples; frame everything available. Returns new frame count."""
        self._buf = np.concatenate([self._buf, samples])
        new = 0
        while len(self._buf) >= FRAME:
            frame = self._buf[:FRAME]
            spec = np.fft.rfft(frame * self._window)
            power = (np.abs(spec) ** 2).astype(np.float32) / FRAME
            self.band_power.append(self._fb @ power)
            self.peak.append(float(np.max(np.abs(frame))))
            self._buf = self._buf[HOP:]
            new += 1
        return new

    @property
    def n_frames(self) -> int:
        return len(self.band_power)

    def reset(self):
        self._buf = np.zeros(0, dtype=np.float32)
        self.band_power.clear()
        self.peak.clear()


class BargeInDetector:
    """Detects a human interrupting the robot mid-utterance.

    Lifecycle per TTS utterance:
      on_ref_start(seq) -> feed_ref(pcm)* + feed_mic(pcm)* -> on_ref_end(seq)

    feed_mic() drives all processing and must be called from one thread.
    On detection, ``on_trigger(info_dict)`` fires once per utterance.
    """

    def __init__(
        self,
        logger=None,
        on_trigger: Callable[[dict], None] | None = None,
        threshold_db: float = 6.0,
        min_ms: int = 150,
        warmup_ms: int = 400,
        reverb_decay: float = 0.87,
        debug_dump_dir: str = "",
    ):
        self.logger = logger if logger is not None else _SilentLogger()
        self.on_trigger = on_trigger
        self.threshold_db = float(threshold_db)
        self.min_ms = int(min_ms)
        self.warmup_ms = int(warmup_ms)
        self.reverb_decay = float(reverb_decay)
        self.debug_dump_dir = debug_dump_dir

        self._fb = _mel_filterbank(FRAME, MIC_RATE, N_MELS, FMIN, FMAX)
        freqs = np.fft.rfftfreq(FRAME, 1.0 / MIC_RATE)
        centers = np.array([freqs[np.argmax(self._fb[i])] for i in range(N_MELS)])
        self._speech_bands = (centers >= SPEECH_LO) & (centers <= SPEECH_HI)
        self._score_bands = centers >= SPEECH_LO
        self._state_lock = threading.RLock()

        self._mic = _FramedStream(self._fb)
        self._ref = _FramedStream(self._fb)
        self._ref_lock = threading.Lock()
        self._ref_pending: list[np.ndarray] = []
        self._ratecv_state = None

        # Coupling state persists across utterances: the transfer function only
        # changes when the arm moves or volume changes, and re-adapts in ~300ms.
        self._gains = np.full(N_MELS, np.nan, dtype=np.float32)
        self._ambient = np.full(N_MELS, 10 ** (-55 / 10) * 32768.0**2, dtype=np.float32)
        self._model: Callable[[np.ndarray], np.ndarray] | None = None
        self._model_ctx = 21
        self._model_scale = 1.0
        self._last_model_p: np.ndarray | None = None
        # External noise gate (wall clock): the host sets this while its own
        # servos move — that noise is loud at the mic and not in the ref.
        self.suppress_hot_until_wall = 0.0

        self._active = False
        self._reset_utterance_state()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def _reset_utterance_state(self):
        self._seq = -1
        self._mic.reset()
        self._ref.reset()
        with self._ref_lock:
            self._ref_pending.clear()
        self._ratecv_state = None
        self._ref_byte_tail = b""
        self._processed = 0  # mic frames already scored
        self._lag: int | None = None  # ref frame r echoes at mic frame r + lag
        self._lag_confident = False
        self._last_align_at = 0
        self._mic_onset: int | None = None
        self._onset_run = 0
        self._ref_onset: int | None = None
        self._ref_scan = 0
        self._suppress_until = 0
        # A human talking over the robot is only audible in the echo's
        # syllable dips and gaps, and the mic clips through much of the
        # overlap, so evidence is sparse: require `need` hot frames within a
        # 15x window (measured live: interrupters give 9-16 hot frames/1.5s,
        # echo-only gives 0-1). Clipped frames don't consume window slots.
        self._need_hot = max(3, self.min_ms // 25)
        self._hot = deque(maxlen=15 * self._need_hot)
        self._clip_win = deque(maxlen=15 * self._need_hot)
        self._gain_init_frames = 0
        self._reverb_env = np.zeros(N_MELS, dtype=np.float32)
        self._triggered = False
        self._clipped_frames = 0
        self._max_score = -99.0
        self._tail: deque[bytes] = deque(maxlen=100)  # ~2 s of post-trigger mic
        self._max_hot = 0
        self._t_proc = 0.0
        self._n_scored_calls = 0
        self._n_model_calls = 0
        self._trace: list[tuple] = []  # (frame, score, hot_sum, clipped, lag) when dumping

    def on_ref_start(self, seq: int):
        with self._state_lock:
            self._reset_utterance_state()
            self._seq = seq
            self._active = True

    def on_ref_end(self, seq: int):
        with self._state_lock:
            if not self._active:
                return
            self._active = False
        lag_ms = self._lag * 10 if self._lag is not None else None
        self.logger.info(
            f"🛑 barge-in utterance {self._seq} done: mic_frames={self._mic.n_frames} "
            f"ref_frames={self._ref.n_frames} lag={lag_ms}ms confident={self._lag_confident} "
            f"max_score={self._max_score:.1f}dB max_hot={self._max_hot}/{self._hot.maxlen} "
            f"clipped={self._clipped_frames} triggered={self._triggered} "
            f"| cpu={self._t_proc * 1000:.0f}ms over {self._n_scored_calls} frames "
            f"({self._t_proc / max(self._n_scored_calls, 1) * 1000:.2f}ms/frame, "
            f"{self._n_model_calls} model calls, "
            f"{self._t_proc / max(self._mic.n_frames / 100.0, 1e-6) * 100:.1f}% realtime)"
        )
        self._dump_debug()

    def _dump_debug(self):
        if not self.debug_dump_dir or not self._trace:
            return
        try:
            import os
            import time

            os.makedirs(self.debug_dump_dir, exist_ok=True)
            path = os.path.join(self.debug_dump_dir, f"barge_{int(time.time())}_{self._seq}.npz")
            np.savez_compressed(
                path,
                trace=np.array(self._trace, dtype=np.float32),
                mic_bands=np.asarray(self._mic.band_power),
                ref_bands=np.asarray(self._ref.band_power),
                mic_peak=np.asarray(self._mic.peak),
                gains=self._gains,
                ambient=self._ambient,
            )
            self.logger.info(f"🧪 barge-in debug dump: {path}")
        except Exception as e:
            self.logger.error(f"barge-in debug dump failed: {e}")

    # ------------------------------------------------------------------ #
    # data feeds
    # ------------------------------------------------------------------ #

    def feed_ref(self, pcm: bytes, rate: int):
        """Reference TTS PCM (s16le mono). Called from the ROS executor thread."""
        if not self._active:
            return
        # network chunk boundaries are not sample-aligned; carry odd bytes over
        pcm = self._ref_byte_tail + pcm
        if len(pcm) % 2:
            pcm, self._ref_byte_tail = pcm[:-1], pcm[-1:]
        else:
            self._ref_byte_tail = b""
        if not pcm:
            return
        if rate != MIC_RATE:
            pcm, self._ratecv_state = audioop.ratecv(pcm, 2, 1, rate, MIC_RATE, self._ratecv_state)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        with self._ref_lock:
            self._ref_pending.append(samples)
            if len(self._ref_pending) > 500:  # mic feed stalled; don't grow forever
                del self._ref_pending[:250]

    def feed_mic(self, chunk: bytes):
        """Mic PCM (s16le mono 24k). Drives framing, alignment and scoring."""
        with self._state_lock:
            if not self._active:
                return
            if self._triggered:
                self._tail.append(chunk)
                return
            with self._ref_lock:
                pending, self._ref_pending = self._ref_pending, []
            for samples in pending:
                self._ref.append(samples)
            self._mic.append(np.frombuffer(chunk, dtype=np.int16).astype(np.float32))
            if self._mic.n_frames > MAX_UTTERANCE_FRAMES:
                return
            self._process_new_frames()

    # ------------------------------------------------------------------ #
    # processing
    # ------------------------------------------------------------------ #

    def _process_new_frames(self):
        t0 = time.perf_counter()
        self._n_scored_calls += self._mic.n_frames - self._processed
        while self._processed < self._mic.n_frames:
            m = self._processed
            self._processed += 1
            self._track_onsets(m)
            if m - self._last_align_at >= 25:
                self._last_align_at = m
                self._update_alignment(m)
            self._score_frame(m)
        self._t_proc += time.perf_counter() - t0

    def _track_onsets(self, m: int):
        if self._mic_onset is None:
            level = float(np.sum(self._mic.band_power[m]))
            if level > 30.0 * float(np.sum(self._ambient)):
                self._onset_run += 1
                if self._onset_run >= 3:
                    self._mic_onset = m - self._onset_run + 1
            else:
                self._onset_run = 0
        if self._ref_onset is None:
            for r in range(self._ref_scan, self._ref.n_frames):
                if float(np.sum(self._ref.band_power[r])) > 10 ** (-45 / 10) * 32768.0**2:
                    self._ref_onset = r
                    break
            self._ref_scan = self._ref.n_frames
        if (
            self._lag is None
            and self._mic_onset is not None
            and self._ref_onset is not None
            and self._mic_onset >= self._ref_onset
        ):
            self._lag = self._mic_onset - self._ref_onset

    def _envelope(self, stream: _FramedStream, start: int, stop: int) -> np.ndarray:
        bp = np.asarray(stream.band_power[start:stop])
        env = np.log10(np.sum(bp, axis=1) + 1e-3)
        return env - np.mean(env)

    def _update_alignment(self, m: int):
        """Cross-correlate log-energy envelopes to find/refine the ref->mic lag.

        The ref stream leads the mic echo by TTS time-to-first-byte plus pipe
        and ALSA buffering — up to seconds, varying per utterance — so the lag
        must be estimated from the signals, seeded by the energy onsets.
        """
        n_mic = m + 1
        if n_mic < 80 or self._ref.n_frames < 40 or self._mic_onset is None:
            return
        win = min(ALIGN_WINDOW_FRAMES, n_mic - self._mic_onset)
        if win < 60:
            return
        mic_env = self._envelope(self._mic, n_mic - win, n_mic)

        # Narrow the search only once correlation has confirmed the lag; the
        # onset seed can be wrong (e.g. a human spoke during the TTFB gap).
        if self._lag is not None and self._lag_confident:
            lags = range(max(0, self._lag - 30), self._lag + 31)
        else:
            lags = range(0, ALIGN_SEARCH_FRAMES)
        best_corr, best_lag = -1.0, None
        mic_norm = float(np.linalg.norm(mic_env)) + 1e-6
        for lag in lags:
            r_stop = n_mic - lag
            r_start = r_stop - win
            if r_start < 0 or r_stop > self._ref.n_frames:
                continue
            ref_env = self._envelope(self._ref, r_start, r_stop)
            corr = float(np.dot(mic_env, ref_env)) / (mic_norm * (float(np.linalg.norm(ref_env)) + 1e-6))
            if corr > best_corr:
                best_corr, best_lag = corr, lag
        if best_lag is None:
            return
        if best_corr >= ALIGN_MIN_CORR:
            # A lag jump means playback stalled or the estimate was off; the
            # frames scored around it are unreliable, so hold triggering briefly.
            if self._lag_confident and self._lag is not None and abs(best_lag - self._lag) > 2:
                self._suppress_until = m + 30
            self._lag = best_lag
            self._lag_confident = True
        elif self._lag_confident:
            # keep the previous confident lag; a barge-in itself lowers corr
            pass
        else:
            self._lag = best_lag

    def _score_frame(self, m: int):
        # Noise-floor estimate from pre-echo frames (mic runs before playback
        # starts). Only quiet frames update it — a plain EMA would chase any
        # signal up and make the echo-onset test unwinnable.
        mic_p = self._mic.band_power[m]
        if self._mic_onset is None:
            if float(np.sum(mic_p)) < 8.0 * float(np.sum(self._ambient)):
                self._ambient = 0.9 * self._ambient + 0.1 * mic_p
            else:
                self._ambient *= 1.02  # slow drift so a genuine step change absorbs
            return
        if not self._lag_confident:
            return
        r = m - self._lag
        if r < 0 or r >= self._ref.n_frames:
            return

        clipped = self._mic.peak[m] >= CLIP_PEAK
        self._clip_win.append(1 if clipped else 0)
        if clipped:
            self._clipped_frames += 1
            if self.debug_dump_dir:
                self._trace.append((m, -99.0, sum(self._hot), 1.0, self._lag or -1))
            return

        ref_p = self._ref.band_power[r]
        # coupling gains: robust init, then leaky adaptation on non-hot frames
        ref_active = ref_p > 1e2
        if np.isnan(self._gains).any():
            with np.errstate(divide="ignore", invalid="ignore"):
                est = np.where(ref_active, mic_p / np.maximum(ref_p, 1e-6), np.nan)
            self._gains = np.where(np.isnan(self._gains), est, self._gains).astype(np.float32)
            self._gain_init_frames += 1
            # bands the TTS never excites (highs) would block init forever
            if self._gain_init_frames >= 30 and np.isnan(self._gains).any():
                fill = np.nanmedian(self._gains) if not np.isnan(self._gains).all() else 1e-3
                self._gains = np.where(np.isnan(self._gains), fill, self._gains).astype(np.float32)
            self._hot.append(0)
            return

        predicted = self._predict_echo(r, ref_p)
        floor = predicted + 4.0 * self._ambient + 1e-3
        with np.errstate(divide="ignore"):
            excess_db = 10.0 * np.log10(np.maximum(mic_p, 1e-6) / floor)
        # A voice only pokes above the echo in its few strongest bands; a wide
        # mean averages those away (measured: 300+ frames of top-5 excess
        # became 1 hot frame). Score the loudest K bands only.
        sb = excess_db[self._score_bands]
        score = float(np.mean(np.sort(sb)[-SCORE_TOP_K:]))
        self._max_score = max(self._max_score, score)

        hot = (
            score > self.threshold_db and m >= self._suppress_until and time.monotonic() >= self.suppress_hot_until_wall
        )
        self._hot.append(1 if hot else 0)
        self._max_hot = max(self._max_hot, sum(self._hot))

        # Adapt slowly (the coupling only changes on arm/volume moves) and only
        # while nothing suspicious is present — a fast EMA would absorb a
        # sustained interrupter and self-normalize it away.
        if score < self.threshold_db * 0.5:
            adapt = ref_active & (excess_db < 3.0)
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = np.where(adapt, mic_p / np.maximum(ref_p, 1e-6), self._gains)
            ratio = np.clip(ratio, self._gains * 0.5, self._gains * 2.0 + 1e-9)
            self._gains = (0.98 * self._gains + 0.02 * ratio).astype(np.float32)
            self._adapt_model_scale(mic_p, ref_p, r)
        # Persistent excess is the robot, not a human: the speaker's nonlinear
        # distortion sprays energy into bands the linear gains can never enter
        # through the <3dB adapt mask above, so it would read as interruption
        # forever. Leak upward at a fixed +1 dB/s while excess persists — a
        # real interruption (1-3s) loses at most ~3 dB before it has already
        # accumulated its trigger evidence; distortion present through minutes
        # of speech is fully absorbed. Gains persist across utterances.
        leak = ref_active & (excess_db >= 3.0)
        if np.any(leak):
            self._gains = np.where(leak, self._gains * 1.0023, self._gains).astype(np.float32)

        if self.debug_dump_dir:
            self._trace.append((m, score, sum(self._hot), 0.0, self._lag or -1))
        self._maybe_trigger(m, score)

    def _predict_echo(self, r: int, ref_p: np.ndarray) -> np.ndarray:
        # Reverb smears the echo (resonating room), so the prediction follows
        # the reference with an exponential decay tail instead of dropping
        # instantly in speech gaps. Set from measured utterance tails (median
        # 0.87/frame on MARS): too fast fires on the robot's own ring-out, too
        # slow buries short interruptions in the dips.
        self._reverb_env = np.maximum(ref_p, self._reverb_env * self.reverb_decay)
        predicted = self._gains * self._reverb_env
        if self._model is None:
            return predicted
        # The learned model captures the speaker's nonlinear spectral shape but
        # is static, and the camera mic's AGC swings the overall level (seen
        # live: 27 dB under-prediction bursts -> false triggers). An online
        # scalar tracks that common-mode drift, and the adaptive-gain estimate
        # floors the result so neither predictor can under-predict alone.
        r0 = max(0, r - (self._model_ctx - 1))
        self._n_model_calls += 1
        self._last_model_p = self._model(np.asarray(self._ref.band_power[r0 : r + 1]))
        return np.maximum(predicted, self._model_scale * self._last_model_p)

    def _adapt_model_scale(self, mic_p: np.ndarray, ref_p: np.ndarray, r: int):
        if self._model is None or self._last_model_p is None or not np.any(ref_p > 1e2):
            return
        model_p = self._last_model_p
        num = float(np.sum(mic_p[self._speech_bands]))
        den = float(np.sum(model_p[self._speech_bands])) + 1e-6
        ratio = np.clip(num / den, self._model_scale * 0.5, self._model_scale * 2.0)
        self._model_scale = float(np.clip(0.95 * self._model_scale + 0.05 * ratio, 0.05, 20.0))

    def _maybe_trigger(self, m: int, score: float):
        if self._triggered:
            return
        elapsed_ms = (m - (self._mic_onset or 0)) * 10
        if elapsed_ms < self.warmup_ms:
            return
        if sum(self._hot) < self._need_hot:
            return
        if sum(self._clip_win) > 0.6 * len(self._clip_win):
            return  # too much saturation in the window to judge
        self._triggered = True
        info = {
            "seq": self._seq,
            "score_db": round(score, 1),
            "at_ms": m * 10,  # position in the mic capture timeline
            "hot_frames": int(sum(self._hot)),
        }
        self.logger.info(f"🙋 BARGE-IN detected: {info}")
        if self.on_trigger:
            try:
                self.on_trigger(info)
            except Exception as e:  # callback is app code; never kill the audio thread
                self.logger.error(f"barge-in trigger callback failed: {e}")

    # ------------------------------------------------------------------ #
    # extras
    # ------------------------------------------------------------------ #

    def drain_tail(self) -> list[bytes]:
        """Mic chunks captured after the trigger (for STT prefix replay)."""
        tail = list(self._tail)
        self._tail.clear()
        return tail

    def set_echo_predictor(self, predictor: Callable[[np.ndarray], np.ndarray]):
        """Install a learned echo model: (ctx_frames, N_MELS) ref powers -> (N_MELS,) predicted mic powers.

        Combined with (never replacing) the adaptive gains — see _predict_echo.
        """
        self._model = predictor
        self._model_ctx = int(getattr(predictor, "context_frames", 21))
        self._model_scale = 1.0
