"""Model backends for BrainAgent. One class per way of getting a decision.

The point of the split is the benchmark's second requirement: pointing the
suite at a different agent architecture should be choosing a backend, not
editing the harness. Everything here implements one method --
decide(observation, action_menu) -> {"action": ..., "args": {...}} -- and
declares whether it wants a camera frame.

THE BLIND BACKEND IS NOT A PLACEHOLDER. CodexBlindBackend runs text-only.
The CLI takes no inline images, so CodexBackend writes the frame to disk and
names the path instead; the blind control is the subclass that does not. That makes it a control worth reporting in its
own right: any challenge a blind agent passes is a challenge with a shortcut in
it, and finding those is the point of the validity gate. Read its scores as
"what is reachable from the brief alone", not as a vision result.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "args"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["turn", "forward", "pick", "place", "say", "answer", "look", "finish"],
        },
        # Closed-schema APIs reject a free-form object, and the arg shape
        # differs per action. A JSON STRING sidesteps both without inventing a
        # union type the model then has to satisfy.
        "args": {"type": "string", "description": 'JSON object, e.g. {"degrees": 45} or {}'},
    },
}

SYSTEM = """You are a small mobile robot about 25 cm tall in a room, doing what
a person asks. You act one step at a time and see the result of each step
before choosing the next.

Be efficient: every action costs a turn, and you have few. Drive in useful
distances rather than 5 cm nudges. If you are asked for something you cannot
do -- anything above about 30 cm is out of your arm's reach, and you cannot
climb -- say so plainly instead of trying.

