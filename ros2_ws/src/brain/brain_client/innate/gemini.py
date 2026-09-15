#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The historical name of :mod:`innate.llm` — skills in the field import it, so it stays.

It was never Gemini-only once the model became a setting; new code imports ``innate.llm``.
"""

from innate.llm import ask_image, make_client

__all__ = ["ask_image", "make_client"]
