# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The coding model behind learn_skill: a streamed chat through the Innate proxy that keeps its rounds.

Which model writes the skill is one setting, ``INNATE_LEARN_CODER="<proxy service>/<model>"``
(in the environment or the repo-root .env): ``openai/gpt-6-astra`` by default,
``gemini/gemini-3.6-flash`` for the cheaper fallback. Both speak OpenAI-style chat completions
through the proxy."""

from __future__ import annotations

import inspect
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from httpx import HTTPError

import innate
from brain_client.common.script_paths import get_innate_skills_dir
from innate import Manipulation

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

CODER_ENV = "INNATE_LEARN_CODER"
DEFAULT_CODER = "openai/gpt-6-astra"
REASONING_EFFORT = "low"  # keeps a draft under a minute; GPT-6 Astra does not take `none` (or temperature)
ENDPOINT = "/v1/chat/completions"
EXEMPLARS = ("head_emotion.py", "turn_in_place.py", "arm_rest_position.py")
ARM_CONSTANTS = ("JOINT_NAMES", "ZERO", "REST", "REACH_X", "REACH_Y", "GRIPPER_CLOSED", "GRIPPER_OPEN")
ARM_METHODS = ("move_joints", "rest", "move_to", "move_by", "reachable", "gripper_open", "gripper_close", "wait")
_FENCE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)
_MODULE_PREFIX = re.compile(r"\b(?:[a-z_]+\.)+(?=[A-Z])")

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
- Pause with self.sleep(seconds), never time.sleep. End a failed run with self.fail(message); \
otherwise return a short result message.
- Speak with self.say(text). Play a sound effect with self.play(description), the sound described \
in words: self.play("a small dog barking twice"). Both take wait=True to block until the audio \
has played.
- Keep motion small and deliberate: turns under 180 degrees, drives under 0.5 m, head angles \
between -30 and 30 degrees, arm poses inside the joint ranges below, ending at rest.
- No comments and no prints.

Reply with the complete file in one ```python block and nothing else.
"""

# Joint conventions are the URDF's: limits from mars.urdf, the joint2 floor from the arm driver's
# joint1-dependent clamp, and "j4 negative pitches UP" from Manipulation.REST's tuning notes.
ARM = """\
Declare `manipulation: Manipulation` on the class to move the arm. Joint targets are radians in \
JOINT_NAMES order: joint1 base yaw (+ turns left; -1.57..1.57), joint2 shoulder pitch (+ leans \
forward; -1.57..1.22, and the driver holds it above -0.5 while joint1 is within -1.0..1.0), joint3 \
elbow pitch (+ folds the forearm down, - raises it; -1.57..1.75), joint4 wrist pitch (+ down, - up; \
-1.92..1.75), joint5 wrist roll (-1.57..1.57), joint6 claw (0 closed .. 0.85 open). At ZERO the \
upper arm stands vertical and the forearm points straight ahead, level; REST is the arm folded \
against the body, where every skill starts and must end (self.manipulation.rest()). Five joint \
values keep the current grip. Cartesian poses are base_link metres: x forward, y left, z up; the \
graspable box is REACH_X by REACH_Y just above the floor. Moves block until the arm arrives and \
raise ArmFailed or ArmUnhealthy (both importable from innate) when it cannot.
"""


def system_prompt() -> str:
    exemplars = "\n\n".join(
        f"## {name}\n```python\n{(get_innate_skills_dir() / name).read_text()}```" for name in EXEMPLARS
    )
    return (
        f"{RULES}\n# The `innate` API\n{innate.__doc__}\n\n# The arm\n{arm_reference()}\n\n"
        f"# Example skills\n{exemplars}"
    )


def arm_reference() -> str:
    """Manipulation's skill-facing surface, read off the class so the prompt cannot drift from it."""
    constants = "\n".join(f"Manipulation.{name} = {getattr(Manipulation, name)!r}" for name in ARM_CONSTANTS)
    methods = "\n\n".join(_method_stub(name) for name in ARM_METHODS)
    return f"{ARM}```python\n{constants}\n\n{methods}\n```"


def _method_stub(name: str) -> str:
    method = getattr(Manipulation, name)
    signature = _MODULE_PREFIX.sub("", str(inspect.signature(method)))
    doc = (inspect.getdoc(method) or "").replace("\n", "\n    ")
    return f'def {name}{signature}:\n    """{doc}"""'


class ForgeUnreachable(Exception):
    """The coding model could not be reached; the round is worth retrying."""


@dataclass(frozen=True)
class Coder:
    """A proxy service and a model id, from ``INNATE_LEARN_CODER``."""

    service: str
    model: str

    @classmethod
    def from_env(cls) -> Coder:
        spec = os.environ.get(CODER_ENV, DEFAULT_CODER)
        service, _, model = spec.partition("/")
        if not service or not model:
            raise ValueError(f"{CODER_ENV} must be '<proxy service>/<model>', got {spec!r}")
        return cls(service, model)

    def request(self, messages: list[dict[str, str]]) -> dict[str, object]:
        body: dict[str, object] = {"model": self.model, "messages": messages, "stream": True}
        if self.service == "openai":
            body["reasoning_effort"] = REASONING_EFFORT
        else:
            body["temperature"] = 0.2
        return body


class Forge:
    def __init__(self, client: ProxyClient, coder: Coder, system: str):
        self._client = client
        self._coder = coder
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
        body = self._coder.request(self._messages)
        try:
            with self._client.request_stream(self._coder.service, ENDPOINT, json=body, timeout=180.0) as response:
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
