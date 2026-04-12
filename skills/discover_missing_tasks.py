#!/usr/bin/env python3
"""
Discover missing tasks — same mic + transcribe pipeline as ``user_listener``, then runs
:func:`semantic_skill_analyzer.analyze` on the transcript.

Recording modes and JSON success shape match ``user_listener``; see that module for parameters.
"""

from __future__ import annotations

import array
import importlib.util
import json
import math
import os
import re
import select
import statistics
import subprocess
import tempfile
import time
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from brain_client.skill_types import Skill, SkillResult
from semantic_skill_analyzer import analyze, analyze_gemma

_tspec = importlib.util.spec_from_file_location(
    "innate_skills_openai_transcription_http",
    Path(__file__).resolve().parent / "_openai_transcription_http.py",
)
_tx_http = importlib.util.module_from_spec(_tspec)
assert _tspec and _tspec.loader
_tspec.loader.exec_module(_tx_http)

try:
    from innate_proxy import ProxyClient
except ImportError:
    ProxyClient = None  # type: ignore[misc, assignment]

_RECORD_FORMAT = ["-f", "S16_LE", "-r", "16000", "-c", "1"]
_RECORD_TYPE = "wav"
_SAMPLE_RATE = 16000
_CHUNK_MS = 20
_CHUNK_SAMPLES = int(_SAMPLE_RATE * (_CHUNK_MS / 1000.0))
_CHUNK_BYTES = _CHUNK_SAMPLES * 2

_CANCELLED = "cancelled"


