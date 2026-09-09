# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What a running skill draws over the robot's cameras: a drawing vocabulary
(brackets, boxes, points, lines) plus a HUD of stages and a readout, published
as JSON events on /brain/skill_overlay for the webapp's targeting overlay.
Nothing here knows what a pick is — the skill says what to draw where.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol

OVERLAY_TOPIC = "/brain/skill_overlay"
# A moving marker is coalesced to 10 Hz (the webapp glides its points between
# updates; more only floods rosbridge); an unchanged one is repeated once a
# second so a page opened mid-run still picks it up.
MARK_MIN_INTERVAL_S = 0.1
MARK_REFRESH_S = 1.0

View = Literal["main", "arm"]
Px = Sequence[float]
Corners = Sequence[float]
Publish = Callable[[str], None]


class _Logger(Protocol):
    def warning(self, msg: str) -> None: ...


# The run every overlay event belongs to, process-wide like the run cancel
# latch: a sub-skill's overlay draws into its root's run, and the UI drops
# stragglers from a run that has already ended. Nested code skills re-enter
# the server's run body, so only the outermost entry owns the run.
_run_id = ""
_run_drew = False


def enter_run() -> bool:
    """Server hook around a code skill's execute(): opens a run when none is
    open and returns whether this caller owns it (and so must end it)."""
    global _run_id, _run_drew
    if _run_id:
        return False
    _run_id = uuid.uuid4().hex[:8]
    _run_drew = False
    return True


def end_run() -> None:
    global _run_id, _run_drew
    _run_id = ""
    _run_drew = False


def _jsonable(value: Any) -> Any:
    """Floats rounded to 3 dp, numpy scalars (bool_ included) unboxed, tuples listed."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    return round(value, 3) if isinstance(value, float) else value


class Overlay:
    """One skill's drawing channel. Markers are keyed by id: sending an id
    again moves it, ``clear`` removes it. ``begin`` is optional — the first
    event opens the run on the UI — and the server closes the run with the
    skill's result, so a skill never calls ``end``. Every event repeats the
    run's header (prompt, stages, frame, current stage), so a page that opens
    mid-run, or missed the start while the publisher was still matching,
    rebuilds the HUD from whichever message reaches it first. Best effort: a
    publish failure is logged, never raised into the run."""

    def __init__(self, skill: str, publish: Publish, logger: _Logger) -> None:
        self._skill = skill
        self._publish = publish
        self._logger = logger
        self._header: dict[str, Any] = {}
        self._last_mark: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_readout: tuple[str, bool, float | None] | None = None

    def begin(self, prompt: str = "", stages: Sequence[str] = (), frame: tuple[int, int] | None = None) -> None:
        """Declare the run up front: what it is after, its stage ladder, and
        the image size its pixels are in (the head camera's when omitted)."""
        self._header = {"prompt": prompt, "stages": list(stages), "frame": frame, "stage": None}
        self._emit("run", state="start")

    def stage(self, name: str) -> None:
        self._header["stage"] = name
        self._emit("stage", name=name)

    def readout(self, text: str, *, busy: bool = False, progress: float | None = None) -> None:
        """The HUD's one live line; ``busy`` sweeps the picture (a frame is out
        to a model), ``progress`` in 0..1 fills the bar beside it."""
        if (text, busy, progress) == self._last_readout:
            return
        self._last_readout = (text, busy, progress)
        self._emit("readout", text=text, busy=busy, progress=progress)

    def bracket(self, id: str, corners: Corners, *, label: str = "", view: View = "main", locked: bool = False) -> None:
        """Corner brackets on (x0, y0, x1, y1): the "seen HERE" shape."""
        self._mark(id, "bracket", view, label, locked, corners=corners)

    def box(
        self,
        id: str,
        corners: Corners,
        *,
        inner: float | None = None,
        label: str = "",
        view: View = "main",
        locked: bool = False,
    ) -> None:
        """A goal rectangle; ``inner`` draws the accept deadband as that fraction of it."""
        self._mark(id, "box", view, label, locked, corners=corners, inner=inner)

    def point(self, id: str, px: Px, *, label: str = "", view: View = "main", locked: bool = False) -> None:
        self._mark(id, "point", view, label, locked, px=px)

    def reticle(self, id: str, px: Px, *, label: str = "", view: View = "main") -> None:
        self._mark(id, "reticle", view, label, False, px=px)

    def vector(self, id: str, a: Px, b: Px, *, view: View = "main") -> None:
        """A dashed steering line from a to b."""
        self._mark(id, "vector", view, "", False, a=a, b=b)

    def line(self, id: str, a: Px, b: Px, *, view: View = "main") -> None:
        self._mark(id, "line", view, "", False, a=a, b=b)

    def clear(self, *ids: str, view: View | None = None) -> None:
        """Remove the named markers, every marker on ``view``, or everything."""
        if ids and not any(id in self._last_mark for id in ids):
            return
        for id in ids:
            self._last_mark.pop(id, None)
        if not ids:
            self._last_mark = {k: v for k, v in self._last_mark.items() if view is not None and v[1]["view"] != view}
        self._emit("clear", ids=list(ids), view=view)

    def end(self, ok: bool, cancelled: bool, text: str) -> None:
        """Close the run with its result. The server's call on the root skill,
        made after execute() returns; silent when nothing in the run drew."""
        if not _run_drew:
            return
        self._emit("run", state="end", ok=ok, cancelled=cancelled, text=text)
        self._header = {}
        self._last_mark.clear()
        self._last_readout = None

    def _mark(self, id: str, kind: str, view: View, label: str, locked: bool, **geometry: Any) -> None:
        fields = _jsonable({"kind": kind, "view": view, "label": label, "locked": locked, **geometry})
        now = time.monotonic()
        last = self._last_mark.get(id)
        if last is not None:
            age = now - last[0]
            if last[1] == fields and age < MARK_REFRESH_S:
                return
            if last[1]["locked"] == locked and age < MARK_MIN_INTERVAL_S:
                return
        self._last_mark[id] = (now, fields)
        self._emit("mark", id=id, **fields)

    def _emit(self, event: str, **fields: Any) -> None:
        global _run_drew
        payload = {"skill": self._skill, "run": _run_id, "ev": event, "t": time.time(), **self._header, **fields}
        try:
            self._publish(json.dumps(_jsonable(payload)))
        except Exception as e:  # noqa: BLE001 — a side channel must never become the run's failure
            self._logger.warning(f"[{self._skill}] overlay '{event}' dropped: {e}")
            return
        _run_drew = True
