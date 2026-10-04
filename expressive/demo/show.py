"""A scripted show: MARS speaks each beat's line (macOS ``say``) and performs its emote, all through ONE
offline ``Animator`` ticked at the video's frame rate: speech sway during lines, crossfades into
gestures, idle breathing in between, played physically in the sim and filmed with subtitles.

show.yaml::

    title: ...            # top-right of every frame
    voice: Samantha       # `say -v '?'`
    rate: 175             # words per minute
    intro: 1.0            # seconds of idle before the first beat
    outro: 1.5
    duration: null        # pad or cut the show to this many seconds
    beats:
      - say: "Hi! I'm MARS."
        emote: "a cheerful hello"     # planned by the LLM (cached), else the closest preset
        recipe: "go .5 ..."           # or an explicit recipe instead of a planned one
        note: "approach"              # top-left label while the gesture plays (default: the emote)
        pause: 0.5                    # seconds after the line and the gesture
        sync: release                 # the gesture's energy peak lands on the line's last syllable
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import _core  # noqa: F401
import numpy as np
import yaml
from numpy.typing import NDArray

from brain_client.expressive import planner, presets
from brain_client.expressive.animator import Animator
from brain_client.expressive.channels import FPS as PLAN_FPS
from brain_client.expressive.channels import Ch
from brain_client.expressive.dsl import expand
from brain_client.expressive.motion import Clip
from demo.film import Encoder, Player, caption

logger = logging.getLogger("mars-express.show")
FPS = 30
SIZE = (1280, 720)
SAMPLE_RATE = 24000
GESTURE_TAIL_S = 0.6  # the next beat may start this long before a gesture finishes (the crossfade covers it)
Pcm = NDArray[np.int16]


@dataclass(frozen=True)
class Beat:
    say: str = ""
    emote: str = ""
    recipe: str = ""
    note: str = ""
    pause: float = 0.6
    sync: Literal["start", "release"] = "start"


@dataclass(frozen=True)
class Show:
    beats: list[Beat]
    title: str = ""
    voice: str = "Samantha"
    rate: int = 175
    intro: float = 1.0
    outro: float = 1.5
    duration: float | None = None

    @classmethod
    def load(cls, path: Path) -> Show:
        data = yaml.safe_load(path.read_text())
        beats = [Beat(**beat) for beat in data.pop("beats")]
        return cls(beats=beats, **data)


@dataclass(frozen=True)
class Cue:
    beat: Beat
    line_at: float
    pcm: Pcm
    clip: Clip | None
    clip_at: float

    @property
    def line_end(self) -> float:
        return self.line_at + len(self.pcm) / SAMPLE_RATE

    @property
    def clip_end(self) -> float:
        return self.clip_at + self.clip.duration if self.clip is not None else self.clip_at


@dataclass
class Studio:
    """Caches TTS and planned recipes under ``cache`` so a re-render is deterministic and offline."""

    cache: Path
    planner_model: str | None = "gpt-6-astra"
    _chat: planner.Chat | None = field(default=None, init=False)

    def speak(self, text: str, voice: str, rate: int) -> Pcm:
        if not text:
            return np.zeros(0, dtype=np.int16)
        key = hashlib.sha1(f"{voice}|{rate}|{text}".encode()).hexdigest()[:12]
        wav = self.cache / "tts" / f"{key}.wav"
        if not wav.exists():
            wav.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["say", "-v", voice, "-r", str(rate), "-o", str(wav), f"--data-format=LEI16@{SAMPLE_RATE}", text],
                check=True,
            )
        with wave.open(str(wav)) as f:
            return np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(np.int16)

    def gesture(self, beat: Beat) -> tuple[str, str]:
        """``(recipe, source)`` of a beat: its scripted recipe, else its emote planned, else none."""
        if beat.recipe:
            return beat.recipe, "script"
        if beat.emote:
            return self.recipe(beat.emote)
        return "", ""

    def recipe(self, prompt: str) -> tuple[str, str]:
        """``(recipe, source)``: the planner's, cached per prompt and model, else the closest preset's."""
        key = hashlib.sha1(f"{self.planner_model}|{prompt}".encode()).hexdigest()[:12]
        path = self.cache / "plans" / f"{key}.json"
        if path.exists():
            cached = json.loads(path.read_text())
            return cached["recipe"], cached["source"]
        recipe, source = presets.PRESETS[presets.match(prompt)].recipe, "preset"
        if self.planner_model:
            try:
                written = planner.write(prompt, self._planner_chat())
                recipe, source = written.recipe, f"planner {self.planner_model}"
            except Exception as e:  # noqa: BLE001 — a dead planner falls back to a preset, like the robot
                logger.warning("planner failed for %r, using a preset: %s", prompt, e)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"prompt": prompt, "recipe": recipe, "source": source}))
        return recipe, source

    def _planner_chat(self) -> planner.Chat:
        if self._chat is None:
            from eval.llm import load_env, openai_chat

            load_env()
            self._chat = openai_chat(self.planner_model or "")
        return self._chat


def last_onset(pcm: Pcm, gap_s: float = 0.12) -> float:
    """Start (s) of the last burst of sound after a silence of at least ``gap_s``: the final syllable."""
    hop = SAMPLE_RATE // 100
    count = len(pcm) // hop
    if count == 0:
        return 0.0
    level = np.sqrt(np.mean(pcm[: count * hop].astype(np.float64).reshape(count, hop) ** 2, axis=1))
    loud = level > 0.12 * level.max()
    start, quiet = 0, 0
    for i, on in enumerate(loud):
        if not on:
            quiet += 1
            continue
        if quiet * hop / SAMPLE_RATE >= gap_s or i == 0:
            start = i
        quiet = 0
    return start * hop / SAMPLE_RATE


def peak_time(recipe: str) -> float:
    return float(np.argmax(expand(recipe)[:, Ch.ENERGY])) / PLAN_FPS


def cue_sheet(show: Show, studio: Studio) -> list[Cue]:
    cues: list[Cue] = []
    t = show.intro
    for k, beat in enumerate(show.beats):
        pcm = studio.speak(beat.say, show.voice, show.rate)
        recipe, source = studio.gesture(beat)
        clip = Clip.from_recipe(recipe, name=beat.note or beat.emote, seed=k, prompt=beat.emote) if recipe else None
        line_at = clip_at = t
        if clip is not None and beat.sync == "release":
            lead = last_onset(pcm) - peak_time(recipe)
            line_at, clip_at = (t, t + lead) if lead >= 0 else (t - lead, t)
        cue = Cue(beat, line_at, pcm, clip, clip_at)
        cues.append(cue)
        logger.info("%5.1f s  %-48s %s %s", t, beat.say[:48], source, recipe[:70])
        t = max(cue.line_end, cue.clip_end - GESTURE_TAIL_S) + beat.pause
    return cues


def soundtrack(cues: list[Cue], seconds: float, wav: Path) -> Path:
    track = np.zeros(int(seconds * SAMPLE_RATE) + 1, dtype=np.int32)
    for cue in cues:
        start = round(cue.line_at * SAMPLE_RATE)
        end = min(len(track), start + len(cue.pcm))
        track[start:end] += cue.pcm[: end - start]
    wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(np.clip(track, -32768, 32767).astype("<i2").tobytes())
    return wav


def _subtitle(cues: list[Cue], t: float) -> str:
    """The line being spoken, held until the next line starts (at most 1.5 s past its end)."""
    shown = ""
    for cue in cues:
        if cue.beat.say and cue.line_at <= t < cue.line_end + 1.5:
            shown = cue.beat.say
    return shown


def _note(cues: list[Cue], t: float) -> str:
    playing = [c for c in cues if c.clip is not None and c.clip_at <= t < c.clip_end]
    if not playing:
        return "idle: breathing" if not any(c.line_at <= t < c.line_end for c in cues) else "speech sway"
    beat = playing[-1].beat
    return f"emote: {beat.note or beat.emote}"


def render_show(show: Show, out: Path, studio: Studio) -> Path:
    cues = cue_sheet(show, studio)
    end = max((max(c.line_end, c.clip_end) for c in cues), default=0.0) + show.outro
    seconds = show.duration or end
    audio = soundtrack(cues, seconds, studio.cache / f"{out.stem}.wav")
    clock = [0.0]
    animator = Animator(fps=FPS, clock=lambda: clock[0])
    events = sorted(
        [(c.line_at, 0, c) for c in cues if len(c.pcm)] + [(c.clip_at, 1, c) for c in cues if c.clip is not None],
        key=lambda e: (e[0], e[1]),
    )
    player = Player(SIZE)
    pose = animator.tick(0.0)
    player.settle(pose.vector)
    frames = round(seconds * FPS)
    logger.info("rendering %s: %.1f s, %d frames", out.name, seconds, frames)
    with Encoder(out, SIZE, FPS, audio) as encoder:
        for k in range(frames):
            t = k / FPS
            while events and events[0][0] <= t:
                at, kind, cue = events.pop(0)
                clock[0] = at
                if kind == 0:
                    animator.feed_speech(cue.pcm, SAMPLE_RATE)
                elif cue.clip is not None:
                    animator.play(cue.clip)
            if k:
                pose = animator.tick(t)
                player.step(pose.vector, 1.0 / FPS)
            encoder.write(caption(player.shot(), _subtitle(cues, t), _note(cues, t), show.title))
            if k % (10 * FPS) == 0:
                logger.info("  %5.1f / %.1f s", t, seconds)
    return out