def _rms_int16_mono(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    a = array.array("h")
    a.frombytes(pcm)
    n = len(a)
    if n == 0:
        return 0.0
    acc = sum(s * s for s in a)
    return math.sqrt(acc / n)


def _detect_alsa_capture_device(logger: Any) -> str | None:
    devices: list[dict] = []
    try:
        result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            pattern = r"card (\d+):.*?\[([^\]]+)\].*?device (\d+):"
            for match in re.finditer(pattern, result.stdout):
                devices.append(
                    {
                        "id": f"plughw:{match.group(1)},{match.group(3)}",
                        "name": match.group(2),
                    }
                )
    except Exception as e:
        logger.warning(f"[DiscoverMissingTasks] arecord -l failed: {e}")
        return None

    logger.info(f"[DiscoverMissingTasks] ALSA capture devices: {[d['name'] for d in devices]}")
    if not devices:
        return None

    def pick() -> dict | None:
        for dev in devices:
            n = dev["name"].lower()
            if "mic" in n and "usb" in n:
                return dev
        for dev in devices:
            n = dev["name"].lower()
            if "sound" in n and ("usb" in n or "pnp" in n):
                return dev
        for dev in devices:
            if "mic" in dev["name"].lower():
                return dev
        for dev in devices:
            n = dev["name"].lower()
            if "usb" in n and "camera" not in n and "webcam" not in n:
                return dev
        for dev in devices:
            n = dev["name"].lower()
            if "camera" in n or "webcam" in n:
                return dev
        non_ape = [d for d in devices if "ape" not in d["name"].lower()]
        if non_ape:
            return non_ape[0]
        return devices[0]

    chosen = pick()
    if chosen:
        logger.info(f"[DiscoverMissingTasks] Using ALSA device {chosen['id']} ({chosen['name']})")
        return chosen["id"]
    return None


def _pcm16_mono_to_wav(wav_path: str, pcm: bytes, sample_rate: int = _SAMPLE_RATE) -> None:
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def _transcribe_wav(logger: Any, wav_path: str, transcribe_model: str) -> tuple[str | None, str | None]:
    if ProxyClient is None:
        return None, "innate_proxy not available"
    proxy = ProxyClient()
    if not proxy.is_available():
        return None, "Proxy not configured (set INNATE_PROXY_URL and INNATE_SERVICE_KEY)"

    url = f"{proxy.proxy_url}/v1/services/openai/v1/audio/transcriptions"
    client = proxy.get_sync_client()

    try:
        with open(wav_path, "rb") as audio_f:
            resp = client.post(
                url,
                files={"file": ("speech.wav", audio_f, "audio/wav")},
                data={"model": transcribe_model},
                headers=_tx_http.TRANSCRIPTION_REQUEST_HEADERS,
            )
        data, err = _tx_http.transcription_json_from_response(resp, logger)
        if err:
            return None, err
        text = (data.get("text") or "").strip()
        return text, None
    except Exception as e:
        logger.exception("[DiscoverMissingTasks] transcription request failed")
        return None, str(e)
    finally:
        try:
            proxy.close()
        except Exception:
            pass


def _terminate_proc(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _record_fixed_duration(
    logger: Any,
    dev: str,
    dur_int: int,
    is_cancelled: Callable[[], bool],
) -> tuple[bytes | None, str | None]:
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        cmd = [
            "arecord",
            "-D",
            str(dev),
            *_RECORD_FORMAT,
            "-t",
            _RECORD_TYPE,
            "-d",
            str(dur_int),
            wav_path,
        ]
        logger.info(f"[DiscoverMissingTasks] Recording {dur_int}s (fixed) from -D {dev!r}: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        while True:
            if is_cancelled():
                _terminate_proc(proc)
                try:
                    if os.path.isfile(wav_path):
                        os.unlink(wav_path)
                except OSError:
                    pass
                return None, _CANCELLED

            code = proc.poll()
            if code is not None:
                err = ""
                if proc.stderr:
                    try:
                        err = proc.stderr.read().decode(errors="replace")[:800]
                    except Exception:
                        pass
                if code != 0:
                    logger.error(f"[DiscoverMissingTasks] arecord exited {code}: {err}")
                    return None, f"Recording failed (arecord){': ' + err.strip() if err.strip() else ''}"
                break
            time.sleep(0.05)

        if not os.path.isfile(wav_path) or os.path.getsize(wav_path) < 100:
            return None, "Recording empty or too short"

        with open(wav_path, "rb") as f:
            return f.read(), None
    finally:
        try:
            if os.path.isfile(wav_path):
                os.unlink(wav_path)
        except OSError:
            pass


def _record_until_silence(
    logger: Any,
    dev: str,
    is_cancelled: Callable[[], bool],
    *,
    max_duration_seconds: float,
    silence_ms_after_speech: int,
    min_speech_ms: int,
    noise_calibration_ms: int,
    speech_rms_multiplier: float,
    pre_roll_ms: int,
) -> tuple[bytes | None, str | None]:
    """
    Stream raw S16LE mono from arecord; stop after trailing silence post-speech (capped by max duration).
    """
    cmd = [
        "arecord",
        "-D",
        str(dev),
        "-f",
        "S16_LE",
        "-r",
        str(_SAMPLE_RATE),
        "-c",
        "1",
        "-t",
        "raw",
        "-q",
        "-",
    ]
    logger.info(
        f"[DiscoverMissingTasks] Recording (silence-end) from -D {dev!r}, "
        f"max {max_duration_seconds}s, end after {silence_ms_after_speech}ms quiet: {' '.join(cmd)}"
    )
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    if proc.stdout is None:
        _terminate_proc(proc)
        return None, "arecord did not provide stdout"

    pre_chunks = max(1, int(pre_roll_ms / _CHUNK_MS))
    pre_roll: deque[bytes] = deque(maxlen=pre_chunks)
    partial = bytearray()
    calib_rms: list[float] = []
    t0 = time.monotonic()
    calib_until = t0 + noise_calibration_ms / 1000.0
    deadline = t0 + max_duration_seconds
    thresh: float | None = None
    recording = False
    out = bytearray()
    speech_ms = 0.0
    silence_ms = 0.0

    finished_after_silence = False
    try:
        while time.monotonic() < deadline and not finished_after_silence:
            if is_cancelled():
                _terminate_proc(proc)
                return None, _CANCELLED

            r, _, _ = select.select([proc.stdout], [], [], 0.05)
            if proc.stdout in r:
                block = proc.stdout.read(4096)
                if not block:
                    break
                partial.extend(block)

            while len(partial) >= _CHUNK_BYTES:
                chunk = bytes(partial[:_CHUNK_BYTES])
                del partial[:_CHUNK_BYTES]
                rms = _rms_int16_mono(chunk)
                now = time.monotonic()

                if thresh is None:
                    calib_rms.append(rms)
                    if now >= calib_until:
                        if calib_rms:
                            med = float(statistics.median(calib_rms))
                            thresh = max(med * float(speech_rms_multiplier), 60.0)
                        else:
                            thresh = 500.0
                        logger.info(f"[DiscoverMissingTasks] Silence-end RMS threshold ≈ {thresh:.1f} (from calibration)")
                    if thresh is None:
                        pre_roll.append(chunk)
                        continue

                assert thresh is not None
                pre_roll.append(chunk)
                loud = rms >= thresh

                if not recording:
                    if loud:
                        recording = True
                        out.extend(b"".join(pre_roll))
                        pre_roll.clear()
                        speech_ms += float(_CHUNK_MS)
                        silence_ms = 0.0
                else:
                    out.extend(chunk)
                    if loud:
                        speech_ms += float(_CHUNK_MS)
                        silence_ms = 0.0
                    else:
                        silence_ms += float(_CHUNK_MS)
                        if speech_ms >= float(min_speech_ms) and silence_ms >= float(silence_ms_after_speech):
                            logger.info(
                                f"[DiscoverMissingTasks] End of speech (~{silence_ms_after_speech}ms quiet), "
                                f"speech≈{speech_ms:.0f}ms, captured {len(out)} bytes PCM"
                            )
                            _terminate_proc(proc)
                            finished_after_silence = True
                            break

            if proc.poll() is not None:
                break

        if not recording or len(out) < _CHUNK_BYTES * 2:
            _terminate_proc(proc)
            return None, "No speech detected (or recording too short); try louder input or adjust thresholds"

        if thresh is None and calib_rms:
            med = float(statistics.median(calib_rms))
            thresh = max(med * float(speech_rms_multiplier), 60.0)

        _terminate_proc(proc)
        if proc.stderr:
            try:
                err = proc.stderr.read().decode(errors="replace")[:400]
                if err.strip():
                    logger.debug(f"[DiscoverMissingTasks] arecord stderr: {err.strip()}")
            except Exception:
                pass

        return bytes(out), None
    except Exception as e:
        logger.exception("[DiscoverMissingTasks] silence-end recording failed")
        _terminate_proc(proc)
        return None, str(e)


def listen_for_transcript(
    logger: Any,
    *,
    duration_seconds: float = 5.0,
    device: str,
    transcribe_model: str,
    is_cancelled: Callable[[], bool],
    end_on_silence: bool = False,
    max_duration_seconds: float = 30.0,
    silence_ms_after_speech: int = 750,
    min_speech_ms: int = 200,
    noise_calibration_ms: int = 400,
    speech_rms_multiplier: float = 4.0,
    pre_roll_ms: int = 300,
) -> tuple[str | None, str | None]:
    """
    Record mono audio then transcribe.

    ``is_cancelled`` is polled while recording (e.g. parent skill cancel).

    If ``end_on_silence`` is False (default), records for ``duration_seconds`` (clamped 1–30).

    If ``end_on_silence`` is True, records until ``silence_ms_after_speech`` of low-RMS audio after
    at least ``min_speech_ms`` of speech, or until ``max_duration_seconds`` (hard cap).

    Returns:
        ``(transcript, None)`` on success with non-empty text,
        ``(None, error)`` on failure,
        ``(None, CANCELLED sentinel)`` if cancelled — use :func:`listen_was_cancelled`.
    """
    dev = (device or "default").strip()
    if dev == "default":
        detected = _detect_alsa_capture_device(logger)
        if detected:
            dev = detected

    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        if end_on_silence:
            cap = max(2.0, min(float(max_duration_seconds), 120.0))
            pcm, err = _record_until_silence(
                logger,
                dev,
                is_cancelled,
                max_duration_seconds=cap,
                silence_ms_after_speech=max(200, int(silence_ms_after_speech)),
                min_speech_ms=max(50, int(min_speech_ms)),
                noise_calibration_ms=max(100, int(noise_calibration_ms)),
                speech_rms_multiplier=float(speech_rms_multiplier),
                pre_roll_ms=max(0, int(pre_roll_ms)),
            )
            if listen_was_cancelled(err):
                return None, _CANCELLED
            if err:
                return None, err
            assert pcm is not None
            _pcm16_mono_to_wav(wav_path, pcm)
        else:
            dur = max(1.0, min(float(duration_seconds), 30.0))
            dur_int = int(round(dur))
            wav_bytes, err = _record_fixed_duration(logger, dev, dur_int, is_cancelled)
            if listen_was_cancelled(err):
                return None, _CANCELLED
            if err:
                return None, err
            assert wav_bytes is not None
            with open(wav_path, "wb") as wf:
                wf.write(wav_bytes)

        if not os.path.isfile(wav_path) or os.path.getsize(wav_path) < 100:
            return None, "Recording empty or too short"

        text, err = _transcribe_wav(logger, wav_path, transcribe_model)
        if err:
            return None, f"Transcription failed: {err}"
        if not text:
            return None, "Transcription empty (nothing recognized)"
        return text, None
    finally:
        try:
            if os.path.isfile(wav_path):
                os.unlink(wav_path)
        except OSError:
            pass


def listen_was_cancelled(err: str | None) -> bool:
    return err == _CANCELLED


def _coerce_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


class DiscoverMissingTasks(Skill):
    """Record audio, transcribe, run semantic task analysis on the transcript."""

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "discover_missing_tasks"

    def guidelines(self):
        return (
            "Same as user_listener (mic → transcript), then semantic_skill_analyzer.analyze on the text. "
            'On success message is JSON: {"transcript": "<text>"}. '
            "Parameters: duration_seconds, end_on_silence, silence tuning like user_listener. "
            "Requires arecord and proxy."
        )

    def execute(
        self,
        duration_seconds: float = 5.0,
        device: str = "default",
        transcribe_model: str = "whisper-1",
        end_on_silence: bool = False,
        max_duration_seconds: float = 30.0,
        silence_ms_after_speech: int = 750,
        min_speech_ms: int = 200,
        noise_calibration_ms: int = 400,
        speech_rms_multiplier: float = 4.0,
        pre_roll_ms: int = 300,
        **_kwargs,
    ):
        self._cancelled = False

        if self.node is None:
            return "Skill has no ROS node", SkillResult.FAILURE

        text, err = listen_for_transcript(
            self.logger,
            duration_seconds=duration_seconds,
            device=device,
            transcribe_model=transcribe_model,
            is_cancelled=lambda: self._cancelled,
            end_on_silence=_coerce_bool(end_on_silence, default=False),
            max_duration_seconds=max_duration_seconds,
            silence_ms_after_speech=silence_ms_after_speech,
            min_speech_ms=min_speech_ms,
            noise_calibration_ms=noise_calibration_ms,
            speech_rms_multiplier=speech_rms_multiplier,
            pre_roll_ms=pre_roll_ms,
        )

        if listen_was_cancelled(err):
            return "Recording cancelled", SkillResult.CANCELLED
        if err:
            return err, SkillResult.FAILURE

        assert text is not None
        analyze_gemma(text, api_key="YOUR_GEMINI_API_KEY")
        payload = json.dumps({"transcript": text}, ensure_ascii=False)
        self.logger.info(f"[DiscoverMissingTasks] Transcript: {text!r}")
        return payload, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Discover missing tasks cancel requested"
