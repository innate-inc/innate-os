# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The conversation as the scribe reads it (RFC 6.3, docs/rfc/people-memory.md
in innate-jetson): one line, the tracks in view while it was said, the window
they buffer into — and, on the node's side, how a ``/brain/chat_in`` or
``/brain/chat_out`` payload becomes such a line and which track it is
attributed to. PURE module: no rclpy, no network; stamps are epoch seconds."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from brain_client.common.enums import StrEnum
from brain_client.people.types import IdentityState
from brain_client.transport.chat import Sender

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from brain_client.people.types import TrackState

Revoked = Callable[[str | None], bool]
"""Whether a person id has been forgotten; ``None`` (an untracked view) never is."""

WINDOW_IDLE_SEC = 8.0
WINDOW_MAX_MESSAGES = 12
TALKING_RANGE_M = 3.0
SPEAKING_HOLD_SEC = 3.0  # how long one chat_in message keeps a track marked as the speaker


class Speaker(StrEnum):
    """Who said a line. The node maps chat entries onto these: ``user`` from
    ``/brain/chat_in``, ``robot`` from the robot's own speech and skill output."""

    USER = "user"
    ROBOT = "robot"


@dataclass(frozen=True)
class TagView:
    """One track as it stood when a message arrived: what the scribe is allowed
    to attribute to, and the state the name rules test."""

    tag: str
    state: IdentityState = IdentityState.UNKNOWN
    person_id: str | None = None
    name: str | None = None
    enrolling: bool = False

    @property
    def nameable(self) -> bool:
        """Face-confirmed or currently enrolling — the only tracks a passive
        name may land on."""
        return self.enrolling or self.state in (IdentityState.KNOWN, IdentityState.FAMILIAR)


@dataclass(frozen=True)
class Utterance:
    id: str
    stamp: float
    speaker: Speaker
    text: str
    in_view: tuple[TagView, ...] = ()

    def view(self, tag: str) -> TagView | None:
        return next((seen for seen in self.in_view if seen.tag == tag), None)


@dataclass(frozen=True)
class Window:
    """One closed conversation window: the messages and who was in view for
    each of them."""

    messages: tuple[Utterance, ...]

    @property
    def opened(self) -> float:
        return self.messages[0].stamp if self.messages else 0.0

    @property
    def closed(self) -> float:
        return self.messages[-1].stamp if self.messages else 0.0

    def views(self) -> dict[str, TagView]:
        """The latest view of every tag seen anywhere in the window."""
        views: dict[str, TagView] = {}
        for message in self.messages:
            for seen in message.in_view:
                views[seen.tag] = seen
        return views

    def person_ids(self) -> list[str]:
        return list(dict.fromkeys(view.person_id for view in self.views().values() if view.person_id))


def line_unless_revoked(message: Utterance, revoked: Revoked) -> Utterance | None:
    """One transcript line, or None when anyone who was in view for it has been
    forgotten (RFC section 10). Their words are the whole line, so keeping it
    for whoever else stood there writes a forgotten person's sentence onto
    their neighbour."""
    return None if any(revoked(view.person_id) for view in message.in_view) else message


def without_revoked(window: Window, revoked: Revoked) -> Window:
    """The window as it stands after a forget: deletion has to reach the work
    already in flight, or it lands a moment later anyway."""
    lines = (line_unless_revoked(message, revoked) for message in window.messages)
    return Window(tuple(line for line in lines if line is not None))


class WindowBuffer:
    """Accumulates messages into a window; closes it 8 s after the last message
    or at 12 messages, whichever comes first."""

    def __init__(self, *, idle_sec: float = WINDOW_IDLE_SEC, max_messages: int = WINDOW_MAX_MESSAGES):
        self._idle_sec = idle_sec
        self._max_messages = max_messages
        self._messages: list[Utterance] = []

    def add(self, message: Utterance) -> Window | None:
        """Buffer a message, returning the window when this one fills it."""
        self._messages.append(message)
        if len(self._messages) < self._max_messages:
            return None
        return self._close()

    def due(self, now: float) -> Window | None:
        """The window if the conversation has been quiet long enough."""
        if not self._messages or now - self._messages[-1].stamp < self._idle_sec:
            return None
        return self._close()

    def pending(self) -> int:
        return len(self._messages)

    def clear(self) -> None:
        """Throw the buffer away: collection went off, and these lines were
        never meant to be kept."""
        self._messages = []

    def forget(self, person_id: str) -> None:
        """Take a forgotten person out of the buffered lines before they can be
        sent anywhere (RFC section 10)."""
        self._messages = list(without_revoked(Window(tuple(self._messages)), lambda who: who == person_id).messages)

    def _close(self) -> Window:
        window = Window(tuple(self._messages))
        self._messages = []
        return window


def tag_views(tracks: Sequence[TrackState], enrolling: Iterable[str] = ()) -> tuple[TagView, ...]:
    """The live tracks as the scribe sees them when a message arrives."""
    fresh = set(enrolling)
    return tuple(
        TagView(
            tag=track.tag,
            state=track.identity.state,
            person_id=track.identity.person_id,
            name=track.identity.name,
            enrolling=track.tag in fresh,
        )
        for track in tracks
        if not track.lost
    )


def chat_in_utterance(
    payload: Mapping[str, Any], *, uid: str, now: float, in_view: Sequence[TagView]
) -> Utterance | None:
    """A ``/brain/chat_in`` entry as one transcript line, or None when it is not
    a person talking to the robot. Simulated environment speech is dropped: it
    comes out of the robot's own speaker on behalf of a scripted resident, so it
    is neither the user in view nor the robot's own words, and a transcript
    claiming either would teach the scribe a lie."""
    if payload.get("sender") == "environment_speech":
        return None
    text = str(payload.get("text") or "").strip()
    if not text:
        return None
    return Utterance(id=uid, stamp=now, speaker=Speaker.USER, text=text, in_view=tuple(in_view))


def chat_out_utterance(
    payload: Mapping[str, Any], *, uid: str, now: float, in_view: Sequence[TagView]
) -> Utterance | None:
    """A ``/brain/chat_out`` entry as one transcript line. Only what the robot
    said out loud and what a skill reported count; thoughts and system notes
    were never spoken, and would read to the scribe as speech."""
    if payload.get("sender") not in (Sender.ROBOT, Sender.SKILL_OUTPUT):
        return None
    text = str(payload.get("text") or "").strip()
    if not text:
        return None
    return Utterance(id=uid, stamp=now, speaker=Speaker.ROBOT, text=text, in_view=tuple(in_view))


def speaking_tag(tracks: Sequence[TrackState], *, max_range_m: float = TALKING_RANGE_M) -> str | None:
    """Who just spoke, when the answer is not a guess: the one live track in
    talking range. Two candidates or none attribute to nobody — the scribe's
    name rules refuse to commit on a guess, and so does this."""
    candidates = [
        track for track in tracks if not track.lost and (track.range_m is None or track.range_m <= max_range_m)
    ]
    return candidates[0].tag if len(candidates) == 1 else None


def held_speaking(pending: tuple[str, float] | None, now: float) -> tuple[str, ...]:
    """The tag a recent chat_in message was attributed to, while it lasts."""
    if pending is None or now >= pending[1]:
        return ()
    return (pending[0],)
