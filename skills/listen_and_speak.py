#!/usr/bin/env python3
"""
Listen and speak — use user_listener to transcribe speech, save to JOSE_HERE.txt, optionally
speak via speak_aloud (TTS on /brain/tts).
"""

import importlib.util
from pathlib import Path
from typing import Any

from brain_client.skill_types import Skill, SkillResult

_ul_spec = importlib.util.spec_from_file_location(
    "innate_skills_user_listener",
    Path(__file__).resolve().parent / "user_listener.py",
)
_ul_mod = importlib.util.module_from_spec(_ul_spec)
assert _ul_spec and _ul_spec.loader
_ul_spec.loader.exec_module(_ul_mod)
listen_for_transcript = _ul_mod.listen_for_transcript
listen_was_cancelled = _ul_mod.listen_was_cancelled


def _load_speak_aloud_class():
    """Load sibling module speak_aloud.py (skills dir is not a Python package)."""
    path = Path(__file__).resolve().parent / "speak_aloud.py"
    spec = importlib.util.spec_from_file_location("innate_skills_speak_aloud", path)
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load speak_aloud.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SpeakAloud


_SKILLS_DIR = Path(__file__).resolve().parent
_TRANSCRIPT_FILE = _SKILLS_DIR / "JOSE_HERE.txt"


class ListenAndSpeak(Skill):
    """Transcribe via user_listener, save to JOSE_HERE.txt; optionally speak via speak_aloud."""

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "listen_and_speak"

    def guidelines(self):
        return (
            "Listen to the user, transcribe (user_listener), save to JOSE_HERE.txt. Optional speak "
            "(default true): speak via speak_aloud or save only. Recording: end_on_silence false "
            "(default) uses duration_seconds (1–30s). end_on_silence true stops after the user "
            "finishes speaking (quiet tail), capped by max_duration_seconds; tune silence_ms_after_speech, "
            "min_speech_ms, etc. See user_listener guidelines. Requires arecord and proxy credentials."
        )

    def execute(
        self,
        duration_seconds: float = 5.0,
        device: str = "default",
        transcribe_model: str = "whisper-1",
        speak: bool = True,
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
        if speak is None:
            do_speak = True
        elif isinstance(speak, bool):
            do_speak = speak
        else:
            do_speak = str(speak).strip().lower() in ("1", "true", "yes", "on")

        def _bool(v: Any, default: bool = False) -> bool:
            if v is None:
                return default
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            return str(v).strip().lower() in ("1", "true", "yes", "on")

        if self.node is None:
            return "Skill has no ROS node", SkillResult.FAILURE

        text, err = listen_for_transcript(
            self.logger,
            duration_seconds=duration_seconds,
            device=device,
            transcribe_model=transcribe_model,
            is_cancelled=lambda: self._cancelled,
            end_on_silence=_bool(end_on_silence, False),
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

        _TRANSCRIPT_FILE.write_text(text, encoding="utf-8")
        self.logger.info(f"[ListenAndSpeak] Wrote transcript to {_TRANSCRIPT_FILE}: {text!r}")

        if not do_speak:
            self.logger.info("[ListenAndSpeak] speak is false; skipping TTS")
            return (
                f"Saved transcript to {_TRANSCRIPT_FILE} (not spoken).",
                SkillResult.SUCCESS,
            )

        try:
            SpeakAloud = _load_speak_aloud_class()
        except Exception as e:
            self.logger.exception("[ListenAndSpeak] failed to load speak_aloud")
            return (
                f"Transcribed and saved to {_TRANSCRIPT_FILE}, but could not load speak_aloud: {e}",
                SkillResult.FAILURE,
            )

        speaker = SpeakAloud(self.logger)
        speaker.node = self.node
        self.logger.info(f"[ListenAndSpeak] Transcript -> speak_aloud TTS: {text!r}")
        tts_msg, tts_status = speaker.execute(text=text)
        if tts_status != SkillResult.SUCCESS:
            return (
                f"Saved to {_TRANSCRIPT_FILE}; TTS failed ({tts_msg})",
                tts_status,
            )
        return f"Saved to {_TRANSCRIPT_FILE}. {tts_msg}", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Listen and speak cancel requested"
