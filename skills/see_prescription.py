#!/usr/bin/env python3
"""
See Prescription Skill -- reads a prescription held up to the camera using Gemini VLM.
No arm movement; just looks at the main camera image.
"""
import json
import os
from pathlib import Path

import google.generativeai as genai

from brain_client.skill_types import Skill, SkillResult, RobotState, RobotStateType


def _load_api_key() -> str:
    for env_path in [
        os.path.expanduser("~/skills/.env.scan"),
        os.path.expanduser("~/agents/.env.scan"),
        os.path.expanduser("~/.env"),
    ]:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        return line.split("=", 1)[1]
    return os.environ.get("GEMINI_API_KEY", "")


class SeePrescription(Skill):
    """Look at a prescription through the main camera and read it with Gemini VLM."""

    image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False
        api_key = _load_api_key()
        if api_key:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel("gemini-2.0-flash")
        else:
            self.model = None

    @property
    def name(self):
        return "see_prescription"

    def guidelines(self):
        return (
            "Use when the customer is holding up a prescription to the camera. "
            "Captures the current camera image and uses Gemini VLM to read and "
            "extract all medicine details from the prescription. "
            "Returns structured information about each medicine."
        )

    def execute(self):
        if not self.model:
            return "Gemini API key not configured", SkillResult.FAILURE

        if not self.image:
            return "No camera image available -- ask the customer to hold the prescription up", SkillResult.FAILURE

        self._cancelled = False
        self._send_feedback("Let me read your prescription...")

        prompt = (
            "Read this medical prescription carefully. Extract everything you can see.\n"
            "Return JSON with this structure:\n"
            "{\n"
            '  "patient_name": str or null,\n'
            '  "doctor_name": str or null,\n'
            '  "medicines": [\n'
            "    {\n"
            '      "name": str,\n'
            '      "dosage": str,\n'
            '      "frequency": str,\n'
            '      "duration": str,\n'
            '      "instructions": str\n'
            "    }\n"
            "  ],\n"
            '  "confidence": "high" or "medium" or "low"\n'
            "}\n"
            'If this is not a prescription or is unreadable, return: {"error": "reason"}'
        )

        # Try twice with the current image
        result = None
        for attempt in range(2):
            if self._cancelled:
                return "Cancelled", SkillResult.CANCELLED
            try:
                response = self.model.generate_content(
                    [
                        prompt,
                        {"mime_type": "image/jpeg", "data": self.image},
                    ],
                    generation_config=genai.GenerationConfig(
                        response_mime_type="application/json"
                    ),
                )
                parsed = json.loads(response.text.strip())
                if "error" not in parsed and parsed.get("medicines"):
                    result = parsed
                    break
            except Exception as e:
                self.logger.error(f"[SeePrescription] Attempt {attempt+1} failed: {e}")

        if result is None:
            return (
                "I could not read the prescription. "
                "Could you hold it a bit closer or adjust the angle?",
                SkillResult.FAILURE,
            )

        # Format a readable summary
        parts = []
        if result.get("patient_name"):
            parts.append(f"Prescription for {result['patient_name']}.")
        if result.get("doctor_name"):
            parts.append(f"Prescribed by Dr. {result['doctor_name']}.")

        medicines = result["medicines"]
        parts.append(f"I can see {len(medicines)} medicine(s):")
        for i, m in enumerate(medicines, 1):
            line = f"{i}. {m['name']}"
            if m.get("dosage"):
                line += f", {m['dosage']}"
            if m.get("frequency"):
                line += f", {m['frequency']}"
            if m.get("duration"):
                line += f" for {m['duration']}"
            if m.get("instructions"):
                line += f" ({m['instructions']})"
            parts.append(line)

        summary = " ".join(parts)
        self._send_feedback(summary)
        return summary, SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        return "Cancelled"
