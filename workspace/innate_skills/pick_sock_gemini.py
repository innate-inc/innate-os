# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.pick_sock_fast import PickSockFast

from innate import Llm


class PickSockGemini(PickSockFast):
    """Approach and pick the described floor sock using Gemini 3.6 detection."""

    llm: Llm = Llm("google:gemini-3.6-flash", thinking="minimal", extra_body="{}")
