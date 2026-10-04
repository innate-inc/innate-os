"""Blind judges. A judge sees the motion (a key-frame strip, or the video itself for Gemini) and is never
told the prompt: it spreads probability over the studio's fixed labels, names the motion in its own
words, and rates how alive and how readable it is. A pairwise judge picks the more alive of two motions
(the runner asks in both orders); a text grader, which does see the prompt, scores each blind description
against it."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from eval.llm import Json, Proxy, openai_json
from eval.prompts import LABELS

JudgeKind = Literal["openai", "gemini"]
VIDEO_FPS = 5  # Gemini samples video at 1 fps unless told otherwise; a 0.15 s snap needs more

CONTEXT = (
    "This shows one short motion by a small non-humanoid robot: a dark boxy base on wheels, one yellow "
    "arm ending in a black claw gripper (its only limb), and a flat dark camera head on top that can only "
    "tilt up or down. A fixed camera films it from in front of it and off to one side, about 1.5 m away; "
    "the robot may turn on the spot or roll a little forward or back."
)
MEDIA = {
    "openai": "The image is a strip of 8 key frames in time order, left to right then top row to bottom row; "
    "each is labelled with its number and time.",
    "gemini": "The video is the whole motion in real time.",
}
BODY_ONLY = (
    "Judge ONLY the body language over time: the arm's posture and how it moves, the gripper opening and "
    "closing, the head tilting, the base turning or rolling, and the timing (snaps, pauses, trembling). "
)
WITH_SOUND = (
    "The video has sound: any short sound the robot makes is part of what it expresses. Judge the body "
    "language over time (the arm's posture and how it moves, the gripper opening and closing, the head "
    "tilting, the base turning or rolling, the timing) together with that sound. "
)
READING_TASK = (
    "Do not assume any particular feeling was intended.\n"
    "- cues: 2-4 short physical observations that drive your reading.\n"
    "- description: the single best short description (2-8 words) of what the robot is feeling or doing, "
    "as a person watching would put it.\n"
    f"- probabilities: the probability that a typical person would describe the robot with each label "
    f"({', '.join(LABELS)}); they must sum to 1.\n"
    "- alive: 1-5. 1 = mechanical, a machine moving between poses; 3 = some life; 5 = moves like a living "
    "creature (anticipation, follow-through, small involuntary movement, natural timing).\n"
    "- readable: 1-5. 1 = means nothing in particular; 3 = a vague mood; 5 = one specific feeling or "
    "action that anyone would name."
)
PAIR_TASK = (
    "You will see two different motions of the same robot, ONE then TWO. Which one moves more like a living "
    "creature rather than a machine: organic timing, anticipation, follow-through, small involuntary "
    "movement? Judge only how alive the movement is, not what it means. Give one sentence of reasoning, "
    "the winner, and whether the difference is clear or slight."
)
GRADE_TASK = (
    "A robot performed a motion for a prompt. A judge who never saw the prompt described the motion. Score "
    "how well the description matches the prompt: 2 = the same feeling or action (synonyms count; a "
    "multi-part story counts if the main part is named); 1 = related (same mood family, or one part of it); "
    "0 = a different reading. One sentence of reasoning first."
)

READING_SCHEMA: Json = {
    "type": "object",
    "properties": {
        "cues": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
        "probabilities": {
            "type": "object",
            "properties": {label: {"type": "number"} for label in LABELS},
            "required": list(LABELS),
            "additionalProperties": False,
        },
        "alive": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "readable": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
    },
    "required": ["cues", "description", "probabilities", "alive", "readable"],
    "additionalProperties": False,
}
PAIR_SCHEMA: Json = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "winner": {"type": "string", "enum": ["ONE", "TWO"]},
        "margin": {"type": "string", "enum": ["clear", "slight"]},
    },
    "required": ["reason", "winner", "margin"],
    "additionalProperties": False,
}
GRADE_SCHEMA: Json = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "score": {"type": "integer", "enum": [0, 1, 2]}},
    "required": ["reason", "score"],
    "additionalProperties": False,
}


class Reading(TypedDict):
    cues: list[str]
    description: str
    probabilities: dict[str, float]
    alive: int
    readable: int


class Verdict(TypedDict):
    winner: str  # the arm name
    margin: str
    reason: str
    shown_first: str


@dataclass(frozen=True)
class Media:
    """What one judge call gets to see of a clip."""

    strip: Path
    video: Path


def normalize(raw: object) -> dict[str, float]:
    """Coerce a judge's probabilities onto the labels: junk and negatives become 0, then sum to 1 (uniform
    when nothing is left), exactly as judge.js does."""
    values = {label: 0.0 for label in LABELS}
    if isinstance(raw, dict):
        for label in LABELS:
            value = raw.get(label)
            if isinstance(value, int | float) and value > 0:
                values[label] = float(value)
    total = sum(values.values())
    return {label: (v / total if total > 0 else 1.0 / len(LABELS)) for label, v in values.items()}


class Judge:
    def __init__(
        self, kind: JudgeKind, model: str | None = None, grader_model: str = "gpt-5.5", audio: bool = False
    ) -> None:
        if audio and kind != "gemini":
            raise ValueError("only the gemini judge hears the video's sound")
        self.audio = audio
        self.kind = kind
        self.model = model or {"openai": "gpt-5.5", "gemini": "gemini-3.1-pro-preview"}[kind]
        self.grader_model = grader_model
        self._proxy = Proxy() if kind == "gemini" else None

    def _ask(self, parts: list[tuple[str, Path | str]], schema: Json, temperature: float) -> Json:
        """``parts`` are ("text", str) / ("strip", png) / ("video", mp4) in order."""
        if self._proxy is not None:
            native: list[Json] = []
            for kind, value in parts:
                if kind == "text":
                    native.append({"text": str(value)})
                elif kind == "video":
                    native.append(
                        {
                            "inlineData": {"mimeType": "video/mp4", "data": _b64(Path(value))},
                            "videoMetadata": {"fps": VIDEO_FPS},
                        }
                    )
                else:
                    native.append({"inlineData": {"mimeType": "image/png", "data": _b64(Path(value))}})
            return self._proxy.gemini_json(self.model, native, schema, temperature)
        content: list[Json] = []
        for kind, value in parts:
            if kind == "text":
                content.append({"type": "text", "text": str(value)})
            else:
                url = f"data:image/png;base64,{_b64(Path(value))}"
                content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})
        return openai_json(self.model, content, schema)

    def _show(self, media: Media) -> tuple[str, Path]:
        return ("video", media.video) if self.kind == "gemini" else ("strip", media.strip)

    def read(self, media: Media) -> Reading:
        raw = self._ask(
            [
                ("text", f"{CONTEXT}\n{MEDIA[self.kind]}\n\n{WITH_SOUND if self.audio else BODY_ONLY}{READING_TASK}"),
                self._show(media),
            ],
            READING_SCHEMA,
            0.7,
        )
        return {
            "cues": [str(c) for c in raw.get("cues", [])][:6],
            "description": str(raw.get("description", "")),
            "probabilities": normalize(raw.get("probabilities")),
            "alive": int(raw.get("alive", 0)),
            "readable": int(raw.get("readable", 0)),
        }

    def pair(self, first: tuple[str, Media], second: tuple[str, Media]) -> Verdict:
        """Which of two arms' motions looks more alive, shown in exactly this order."""
        shown = (first, second)
        raw = self._ask(
            [
                ("text", f"{CONTEXT}\n{MEDIA[self.kind]}\n\n{PAIR_TASK}"),
                ("text", "Motion ONE:"),
                self._show(shown[0][1]),
                ("text", "Motion TWO:"),
                self._show(shown[1][1]),
            ],
            PAIR_SCHEMA,
            0.3,
        )
        winner = shown[0][0] if raw.get("winner") == "ONE" else shown[1][0]
        margin = "clear" if raw.get("margin") == "clear" else "slight"
        return {"winner": winner, "margin": margin, "reason": str(raw.get("reason", "")), "shown_first": shown[0][0]}

    def grade(self, prompt: str, description: str) -> int:
        """0/1/2: does a blind description match the prompt (text only, the grader sees both)."""
        text = f"{GRADE_TASK}\n\nPrompt: {prompt}\nDescription: {description}"
        raw = openai_json(self.grader_model, [{"type": "text", "text": text}], GRADE_SCHEMA)
        return int(raw.get("score", 0))


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()
