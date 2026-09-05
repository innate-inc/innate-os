# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The GPT cabinet agent's identical control loop, using Gemini instead."""

import os

from .cabinet_agent_policy import CabinetPolicy
from .cabinet_gemini_transport import GeminiCabinetTransport
from .open_cabinet_with_gpt import OpenCabinetWithGpt


class OpenCabinetWithGemini(OpenCabinetWithGpt):
    """Open the lower kitchen cabinet with Gemini 3.8 Flash and camera feedback.

    Same prompt, head/wrist views, telemetry, history, bounded actions, level
    checks and 40 cm pull guidance as open_cabinet_with_gpt. Start facing the
    cabinet within about 60 cm with a clear arm path and empty gripper.
    Requires INNATE_SERVICE_KEY with Gemini access. Experimental house skill.
    """

    def _make_policy(self):
        return CabinetPolicy(
            model=os.environ.get("INNATE_CABINET_GEMINI_MODEL", "gemini-3.8-flash"),
            transport=GeminiCabinetTransport(),
        )
