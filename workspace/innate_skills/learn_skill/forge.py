# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The coding model behind learn_skill: a streamed chat through the Innate proxy that keeps its rounds."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING

from httpx import HTTPError

import innate
from brain_client.common.script_paths import get_innate_skills_dir

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

SERVICE = "gemini"
ENDPOINT = "/v1/chat/completions"
MODEL = "gemini-3.6-flash"
EXEMPLARS = ("head_emotion.py", "turn_in_place.py")
_FENCE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)

RULES = """\
You write skills for MARS, a small home robot with a wheeled base, a tilting head with a camera, \
a five-joint arm with a gripper, and a speaker. A skill is one Python file defining exactly one \
`Skill` subclass; the file is installed on the robot and run once as a trial, unchanged.

Rules:
- Import only from `innate`, `innate_skills`, and the standard library (math, random, json, \
typing, dataclasses, enum, collections; `time` for measuring only).
- The class docstring is what the robot's brain reads to decide when to call the skill: what it \
does and when to use it, in two sentences.
- Every execute() parameter has a default that gives a good demonstration; the trial calls \
execute() with no inputs.
- Pause with self.sleep(seconds), never time.sleep. Speak with self.say(text). End a failed run \
with self.fail(message); otherwise return a short result message.
- Keep motion small and deliberate: turns under 180 degrees, drives under 0.5 m, head angles \
between -30 and 30 degrees, arm moves through Manipulation within its documented reach.
- No comments and no prints.

Reply with the complete file in one ```python block and nothing else.
"""


def system_prompt() -> str:
    exemplars = "\n\n".join(
        f"## {name}\n```python\n{(get_innate_skills_dir() / name).read_text()}```" for name in EXEMPLARS
    )
    return f"{RULES}\n# The `innate` API\n{innate.__doc__}\n\n# Example skills\n{exemplars}"


class ForgeUnreachable(Exception):
    """The coding model could not be reached; the round is worth retrying."""


class Forge:
    def __init__(self, client: ProxyClient, system: str):
        self._client = client
        self._messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    def ask(self, prompt: str) -> Iterator[str]:
        """Stream the reply text; the exchange stays in the conversation for the next round."""
        self._messages.append({"role": "user", "content": prompt})
        reply: list[str] = []
        for delta in self._stream():
            reply.append(delta)
            yield delta
        self._messages.append({"role": "assistant", "content": "".join(reply)})

    def _stream(self) -> Iterator[str]:
        body = {"model": MODEL, "temperature": 0.2, "stream": True, "messages": self._messages}
        try:
            with self._client.request_stream(SERVICE, ENDPOINT, json=body, timeout=180.0) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    choices = json.loads(line[len("data: ") :]).get("choices") or []
                    delta = choices[0].get("delta", {}).get("content") if choices else None
                    if delta:
                        yield delta
        except (HTTPError, OSError) as error:
            raise ForgeUnreachable(f"the coding model was unreachable ({error})") from error


def extract_code(reply: str) -> str:
    """The fenced python block of a reply, or the whole reply when there is no fence."""
    match = _FENCE.search(reply)
    return match.group(1) if match else reply
