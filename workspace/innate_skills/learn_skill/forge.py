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
import queue
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, get_type_hints

from httpx import HTTPError

import innate
from brain_client.common.script_paths import get_innate_skills_dir
from innate import Head, HeadState, Llm, Manipulation, Mobility, Odometry

if TYPE_CHECKING:
    from innate_proxy import ProxyClient

CODER_ENV = "INNATE_LEARN_CODER"
DEFAULT_CODER = "openai/gpt-6-astra"
REASONING_EFFORT = "low"  # keeps a draft under a minute; GPT-6 Astra does not take `none` (or temperature)
ENDPOINT = "/v1/chat/completions"
HEARTBEAT_S = 0.2  # how long a silent model leaves a Stop unanswered
EXEMPLARS = ("head_emotion.py", "turn_in_place.py", "arm/arm_rest_position.py")
ARM_CONSTANTS = ("JOINT_NAMES", "ZERO", "REST", "REACH_X", "REACH_Y", "GRIPPER_CLOSED", "GRIPPER_OPEN")
ARM_METHODS = ("move_joints", "rest", "move_to", "move_by", "reachable", "gripper_open", "gripper_close", "wait")
HEAD_STATE = ("pitch_degrees", "min_degrees", "max_degrees", "default_degrees")
HEAD_METHODS = ("set_position",)
ODOMETRY = ("x", "y", "theta", "theta_degrees", "linear_velocity", "angular_velocity")
BASE_METHODS = ("send_cmd_vel", "rotate_in_place")
LLM_STATE = ("available",)
LLM_METHODS = ("ask",)
_FENCE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)
_REFUSAL = re.compile(r"^\s*CANNOT:\s*(.+?)\s*$", re.MULTILINE)
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
- Speak with self.say(text). Play a sound with self.play(description, seconds=...), the sound \
described in words — self.play("a small dog barking twice"); music by instruments, tempo and mood, \
self.play("a lilting orchestral waltz, oboe over soft strings, graceful", seconds=20). `seconds` \
(0.5-30) sets the clip's length; unset, the generator picks a few. Both take wait=True to block \
until the audio has played; without it the sound plays under what follows.
- A dance, a song or a performance has its music: start it with self.play(..., seconds=<how long \
the routine runs>) right before the moves, so they happen to it, and self.say only to introduce or \
close the act.
- Keep motion small and deliberate: turns under 180 degrees, drives under 0.5 m, head angles \
between -30 and 30 degrees, arm poses inside the joint ranges below, ending at rest.
- No comments and no prints.

