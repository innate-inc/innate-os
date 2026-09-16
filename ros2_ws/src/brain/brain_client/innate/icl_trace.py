# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What a demonstration-conditioned run tells the In Context Learning page.

Every event mirrors something the run already writes under its
``.imitation_runs/<run>/`` directory, so the live page and the run directory
cannot disagree. No ROS here: the skill owns the publisher and passes a
callable, the same shape as ``skills/overlay.py``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol


def jsonable(value: Any) -> Any:
    """Floats rounded to 3 dp, numpy scalars unboxed, tuples listed."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    return round(value, 3) if isinstance(value, float) else value


ICL_TRACE_TOPIC = "/brain/icl_trace"

Publish = Callable[[str], None]


class _Logger(Protocol):
    def warning(self, msg: str) -> None: ...


class IclTrace:
    """One run's live channel. Every event repeats the run header — model,
    demonstration, phase map — so a page opened mid-run rebuilds from whichever
    message reaches it first. Best effort: a publish failure is logged, never
    raised into the run."""

    def __init__(self, skill: str, run: str, publish: Publish, logger: _Logger) -> None:
        self._skill = skill
        self._run = run
        self._publish = publish
        self._logger = logger
        self._header: dict[str, Any] = {}

    def begin(self, model: str, demonstration: str, episode_frames: int, **fields: Any) -> None:
        self._header = {
            "model": model,
            "demonstration": demonstration,
            "episode_frames": episode_frames,
            "phases": [],
            **fields,
        }
        self._emit("run", state="start")

    def tool(self, name: str, arguments: Any, latency_s: float) -> None:
        """One model tool call as it returns, so the page shows inspect_demo
        and record_phases while the turn is still deciding."""
        self._emit("tool", tool=name, arguments=arguments, latency_s=latency_s)

    def phases(self, phases: list[dict[str, Any]]) -> None:
        self._header["phases"] = phases
        self._emit("phases")

    def step(
        self,
        step: int,
        decision: dict[str, Any],
        observation: dict[str, Any],
        images: dict[str, str],
        latency_s: float,
        phase: int,
        batch: dict[str, Any] | None = None,
    ) -> None:
        """A validated decision with the frames the model actually saw, emitted
        before the arm acts on it — the page shows the reasoning during the move."""
        self._emit(
            "step",
            step=step,
            decision=decision,
            observation=observation,
            images=images,
            latency_s=latency_s,
            phase=phase,
            batch=batch,
        )

    def execution(self, step: int, execution: dict[str, Any]) -> None:
        """What the arm actually did, once the move lands."""
        self._emit("execution", step=step, execution=execution)

    def note(self, text: str) -> None:
        """A turn that produced no decision — a discarded action, a recovery."""
        self._emit("note", text=text)

    def end(self, ok: bool, cancelled: bool, message: str) -> None:
        self._emit("run", state="end", ok=ok, cancelled=cancelled, message=message)

    def _emit(self, event: str, **fields: Any) -> None:
        payload = {"skill": self._skill, "run": self._run, "ev": event, "t": time.time(), **self._header, **fields}
        try:
            self._publish(json.dumps(jsonable(payload), allow_nan=False))
        except Exception as e:  # noqa: BLE001 — a debug channel must never become the run's failure
            self._logger.warning(f"[{self._skill}] icl trace '{event}' dropped: {e}")


def run_trace(skill, run_id: str) -> IclTrace:
    """This run's live mirror, published from the skill's own node. Silent when
    the skill runs without one, so a test needs no publisher."""
    from std_msgs.msg import String

    publisher = None if skill.node is None else skill.node.create_publisher(String, ICL_TRACE_TOPIC, 10)

    def publish(payload: str) -> None:
        if publisher is not None:
            publisher.publish(String(data=payload))

    return IclTrace(skill.name, run_id, publish, skill.logger)
