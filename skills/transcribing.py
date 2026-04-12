#!/usr/bin/env python3
"""
Transcribing — record from the mic for 5 seconds, transcribe via proxy OpenAI, write to JOSE_HERE.txt.
"""

import importlib.util
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from brain_client.skill_types import Skill, SkillResult

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
LISTEN_SECONDS = 5

_SKILLS_DIR = Path(__file__).resolve().parent
_OUTPUT_FILE = _SKILLS_DIR / "JOSE_HERE.txt"


def _detect_alsa_capture_device(logger) -> str | None:
    """Pick ALSA capture device (same heuristics as inputs/micro_input.py)."""
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
        logger.warning(f"[Transcribing] arecord -l failed: {e}")
        return None

    logger.info(f"[Transcribing] ALSA capture devices: {[d['name'] for d in devices]}")
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
        logger.info(f"[Transcribing] Using ALSA device {chosen['id']} ({chosen['name']})")
        return chosen["id"]
    return None


class Transcribing(Skill):
    """Record environment audio, transcribe, save to JOSE_HERE.txt in the skills folder."""

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._arecord_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def name(self):
        return "transcribing"

    def guidelines(self):
        return (
            "Listen with the robot microphone for exactly 5 seconds, transcribe speech to text, "
            "and write the result to JOSE_HERE.txt under the skills folder. "
            "Optional: alsa device (default picks from `arecord -l`), transcribe_model "
            "(default whisper-1). Requires arecord and proxy credentials "
            "(INNATE_PROXY_URL, INNATE_SERVICE_KEY)."
        )

    def _wait_arecord(self, proc: subprocess.Popen, wav_path: str) -> tuple[bool, str]:
        while True:
            if self._cancelled:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    if os.path.isfile(wav_path):
                        os.unlink(wav_path)
                except OSError:
                    pass
                return False, ""
            code = proc.poll()
            if code is not None:
                err = ""
                if proc.stderr:
                    try:
                        err = proc.stderr.read().decode(errors="replace")[:800]
                    except Exception:
                        pass
                if code != 0:
                    self.logger.error(f"[Transcribing] arecord exited {code}: {err}")
                    return False, err.strip()
                return True, ""
            time.sleep(0.05)

    def _transcribe(self, wav_path: str, transcribe_model: str) -> tuple[str | None, str | None]:
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
            data, err = _tx_http.transcription_json_from_response(resp, self.logger)
            if err:
                return None, err
            text = (data.get("text") or "").strip()
            return text, None
        except Exception as e:
            self.logger.exception("[Transcribing] transcription request failed")
            return None, str(e)
        finally:
            try:
                proxy.close()
            except Exception:
                pass

    def execute(
        self,
        device: str = "default",
        transcribe_model: str = "whisper-1",
        **_ignored,
    ):
        self._cancelled = False
        dur_int = LISTEN_SECONDS

        if self.node is None:
            return "Skill has no ROS node", SkillResult.FAILURE

        dev = (device or "default").strip()
        if dev == "default":
            detected = _detect_alsa_capture_device(self.logger)
            if detected:
                dev = detected

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
            self.logger.info(f"[Transcribing] Recording {dur_int}s from -D {dev!r}: {' '.join(cmd)}")
            with self._lock:
                self._arecord_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            proc = self._arecord_proc
            ok, arecord_err = self._wait_arecord(proc, wav_path)
            with self._lock:
                self._arecord_proc = None

            if self._cancelled:
                return "Recording cancelled", SkillResult.CANCELLED
            if not ok:
                detail = f": {arecord_err}" if arecord_err else ""
                return f"Recording failed (arecord){detail}", SkillResult.FAILURE

            if not os.path.isfile(wav_path) or os.path.getsize(wav_path) < 100:
                return "Recording empty or too short", SkillResult.FAILURE

            text, err = self._transcribe(wav_path, transcribe_model=transcribe_model)
            if err:
                return f"Transcription failed: {err}", SkillResult.FAILURE
            if not text:
                return "Transcription empty (nothing recognized)", SkillResult.FAILURE

            _OUTPUT_FILE.write_text(text, encoding="utf-8")
            self.logger.info(f"[Transcribing] Wrote transcript to {_OUTPUT_FILE}: {text!r}")
            return f"Wrote transcript to {_OUTPUT_FILE}", SkillResult.SUCCESS
        finally:
            try:
                if os.path.isfile(wav_path):
                    os.unlink(wav_path)
            except OSError:
                pass

    def cancel(self):
        self._cancelled = True
        with self._lock:
            proc = self._arecord_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        return "Transcribing cancel requested"