Reply with the complete file in one ```python block and nothing else. If the interfaces above \
cannot do what is asked at all (a capability the robot lacks, not one you find undocumented), \
reply with the single line `CANNOT: <why, in one sentence>` and no file; never write a skill \
that only fails.
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

HEAD = """\
Declare `head: Head` on the class to tilt the head with self.head.set_position(degrees), and \
`head_position: HeadState` to read where it is (self.head_position.pitch_degrees). The head has one \
axis, pitch: negative looks down, 0 is level. set_position returns at once; self.sleep while it travels.
"""

BASE = """\
Declare `mobility: Mobility` on the class to drive the base with self.mobility, and `odom: Odometry` \
to read its pose in the odom frame (metres; theta in radians, counter-clockwise positive). Velocity \
commands return at once; give each a duration so the base stops by itself.
"""

VISION = """\
Declare `image: MainImage` (the head camera) or `wrist_image: WristImage` (the camera in the gripper) \
to read the newest frame; the value is the JPEG as base64 text, ready to hand to the model. Declare \
`llm: Llm` to ask the robot's model about a frame: self.llm.ask(self.image, "Is there a ball in view? \
Answer yes or no.") returns the reply text, or None when the model is unreachable. Ask short questions \
with a fixed answer format and parse the reply yourself; the model knows only the frame and the question.
"""


def system_prompt() -> str:
    exemplars = "\n\n".join(
        f"## {name}\n```python\n{(get_innate_skills_dir() / name).read_text()}```" for name in EXEMPLARS
    )
    return (
        f"{RULES}\n# The `innate` API\n{innate.__doc__}\n\n# The arm\n{arm_reference()}\n\n"
        f"# The head\n{head_reference()}\n\n# The base\n{base_reference()}\n\n"
        f"# The cameras and the model\n{vision_reference()}\n\n# Example skills\n{exemplars}"
    )


def arm_reference() -> str:
    """Manipulation's skill-facing surface, read off the class so the prompt cannot drift from it."""
    constants = "\n".join(f"Manipulation.{name} = {getattr(Manipulation, name)!r}" for name in ARM_CONSTANTS)
    return f"{ARM}```python\n{constants}\n\n{_method_stubs(Manipulation, ARM_METHODS)}\n```"


def head_reference() -> str:
    return f"{HEAD}```python\n{_attributes(HeadState, HEAD_STATE)}\n\n{_method_stubs(Head, HEAD_METHODS)}\n```"


def base_reference() -> str:
    return f"{BASE}```python\n{_attributes(Odometry, ODOMETRY)}\n\n{_method_stubs(Mobility, BASE_METHODS)}\n```"


def vision_reference() -> str:
    return f"{VISION}```python\n{_attributes(Llm, LLM_STATE)}\n\n{_method_stubs(Llm, LLM_METHODS)}\n```"


def _attributes(cls: type, names: tuple[str, ...]) -> str:
    return "\n".join(f"{cls.__name__}.{name}: {inspect.formatannotation(_hint(cls, name))}" for name in names)


def _hint(cls: type, name: str) -> object:
    member = vars(cls).get(name)
    if isinstance(member, property):
        return get_type_hints(member.fget)["return"]
    return get_type_hints(cls)[name]


def _method_stubs(cls: type, names: tuple[str, ...]) -> str:
    return "\n\n".join(_method_stub(getattr(cls, name)) for name in names)


def _method_stub(method: Callable[..., object]) -> str:
    signature = _MODULE_PREFIX.sub("", str(inspect.signature(method, eval_str=True)))
    doc = (inspect.getdoc(method) or "").replace("\n", "\n    ")
    return f'def {method.__name__}{signature}:\n    """{doc}"""'


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
        """Stream the reply text, with an empty string every HEARTBEAT_S while the model is silent
        so the caller can answer a Stop; the exchange stays in the conversation for the next round."""
        self._messages.append({"role": "user", "content": prompt})
        reply: list[str] = []
        for delta in self._stream():
            reply.append(delta)
            yield delta
        self._messages.append({"role": "assistant", "content": "".join(reply)})

    def _stream(self) -> Iterator[str]:
        # The socket read blocks for as long as the model reasons before its first token, so a
        # thread pumps it: the consumer keeps waking, and an abandoned stream drains over there.
        deltas: queue.Queue[str | BaseException | None] = queue.Queue()
        body = self._coder.request(self._messages)
        threading.Thread(target=self._pump, args=(body, deltas), name="forge-pump", daemon=True).start()
        while True:
            try:
                item = deltas.get(timeout=HEARTBEAT_S)
            except queue.Empty:
                yield ""
                continue
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def _pump(self, body: dict[str, object], deltas: queue.Queue[str | BaseException | None]) -> None:
        try:
            with self._client.request_stream(self._coder.service, ENDPOINT, json=body, timeout=180.0) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    choices = json.loads(line[len("data: ") :]).get("choices") or []
                    delta = choices[0].get("delta", {}).get("content") if choices else None
                    if delta:
                        deltas.put(delta)
        except (HTTPError, OSError) as error:
            deltas.put(ForgeUnreachable(f"the coding model was unreachable ({error})"))
        except Exception as error:  # noqa: BLE001 — the consumer raises it; swallowed here, it would read as an empty reply
            deltas.put(error)
        finally:
            deltas.put(None)


def extract_code(reply: str) -> str:
    """The fenced python block of a reply, or the whole reply when there is no fence."""
    match = _FENCE.search(reply)
    return match.group(1) if match else reply


def refusal(reply: str) -> str | None:
    """The coder's reason when it declares the request impossible instead of sending a file."""
    match = None if _FENCE.search(reply) else _REFUSAL.search(reply)
    return match.group(1) if match else None
