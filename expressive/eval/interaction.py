"""Interaction-level judging: a recorded conversation with the robot (screen + its voice), watched by Gemini
with no transcript, rated reply by reply on whether the body's feeling fits the words."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from eval.llm import Json, Proxy

TASK = (
    "This is a screen recording of a person chatting with MARS, a small robot in a simulator: a dark wheeled "
    "base, one yellow arm ending in a black claw (its only limb), and a flat camera head that only tilts up "
    "or down. The robot's voice is in the audio; the chat panel on the right shows the messages. Watch the "
    "robot's body while it talks.\n"
    "For each robot reply you hear, give: the time it starts (s), a few words of what it says, the emotion "
    "the robot's body conveys during and right after it, whether the body fits what is being said (yes, "
    "partly or no) and one short reason. Then rate the whole interaction 1-5: expressive = how emotionally "
    "expressive the robot is while interacting (1 = a machine, 5 = clearly feels things), fit = how well "
    "its movements fit its words. List anything that looked wrong (mistimed, contradictory, glitchy, "
    "repetitive), each with its time."
)
SCHEMA: Json = {
    "type": "object",
    "properties": {
        "replies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "t": {"type": "number"},
                    "says": {"type": "string"},
                    "body_emotion": {"type": "string"},
                    "fits": {"type": "string", "enum": ["yes", "partly", "no"]},
                    "reason": {"type": "string"},
                },
                "required": ["t", "says", "body_emotion", "fits", "reason"],
            },
        },
        "expressive": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "fit": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "wrong": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["replies", "expressive", "fit", "wrong"],
}


def rate(video: Path, model: str = "gemini-3.1-pro-preview", proxy: Proxy | None = None) -> dict[str, Any]:
    """One independent viewing of ``video`` (mp4 with audio, under the proxy's request size limit)."""
    parts: list[Json] = [
        {"text": TASK},
        {"inlineData": {"mimeType": "video/mp4", "data": base64.b64encode(video.read_bytes()).decode()}},
    ]
    return (proxy or Proxy()).gemini_json(model, parts, SCHEMA, 0.7, timeout_s=600.0)


def summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean ratings and the yes / partly / no counts of every reply judgment across viewings."""
    fits = [reply["fits"] for run in runs for reply in run["replies"]]
    return {
        "viewings": len(runs),
        "expressive": sum(run["expressive"] for run in runs) / len(runs),
        "fit": sum(run["fit"] for run in runs) / len(runs),
        "replies_heard": [len(run["replies"]) for run in runs],
        "fits": {answer: fits.count(answer) for answer in ("yes", "partly", "no")},
    }
