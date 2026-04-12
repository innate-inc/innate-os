#!/usr/bin/env python3
"""
Hear and greet — record mic audio, transcribe via proxy OpenAI, write transcript to a file
in the skills folder; optionally speak it via say_text (TTS on /brain/tts).
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


# Match micro_input defaults where practical (PCM16 mono); 16 kHz is fine for Whisper.
_RECORD_FORMAT = ["-f", "S16_LE", "-r", "16000", "-c", "1"]
_RECORD_TYPE = "wav"


def _detect_alsa_capture_device(logger) -> str | None:
    """
    Pick a capture device the same way as inputs/micro_input.py (plughw:X,Y).
    On some Jetson images, ALSA 'default' resolves to a broken APE node; an explicit
    plughw from `arecord -l` usually works.
    """
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
        logger.warning(f"[HearAndGreet] arecord -l failed: {e}")
        return None

    logger.info(f"[HearAndGreet] ALSA capture devices: {[d['name'] for d in devices]}")
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
        logger.info(f"[HearAndGreet] Using ALSA device {chosen['id']} ({chosen['name']})")
        return chosen["id"]
    return None


def _load_say_text_class():
    """Load sibling module say_text.py (skills dir is not a Python package)."""
    path = Path(__file__).resolve().parent / "say_text.py"
    spec = importlib.util.spec_from_file_location("innate_skills_say_text", path)
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load say_text.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SayText


_SKILLS_DIR = Path(__file__).resolve().parent
_TRANSCRIPT_FILE = _SKILLS_DIR / "JOSE_HERE.txt"


class HearAndGreet(Skill):
    """Record audio, transcribe, save to JOSE_HERE.txt; optionally speak via say_text."""

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._arecord_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def name(self):
        return "hear_and_greet"

    def guidelines(self):
        return (
            "Listen to the user for a few seconds, transcribe what they said, save the text to "
            "JOSE_HERE.txt in the skills folder. Optional speak (default true): if true, speak "
            "the transcript via TTS (say_text); if false, only save the file. Other options: "
            "duration_seconds (default 5), alsa device (use 'default' to auto-pick from `arecord -l`, "
            "same heuristics as the micro input), transcribe_model (default 'whisper-1'). Requires "
            "arecord and proxy credentials."
        )

    def _wait_arecord(self, proc: subprocess.Popen, wav_path: str) -> tuple[bool, str]:
        """Returns (ok, stderr_fragment_on_failure)."""
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
                    self.logger.error(f"[HearAndGreet] arecord exited {code}: {err}")
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
            self.logger.exception("[HearAndGreet] transcription request failed")
            return None, str(e)
        finally:
            try:
                proxy.close()
            except Exception:
                pass

    def execute(
        self,
        duration_seconds: float = 5.0,
        device: str = "default",
        transcribe_model: str = "whisper-1",
        speak: bool = True,
        **_kwargs,
    ):
        self._cancelled = False
        dur = max(1.0, min(float(duration_seconds), 30.0))
        dur_int = int(round(dur))
        if speak is None:
            do_speak = True
        elif isinstance(speak, bool):
            do_speak = speak
        else:
            do_speak = str(speak).strip().lower() in ("1", "true", "yes", "on")

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
            self.logger.info(f"[HearAndGreet] Recording {dur_int}s from -D {dev!r}: {' '.join(cmd)}")
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

            _TRANSCRIPT_FILE.write_text(text, encoding="utf-8")
            self.logger.info(f"[HearAndGreet] Wrote transcript to {_TRANSCRIPT_FILE}: {text!r}")

            if not do_speak:
                self.logger.info("[HearAndGreet] speak is false; skipping TTS")
                return (
                    f"Saved transcript to {_TRANSCRIPT_FILE} (not spoken).",
                    SkillResult.SUCCESS,
                )

            try:
                SayText = _load_say_text_class()
            except Exception as e:
                self.logger.exception("[HearAndGreet] failed to load say_text")
                return (
                    f"Transcribed and saved to {_TRANSCRIPT_FILE}, but could not load say_text: {e}",
                    SkillResult.FAILURE,
                )

            st = SayText(self.logger)
            st.node = self.node
            self.logger.info(f"[HearAndGreet] Transcript -> say_text TTS: {text!r}")
            tts_msg, tts_status = st.execute(text=text)
            if tts_status != SkillResult.SUCCESS:
                return (
                    f"Saved to {_TRANSCRIPT_FILE}; TTS failed ({tts_msg})",
                    tts_status,
                )
            return f"Saved to {_TRANSCRIPT_FILE}. {tts_msg}", SkillResult.SUCCESS
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
        return "Hear and greet cancel requested"
