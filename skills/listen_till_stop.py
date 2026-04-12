#!/usr/bin/env python3
"""
Listen until the user signals they want to stop.

Repeatedly records with silence-end (same pipeline as ``user_listener``), transcribes each
utterance, then decides whether the user wants to **end the listening session**. Detection is
**hybrid**:

1. **Keyword / pattern pass** — obvious phrases (``stop``, ``enough``, ``cancel``, ``never mind``,
   ``don't do that anymore``, …) with a few anti-patterns (``don't stop``, ``keep going``).
2. **LLM pass** (optional, default on) — small chat model classifies paraphrases and edge cases;
   uses the same Innate proxy as transcription.

On success the result ``message`` is JSON:
``{"stopped_by_user": bool, "reason": "...", "utterances": ["..."], "combined": "..."}``.

``reason`` is one of: ``user_intent``, ``max_turns``, ``max_total_seconds``, ``cancelled``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import time
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

try:
    from innate_proxy import ProxyClient
except ImportError:
    ProxyClient = None  # type: ignore[misc, assignment]

_STOP_REASON_USER = "user_intent"
_STOP_REASON_TURNS = "max_turns"
_STOP_REASON_TIME = "max_total_seconds"
_STOP_REASON_CANCEL = "cancelled"


def _coerce_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _keyword_stop_intent(transcript: str) -> bool | None:
    """
    Return True if transcript clearly requests stop, False if clearly continue, None if unclear.
    """
    s = transcript.strip().lower()
    if not s:
        return None

    if re.search(r"\b(don'?t|do not)\s+stop\b", s):
        return False
    if re.search(r"\bkeep\s+(going|listening|talking)\b", s):
        return False
    if re.search(r"\bgo\s+on\b", s):
        return False

    if re.fullmatch(r"stop[\s!.]*", s):
        return True
    if re.search(
        r"\b("
        r"stop\s+(listening|that|it|now)|"
        r"shut\s+up|quiet\s+down|enough|quit|exit|abort|cancel|"
        r"no\s+more|never\s*mind|forget\s+(that|it)|"
        r"that'?s\s+enough|we'?re\s+done|i'?m\s+done|"
        r"don'?t\s+do\s+(that|it)\s+any\s*more|"
        r"do\s+not\s+do\s+(that|it)\s+any\s*more"
        r")\b",
        s,
    ):
        return True
    return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    t = text.strip()
    if "```" in t:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t, re.IGNORECASE)
        if m:
            t = m.group(1).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", t)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


def _llm_stop_intent(logger: Any, transcript: str, intent_model: str) -> tuple[bool | None, str | None]:
    """
    Ask a small model whether the user wants to end the listening session.
    Returns (want_stop, error_message).
    """
    if ProxyClient is None:
        return None, "innate_proxy not available"
    proxy = ProxyClient()
    if not proxy.is_available():
        return None, "Proxy not configured (set INNATE_PROXY_URL and INNATE_SERVICE_KEY)"

    url = f"{proxy.proxy_url}/v1/services/openai/v1/chat/completions"
    client = proxy.get_sync_client()
    body = {
        "model": intent_model,
        "temperature": 0,
        "max_tokens": 64,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You decide if the user wants to END an ongoing microphone listening session "
                    "(e.g. stop, enough, cancel, never mind, we're done, paraphrases). "
                    'Reply with ONLY JSON: {"end_session": true} or {"end_session": false}. '
                    "If they are giving normal instructions or continuing conversation, end_session "
                    "must be false."
                ),
            },
            {"role": "user", "content": transcript},
        ],
    }
    try:
        resp = client.post(url, json=body)
        if resp.status_code >= 400:
            return None, f"chat completions HTTP {resp.status_code}: {resp.text[:400]}"
        data = resp.json()
        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        parsed = _extract_json_object(raw)
        if not parsed or "end_session" not in parsed:
            logger.warning(f"[ListenTillStop] LLM returned unparseable content: {raw!r}")
            return None, "LLM response not valid JSON"
        v = parsed["end_session"]
        if not isinstance(v, bool):
            return None, "LLM end_session not boolean"
        return v, None
    except Exception as e:
        logger.exception("[ListenTillStop] chat completions failed")
        return None, str(e)
    finally:
        try:
            proxy.close()
        except Exception:
            pass


def _should_stop_listening(
    logger: Any,
    transcript: str,
    *,
    use_llm: bool,
    intent_model: str,
) -> tuple[bool, str]:
    """
    Returns (stop, source) where source is 'keyword', 'llm', or 'llm_fallback_no'.
    """
    kw = _keyword_stop_intent(transcript)
    if kw is True:
        return True, "keyword"
    if kw is False:
        return False, "keyword"
    if not use_llm:
        return False, "keyword_unclear_no_llm"

    want, err = _llm_stop_intent(logger, transcript, intent_model)
    if err:
        logger.warning(f"[ListenTillStop] LLM stop check failed ({err}); treating as not stop")
        return False, "llm_error"
    if want:
        return True, "llm"
    return False, "llm"


class ListenTillStop(Skill):
    """Multi-turn listen (silence-segmented) until stop intent or limits."""

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "listen_till_stop"

    def guidelines(self):
        return (
            "Listen in a loop using silence-end recording (see user_listener). After each utterance, "
            "detect whether the user wants to stop the session (keywords and optional LLM via proxy). "
            "Success JSON: stopped_by_user, reason (user_intent | max_turns | max_total_seconds | "
            "cancelled), utterances[], combined. Params: max_turns, max_total_seconds, use_llm_for_stop, "
            "intent_model, transcribe_model, end_on_silence tuning same as user_listener. Requires "
            "arecord and proxy; LLM needs OpenAI path on proxy when use_llm_for_stop is true."
        )

    def execute(
        self,
        max_turns: int = 80,
        max_total_seconds: float = 600.0,
        device: str = "default",
        transcribe_model: str = "whisper-1",
        use_llm_for_stop: bool = True,
        intent_model: str = "gpt-4o-mini",
        end_on_silence: bool = True,
        max_duration_seconds: float = 25.0,
        silence_ms_after_speech: int = 900,
        min_speech_ms: int = 150,
        noise_calibration_ms: int = 1500,
        speech_rms_multiplier: float = 6.0,
        pre_roll_ms: int = 300,
        **_kwargs,
    ):
        self._cancelled = False
        if self.node is None:
            return "Skill has no ROS node", SkillResult.FAILURE

        turns_cap = max(1, min(int(max_turns), 500))
        wall_cap = max(5.0, min(float(max_total_seconds), 7200.0))
        use_llm = _coerce_bool(use_llm_for_stop, default=True)
        eos = _coerce_bool(end_on_silence, default=True)

        utterances: list[str] = []
        t0 = time.monotonic()

        for turn in range(turns_cap):
            if self._cancelled:
                payload = json.dumps(
                    {
                        "stopped_by_user": False,
                        "reason": _STOP_REASON_CANCEL,
                        "utterances": utterances,
                        "combined": " ".join(utterances).strip(),
                    },
                    ensure_ascii=False,
                )
                return payload, SkillResult.CANCELLED

            if time.monotonic() - t0 >= wall_cap:
                payload = json.dumps(
                    {
                        "stopped_by_user": False,
                        "reason": _STOP_REASON_TIME,
                        "utterances": utterances,
                        "combined": " ".join(utterances).strip(),
                    },
                    ensure_ascii=False,
                )
                return payload, SkillResult.SUCCESS

            text, err = listen_for_transcript(
                self.logger,
                duration_seconds=5.0,
                device=device,
                transcribe_model=transcribe_model,
                is_cancelled=lambda: self._cancelled,
                end_on_silence=eos,
                max_duration_seconds=max_duration_seconds,
                silence_ms_after_speech=silence_ms_after_speech,
                min_speech_ms=min_speech_ms,
                noise_calibration_ms=noise_calibration_ms,
                speech_rms_multiplier=speech_rms_multiplier,
                pre_roll_ms=pre_roll_ms,
            )

            if listen_was_cancelled(err):
                payload = json.dumps(
                    {
                        "stopped_by_user": False,
                        "reason": _STOP_REASON_CANCEL,
                        "utterances": utterances,
                        "combined": " ".join(utterances).strip(),
                    },
                    ensure_ascii=False,
                )
                return payload, SkillResult.CANCELLED
            if err:
                return err, SkillResult.FAILURE

            assert text is not None
            utterances.append(text)
            self.logger.info(
                f"[ListenTillStop] turn {turn + 1}/{turns_cap}: {text!r} "
                f"(elapsed {time.monotonic() - t0:.1f}s)"
            )

            stop, src = _should_stop_listening(
                self.logger,
                text,
                use_llm=use_llm,
                intent_model=intent_model,
            )
            self.logger.info(f"[ListenTillStop] stop={stop} via {src}")
            if stop:
                payload = json.dumps(
                    {
                        "stopped_by_user": True,
                        "reason": _STOP_REASON_USER,
                        "utterances": utterances,
                        "combined": " ".join(utterances).strip(),
                    },
                    ensure_ascii=False,
                )
                return payload, SkillResult.SUCCESS

        payload = json.dumps(
            {
                "stopped_by_user": False,
                "reason": _STOP_REASON_TURNS,
                "utterances": utterances,
                "combined": " ".join(utterances).strip(),
            },
            ensure_ascii=False,
        )
        return payload, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Listen till stop cancel requested"
