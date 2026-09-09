# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The scribe worker: the agent watching the conversation and writing down what
is worth keeping (RFC 6.3, docs/rfc/people-memory.md in innate-jetson). It
buffers ``/brain/chat_in`` and ``/brain/chat_out`` into windows
(:mod:`brain_client.people.transcript`), spends one Gemini call per window
(:mod:`brain_client.people.scribe_prompt`, :mod:`brain_client.people.scribe_output`)
and treats the answer as a proposal :mod:`brain_client.people.scribe_rules`
decides on, queues on disk what an outage could not send
(:mod:`brain_client.people.scribe_queue`), and answers memory questions
(:mod:`brain_client.people.recall`). PURE: no rclpy, no network of its own —
the transport is injected as ``(path, body, timeout) -> dict`` (what
:class:`brain_client.brain.transport.GeminiRest`'s ``post`` is); stamps are
epoch seconds."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from brain_client.brain.transport import GENERATE_PATH
from brain_client.people.memory import FactKind, profile_to_dict
from brain_client.people.recall import parse_recall, recall_request
from brain_client.people.scribe_output import parse_output
from brain_client.people.scribe_prompt import build_request
from brain_client.people.scribe_queue import WindowQueue
from brain_client.people.scribe_rules import apply
from brain_client.people.transcript import WindowBuffer, line_unless_revoked, without_revoked

if TYPE_CHECKING:
    from pathlib import Path

    from brain_client.people.memory import Fact
    from brain_client.people.scribe_output import ScribeOutput
    from brain_client.people.scribe_rules import Change
    from brain_client.people.store import PeopleStore
    from brain_client.people.transcript import Utterance, Window

Transport = Callable[[str, dict, float | None], dict]
"""(api path, request body, timeout) -> parsed response; GeminiRest.post's shape."""

DRAIN_PER_CYCLE = 2  # windows per drain: a backlog must not hold up the live conversation
SCRIBE_TIMEOUT_SEC = 20.0
RECALL_TIMEOUT_SEC = 20.0


class Scribe:
    """Windows in, changes out. The node feeds it chat messages and ticks it;
    every call to Gemini happens on the caller's thread, so the node runs it on
    a worker and never on its executor."""

    def __init__(
        self,
        store: PeopleStore,
        transport: Transport | None,
        *,
        model: str,
        queue_path: Path,
        timeout: float = SCRIBE_TIMEOUT_SEC,
    ):
        self._store = store
        self._transport = transport
        self._model = model
        self._timeout = timeout
        self._buffer = WindowBuffer()
        self._queue = WindowQueue(queue_path)
        self._forgotten: set[str] = set()
        self.last_error = ""

    @property
    def queued(self) -> int:
        return len(self._queue)

    def observe(self, message: Utterance, now: float) -> list[Change]:
        """Buffer one chat message; a window that fills up is spent at once.
        Nothing is buffered while collection is off — that switch is what it
        means (RFC section 10) — and the flag is read here rather than where the
        message was queued, because it may have flipped in between."""
        if not self._store.collection_enabled():
            return []
        live = line_unless_revoked(message, self._revoked)
        if live is None:
            return []
        window = self._buffer.add(live)
        return [] if window is None else self.process(window, now)

    def tick(self, now: float) -> list[Change]:
        """Close an idle window and drain whatever an outage left queued."""
        if not self._store.collection_enabled():
            self._buffer.clear()
            self._queue.expire(now)  # the hour on disk runs whether or not it is being spent
            return []
        changes = self.drain(now)
        window = self._buffer.due(now)
        if window is not None:
            changes.extend(self.process(window, now))
        return changes

    def process(self, window: Window, now: float) -> list[Change]:
        """One window, end to end. A window Gemini could not take queues on
        disk; recognition never depends on any of this."""
        window = self._live(window)
        if not window.messages or not self._store.collection_enabled():
            return []
        try:
            output = self._call(window)
        except Exception as error:  # noqa: BLE001 — any transport failure queues the window for the drain
            self.last_error = repr(error)
            self._queue.push(window, now)
            return []
        return [] if output is None else self._commit(output, window, now)

    def drain(self, now: float, limit: int = DRAIN_PER_CYCLE) -> list[Change]:
        """Spend up to ``limit`` queued windows, oldest first, stopping at the
        first failure — the connection is still down and the rest can wait.
        Each one is a blocking Gemini call on the thread that also carries the
        live chat, so an hour of backlog is drained a couple of windows at a
        time rather than all 240 in one cycle."""
        if not self._store.collection_enabled():
            return []
        changes: list[Change] = []
        for queued in self._queue.pending(now)[:limit]:
            window = self._live(queued)
            if not window.messages:
                self._queue.pop(queued)
                continue
            try:
                output = self._call(window)
            except Exception as error:  # noqa: BLE001 — the rest of the queue waits for the connection
                self.last_error = repr(error)
                break
            self._queue.pop(queued)
            if output is not None:
                changes.extend(self._commit(output, window, now))
        return changes

    def forget(self, person_id: str) -> None:
        """Everything about a forgotten person that has not been written yet:
        the open window, the queue an outage filled, and every call and write
        this thread has not made yet (RFC section 10)."""
        self._forgotten.add(person_id)
        self._buffer.forget(person_id)
        self._queue.forget(person_id)

    def recall(self, person_id: str, question: str, *, alone_in_view: bool = False) -> str:
        """Deep recall over one person's memory; empty when it adds nothing.

        ``alone_in_view`` is whether the subject is the only person the robot
        can see. RFC 6.3 lets a sensitive fact surface through deep recall when
        the person asks, so with anybody else there it is not even sent: an
        answer is spoken out loud, and the room would hear it."""
        profile = self._store.profile(person_id)
        if profile is None or self._transport is None or self._revoked(person_id):
            return ""
        record = profile_to_dict(profile)
        if not alone_in_view:
            record["facts"] = [fact for fact in record["facts"] if fact.get("kind") != FactKind.SENSITIVE]
        try:
            response = self._transport(
                GENERATE_PATH.format(model=self._model),
                recall_request(record, question),
                RECALL_TIMEOUT_SEC,
            )
        except Exception as error:  # noqa: BLE001 — a failed recall is silence, never a broken turn
            self.last_error = repr(error)
            return ""
        return parse_recall(response)

    def _commit(self, output: ScribeOutput, window: Window, now: float) -> list[Change]:
        """The proposal turned into writes, gated a second time: a forget — or
        the owner switching collection off — during the Gemini call takes back
        what that call was about to write."""
        window = self._live(window)
        if not window.messages or not self._store.collection_enabled():
            return []
        return apply(output, window, self._store, now)

    def _live(self, window: Window) -> Window:
        return without_revoked(window, self._revoked)

    def _revoked(self, person_id: str | None) -> bool:
        """Whether a forget has taken this person back (RFC section 10). Both
        answers count: the store's tombstone is the durable one, and the set
        holds the forgets this scribe was handed directly."""
        if person_id is None:
            return False
        return person_id in self._forgotten or self._store.is_tombstoned(person_id)

    def _call(self, window: Window) -> ScribeOutput | None:
        if self._transport is None:
            raise RuntimeError("no Gemini transport configured")
        body = build_request(window, self._context_facts(window))
        output = parse_output(self._transport(GENERATE_PATH.format(model=self._model), body, self._timeout))
        if output is None:
            self.last_error = "unreadable scribe answer"
        return output

    def _context_facts(self, window: Window) -> dict[str, list[Fact]]:
        facts: dict[str, list[Fact]] = {}
        for tag, view in window.views().items():
            profile = self._store.profile(view.person_id) if view.person_id else None
            if profile is not None:
                facts[tag] = [fact for fact in profile.facts if fact.superseded_by is None]
        return facts
