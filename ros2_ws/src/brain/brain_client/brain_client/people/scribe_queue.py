# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The windows waiting for Gemini to come back, bounded to one hour on disk,
and the JSON they are written down as — this file is the only place a window is
persisted, and a forget has to reach it (RFC 6.3 and section 10)."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from brain_client.people.memory import as_dict, as_list, enum_member, optional_str
from brain_client.people.store import DIR_MODE, FILE_MODE
from brain_client.people.transcript import Speaker, TagView, Utterance, Window
from brain_client.people.types import IdentityState

if TYPE_CHECKING:
    from pathlib import Path

QUEUE_HORIZON_SEC = 3600.0  # one hour of windows survives an outage; older ones are dropped
QUEUE_MAX_WINDOWS = 240


class WindowQueue:
    """Windows waiting for Gemini to come back, bounded to one hour on disk.

    One JSON line per window, rewritten whole on every change: a few hundred
    small records, and a torn file would cost more than the rewrite does.
    """

    def __init__(self, path: Path, *, horizon_sec: float = QUEUE_HORIZON_SEC, limit: int = QUEUE_MAX_WINDOWS):
        self._path = path
        self._horizon_sec = horizon_sec
        self._limit = limit
        self._windows: list[Window] = _read_queue(path)

    def push(self, window: Window, now: float) -> None:
        self._windows.append(window)
        self._prune(now)
        self._commit()

    def expire(self, now: float) -> None:
        """Drop the windows past the horizon from disk as well: the hour they
        are promised has to pass whether or not anything is spending them."""
        if self._prune(now):
            self._commit()

    def pending(self, now: float) -> list[Window]:
        self.expire(now)
        return list(self._windows)

    def pop(self, window: Window) -> None:
        if window in self._windows:
            self._windows.remove(window)
            self._commit()

    def clear(self) -> None:
        self._windows = []
        self._commit()

    def forget(self, person_id: str) -> int:
        """Drop every queued window the person was in view for, and say how many.
        Deleting someone has to reach the work still in flight, or the outage
        that queued it sends their transcript to Gemini when it clears."""
        kept = [window for window in self._windows if person_id not in window.person_ids()]
        dropped = len(self._windows) - len(kept)
        if dropped:
            self._windows = kept
            self._commit()
        return dropped

    def __len__(self) -> int:
        return len(self._windows)

    def _prune(self, now: float) -> bool:
        """Whether anything was dropped — pruning only ever removes, so the
        count is the whole answer, and it tells the caller to rewrite the file."""
        kept = [window for window in self._windows if now - window.closed <= self._horizon_sec][-self._limit :]
        if len(kept) == len(self._windows):
            return False
        self._windows = kept
        return True

    def _commit(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text("".join(json.dumps(window_to_dict(window)) + "\n" for window in self._windows), encoding="utf-8")
        os.chmod(tmp, FILE_MODE)  # verbatim transcripts and person ids, like every other store file
        os.replace(tmp, self._path)


def window_to_dict(window: Window) -> dict:
    return {
        "messages": [
            {
                "id": message.id,
                "stamp": message.stamp,
                "speaker": str(message.speaker),
                "text": message.text,
                "in_view": [
                    {
                        "tag": view.tag,
                        "state": str(view.state),
                        "person_id": view.person_id,
                        "name": view.name,
                        "enrolling": view.enrolling,
                    }
                    for view in message.in_view
                ],
            }
            for message in window.messages
        ]
    }


def window_from_dict(data: dict) -> Window:
    messages = []
    for entry in as_list(data.get("messages")):
        message = as_dict(entry)
        messages.append(
            Utterance(
                id=str(message.get("id", "")),
                stamp=float(message.get("stamp") or 0.0),
                speaker=Speaker.ROBOT if message.get("speaker") == Speaker.ROBOT else Speaker.USER,
                text=str(message.get("text", "")),
                in_view=tuple(
                    TagView(
                        tag=str(as_dict(view).get("tag", "")),
                        state=_state(as_dict(view).get("state")),
                        person_id=optional_str(as_dict(view).get("person_id")),
                        name=optional_str(as_dict(view).get("name")),
                        enrolling=bool(as_dict(view).get("enrolling", False)),
                    )
                    for view in as_list(message.get("in_view"))
                ),
            )
        )
    return Window(tuple(messages))


def _read_queue(path: Path) -> list[Window]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    windows: list[Window] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # one torn line must not cost the rest of the queue
        if isinstance(data, dict):
            windows.append(window_from_dict(data))
    return windows


def _state(value: object) -> IdentityState:
    return enum_member(IdentityState, value, IdentityState.UNKNOWN)