Not everything you hear is addressed to you. People talk to each other in the
same room. Act on what you were asked to do."""


def _last_json_object(text: str) -> dict | None:
    """The last complete JSON object carrying an "action" key, or None.

    A regex cannot do this: `args` is an escaped JSON string, so the payload
    contains braces inside string literals and any brace-matching pattern
    truncates it. raw_decode is the only thing here that understands quoting.
    """
    decoder = json.JSONDecoder()
    best = None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            best = obj  # last wins: the CLI prints its preamble first
    return best


def _coerce(raw: dict) -> dict:
    """Normalise a model reply into {"action", "args"} with args a dict."""
    action = str(raw.get("action", "")).strip().lower()
    args = raw.get("args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            pass
    if not isinstance(args, dict):
        # A model that replied `90` or `\"90\"` or `left 90` instead of a JSON
        # object has still said something usable. Throwing the turn away would
        # score the parser rather than the agent, and the action name already
        # says which key the number belongs to -- so offer it as both and let
        # the action pick. json.loads(\"90\") returns an int, not a dict, which
        # is why this check cannot live in the except branch above.
        m = re.search(r"-?\d+(\.\d+)?", str(args))
        args = {"degrees": float(m.group()), "metres": float(m.group())} if m else {}
    return {"action": action, "args": args}


class EchoBackend:
    """A fixed script. The test double for the harness itself -- it makes every
    part of BrainAgent runnable without a network or an API key, which is what
    keeps a broken agent loop from being discovered only during a paid sweep."""

    wants_image = False

    # A short patrol, so the registry can build one with no arguments. Every
    # other backend is constructed by registry.resolve(name)(), and requiring a script
    # made `main.py --agents brain:echo` -- the offline smoke path, whose whole
    # purpose is exercising the agent loop without spending money -- crash on
    # startup. Enough steps to move the robot and produce turns.
    DEFAULT_SCRIPT = [
        {"action": "look", "args": "{}"},
        {"action": "forward", "args": '{"metres": 0.9}'},
        {"action": "look", "args": "{}"},
        {"action": "turn", "args": '{"degrees": 25}'},
        {"action": "finish", "args": "{}"},
    ]

    def __init__(self, script: list[dict] | None = None):
        self.script = list(script if script is not None else self.DEFAULT_SCRIPT)
        self.i = 0

    def decide(self, obs, menu):  # noqa: ARG002
        if self.i >= len(self.script):
            return {"action": "finish", "args": {}}
        step = self.script[self.i]
        self.i += 1
        return _coerce(step)


class CodexBackend:
    """The Codex CLI as the decision-maker. Sees, by being handed a file path.

    --ignore-user-config matters: with the user's config loaded a call took
    29.8 s, and without it 4.7 s. At one call per turn and forty turns per
    episode that is the difference between a sweep and an afternoon.
    """

    # The CLI takes no inline images, but it CAN open files -- so the frame is
    # written to disk and the prompt names the path. Verified on a real robot
    # frame before being relied on: at 640x480 it read back three cups, their
    # colours, the teapot and the menu board, all correct.
    wants_image = True

    def __init__(self, model: str = "gpt-5.6-luna", timeout_s: float = 180.0):
        self.model, self.timeout_s = model, timeout_s
        if shutil.which("codex") is None:
            raise RuntimeError("codex CLI not on PATH")

    def decide(self, obs, menu) -> dict:
        eye = ""
        if obs.image_path:
            eye = (
                f"\nYour camera's current view is the image file {obs.image_path}.\n"
                "Open and look at it before deciding. It is what you can see right now.\n"
            )
        prompt = f"{SYSTEM}\n{eye}\n{obs.as_text()}\n\n{menu}\n"
        with tempfile.TemporaryDirectory() as tmp:
            schema = os.path.join(tmp, "schema.json")
            with open(schema, "w", encoding="utf-8") as fh:
                json.dump(SCHEMA, fh)
            proc = subprocess.run(
                [
                    "codex",
                    "exec",
                    "-m",
                    self.model,
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "-s",
                    "read-only",
                    "--output-schema",
                    schema,
                    "-",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                # cwd matters: the CLI resolves relative paths against it, and
                # a sandboxed read-only run in a deep tree is slower to start.
                cwd="/",
            )
        out = proc.stdout.strip()
        if not out:
            raise RuntimeError(f"codex produced nothing (rc={proc.returncode}): {proc.stderr[-300:]}")
        data = _last_json_object(out)
        if data is None:
            raise RuntimeError(f"no action in codex output: {out[-300:]}")
        return _coerce(data)


class CodexBlindBackend(CodexBackend):
    """CodexBackend with the camera taken away. THE CONTROL, not a fallback.

    When CodexBackend gained sight the blind baseline vanished with it, because
    the same class was upgraded. That baseline is what proves a perception
    challenge needs perception: any challenge a blind agent passes has a
    shortcut in it, and finding those is the whole job of the validity gate.

    Run it alongside the seeing one. The GAP between the two is the perception
    result; either number on its own is not.
    """

    wants_image = False


class GeminiBackend:
    """The multimodal path: the camera frame goes to the model.

    This is the backend the reported vision numbers should come from, and it
    needs GEMINI_API_KEY. Without one it refuses at construction rather than
    silently running blind, because a blind run that is labelled as a vision
    run is exactly the kind of unreproducible number this suite exists to
    avoid publishing.
    """

    wants_image = True

    def __init__(self, model: str = "gemini-3.1-pro", timeout_s: float = 90.0):
        self.key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not self.key:
            raise RuntimeError("GEMINI_API_KEY is not set; refusing to run a vision agent blind")
        self.model, self.timeout_s = model, timeout_s
        self.base = os.environ.get("GEMINI_BASE_URL", "").strip() or "https://generativelanguage.googleapis.com"

    def decide(self, obs, menu) -> dict:
        import urllib.request

        parts = [{"text": f"{obs.as_text()}\n\n{menu}"}]
        if obs.image_path:
            blob = base64.b64encode(Path(obs.image_path).read_bytes()).decode()
            parts.insert(0, {"inline_data": {"mime_type": "image/jpeg", "data": blob}})
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"},
        }
        req = urllib.request.Request(
            f"{self.base}/v1beta/models/{self.model}:generateContent?key={self.key}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _coerce(json.loads(text))
