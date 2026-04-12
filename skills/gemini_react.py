#!/usr/bin/env python3
"""
Gemini React Skill — Use Gemini Vision API to decide robot action + speech.

Given the current camera frame (and/or a text situation description),
Gemini decides:
  - Which head emotion to perform  (action)
  - What the robot should say       (speech)

Requires GEMINI_API_KEY in the environment.
"""

import json
import os
import subprocess
import time

import requests
from std_msgs.msg import String

from brain_client.skill_types import Interface, InterfaceType, RobotState, RobotStateType, Skill, SkillResult


GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
)

_DECISION_PROMPT = """You are the brain of a small humanoid robot called Maurice.
Analyse the situation (image and/or description below) and decide:
1. Which head action to perform.
2. What the robot should say out loud (1–2 short sentences, natural speech).

Respond ONLY with valid JSON — no markdown, no extra text:
{
  "action": "<one of: happy | surprised | thinking | excited | sad | agreeing | disagreeing | none>",
  "speech": "<text the robot says>",
  "reason": "<one short sentence explaining your choice>"
}

Available actions:
  happy      – quick upward nods (pleasant situation)
  surprised  – quick jolt upward (unexpected)
  thinking   – slow thoughtful tilt (processing / unsure)
  excited    – rapid bouncing (very happy / high energy)
  sad        – slow droop (something is wrong)
  agreeing   – nodding yes
  disagreeing – shaking no
  none       – no movement needed
"""

# Head tilt sequences reused from head_emotion.py to avoid cross-imports
_HEAD_SEQUENCES: dict[str, list[tuple[int, float]]] = {
    "happy":       [(5, 0.12), (-5, 0.12), (10, 0.15), (-5, 0.12), (10, 0.15), (0, 0.18)],
    "sad":         [(0, 0.3), (-5, 0.35), (-10, 0.4), (-15, 0.4), (-20, 0.45), (-25, 0.5), (-25, 0.3)],
    "excited":     [(10, 0.08), (-10, 0.08), (15, 0.1), (-15, 0.1), (10, 0.08), (-10, 0.08), (15, 0.1), (0, 0.12)],
    "thinking":    [(5, 0.3), (10, 0.35), (15, 0.4), (15, 0.6), (10, 0.3), (5, 0.25), (-5, 0.2), (0, 0.3)],
    "surprised":   [(15, 0.08), (15, 0.15), (10, 0.15), (5, 0.15), (0, 0.2)],
    "agreeing":    [(-10, 0.15), (5, 0.15), (-10, 0.15), (5, 0.15), (-10, 0.15), (5, 0.15), (0, 0.2)],
    "disagreeing": [(-5, 0.18), (5, 0.18), (-8, 0.2), (8, 0.2), (-5, 0.18), (5, 0.18), (0, 0.2)],
}

_INTERP_HZ = 30


