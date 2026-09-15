#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The function API skills used before ``llm: Llm`` — skills in the field import it, so it stays.

New code declares the interface (``from innate import Llm``); the name here is
historical — the model is whatever the robot is set to, or the one named.
"""

from __future__ import annotations

from collections.abc import Sequence

from brain_client.robot.llm import Llm, robot_default


def make_client(model: str | None = None) -> Llm | None:
    """The model — the robot's, or the ``vendor:name`` given — or None if no route to it is configured."""
    llm = Llm(model) if model else robot_default()
    return llm if llm.available else None


def ask_image(
    client: Llm | None, images_b64: str | Sequence[str], question: str, logger=None, retries: int = 3
) -> str | None:
    """JPEG(s) + question -> reply text. None if no client / all retries fail."""
    if client is None:
        return None
    return client.ask(images_b64, question, logger=logger, retries=retries)