class GeminiReact(Skill):
    """Ask Gemini what to do, then act + speak accordingly."""

    head = Interface(InterfaceType.HEAD)
    image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        self._api_key: str = os.environ.get("GEMINI_API_KEY", "")

    @property
    def name(self):
        return "gemini_react"

    def guidelines(self):
        return (
            "Use Gemini Vision AI to analyse the environment and decide what action to take "
            "and what to say. "
            "Optionally pass 'situation' (str) to describe the current context in words. "
            "Automatically uses the live camera image when available. "
            "Returns the speech Gemini chose as the skill result."
        )

    # ------------------------------------------------------------------
    # Main execute
    # ------------------------------------------------------------------

    def execute(self, situation: str = ""):
        self._cancelled = False

        if not self._api_key:
            self.logger.warning("[GeminiReact] GEMINI_API_KEY is not set — using fallback response")

        self._send_feedback("Analysing situation with Gemini…")

        decision = self._ask_gemini(situation=situation, image_b64=self.image)

        action = decision.get("action", "none")
        speech = decision.get("speech", "")
        reason = decision.get("reason", "")

        self.logger.info(f"[GeminiReact] action={action!r}  speech={speech!r}  reason={reason!r}")
        self._send_feedback(f"Action: {action} | Speech: {speech}")

        if self._cancelled:
            return "Cancelled before acting", SkillResult.CANCELLED

        # Speak and move head concurrently (speech is fire-and-forget)
        if speech:
            self._say(speech)

        if action != "none":
            interrupted = self._play_action(action)
            if interrupted:
                return "Gemini react cancelled during animation", SkillResult.CANCELLED

        return speech or f"Performed {action}", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Gemini react cancelled"

    # ------------------------------------------------------------------
    # Gemini API call
    # ------------------------------------------------------------------

    def _ask_gemini(self, situation: str, image_b64: str | None) -> dict:
        """Call Gemini and return the parsed JSON decision dict."""

        # Fallback when no API key
        if not self._api_key:
            return {
                "action": "thinking",
                "speech": "I need a Gemini API key to make smart decisions.",
                "reason": "missing API key",
            }

        parts: list[dict] = []

        # Attach camera image if available
        if image_b64:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": image_b64,
                    }
                }
            )

        # Build the text prompt
        prompt = _DECISION_PROMPT
        if situation:
            prompt += f"\n\nSituation: {situation}"
        elif not image_b64:
            prompt += "\n\nNo image or situation provided — give a neutral friendly default."

        parts.append({"text": prompt})

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 256,
            },
        }

        try:
            resp = requests.post(
                f"{GEMINI_API_URL}?key={self._api_key}",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

            # Strip optional markdown code fences
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(
                    line for line in lines if not line.startswith("```")
                ).strip()

            return json.loads(raw)

        except requests.exceptions.Timeout:
            self.logger.warning("[GeminiReact] Gemini API timed out")
            return {"action": "thinking", "speech": "I am taking a moment to think.", "reason": "timeout"}
        except json.JSONDecodeError as e:
            self.logger.error(f"[GeminiReact] Could not parse Gemini response as JSON: {e}")
            return {"action": "none", "speech": "I got confused for a second.", "reason": "json parse error"}
        except Exception as e:
            self.logger.error(f"[GeminiReact] Gemini API error: {e}")
            return {"action": "none", "speech": "I encountered an error while thinking.", "reason": str(e)}

    # ------------------------------------------------------------------
    # Head animation
    # ------------------------------------------------------------------

    def _play_action(self, action: str) -> bool:
        """Play the head tilt sequence for the given action. Returns True if cancelled."""
        if self.head is None:
            self.logger.warning("[GeminiReact] Head interface not available")
            return False

        sequence = _HEAD_SEQUENCES.get(action)
        if sequence is None:
            self.logger.warning(f"[GeminiReact] Unknown action '{action}' — skipping animation")
            return False

        dt = 1.0 / _INTERP_HZ
        current = 0.0
        for target, duration in sequence:
            if self._cancelled:
                self.head.set_position(0)
                return True
            steps = max(1, int(round(duration * _INTERP_HZ)))
            for i in range(1, steps + 1):
                if self._cancelled:
                    self.head.set_position(0)
                    return True
                t = i / steps
                angle = current + (target - current) * t
                self.head.set_position(int(round(angle)))
                time.sleep(dt)
            current = float(target)

        self.head.set_position(0)
        return False

    # ------------------------------------------------------------------
    # Voice output
    # ------------------------------------------------------------------

    def _say(self, text: str, speed: int = 150):
        """Publish to /brain/tts (Cartesia), fall back to espeak."""
        if self.node is not None:
            try:
                pub = self.node.create_publisher(String, "/brain/tts", 10)
                msg = String()
                msg.data = text
                pub.publish(msg)
                self.logger.info(f"[GeminiReact] Published to /brain/tts: '{text[:60]}'")
                return
            except Exception as e:
                self.logger.warning(f"[GeminiReact] /brain/tts publish failed ({e}), falling back to espeak")

        try:
            subprocess.Popen(
                ["espeak", "-s", str(speed), "--", text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self.logger.warning("[GeminiReact] espeak not found — voice output skipped")
        except Exception as e:
            self.logger.error(f"[GeminiReact] espeak error: {e}")
