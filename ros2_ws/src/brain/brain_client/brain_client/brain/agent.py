# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The local brain: look → think → act, one turn at a time.

The whole agent is one sequential coroutine on a dedicated loop thread
(:class:`~brain_client.brain.loop.LoopThread`), which buys two guarantees:

* **Cancellation is the only stop mechanism.** Deactivation and reset cancel
  the task; a turn still thinking unwinds at its await and can never absorb
  its response.
* **Turns are transactional.** Events are consumed only when a turn commits,
  so a failed or abandoned turn re-sends them. User speech abandons any turn
  that has not begun to speak, and the rerun sees everything it saw plus the
  new message.

Threading contract: callbacks queue events and wake the loop. Incoming speech
also synchronizes its gate with dispatch and requests owned navigation cancellation;
observing, history, and model-directed acting happen in the coroutine.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import threading
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from brain_client.brain import grounding
from brain_client.brain.context import Decision, GeminiContext, ToolCall
from brain_client.brain.loop import LoopThread
from brain_client.brain.openai_context import OpenAIContext
from brain_client.brain.openai_transport import pick_openai_transport
from brain_client.brain.prompt import build_system_prompt, self_reference_turns
from brain_client.brain.tools import (
    GO_TO_POINT_IN_VIEW,
    START_REQUEST,
    STOP_SKILL,
    WAIT,
    assign_tool_names,
    build_tools,
)
from brain_client.brain.transport import pick_transport
from brain_client.brain.utils import (
    Event,
    EventKind,
    Frame,
    FrameLabel,
    TraceEvent,
    adjust_nav_goal,
    in_image,
    observation_text,
    parse_view_point,
    resolve_timezone,
)
from brain_client.perception.scan_health import ScanHealthReporter
from brain_client.transport.chat import Sender

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclpy.node import Node

    from brain_client.core.config import BrainConfig
    from brain_client.core.state import BrainState, RunningSkill
    from brain_client.perception.battery import BatteryMonitor
    from brain_client.perception.camera import CameraCapture
    from brain_client.perception.gaze_control import GazeController
    from brain_client.perception.identity import IdentityMonitor
    from brain_client.perception.pose import Pose
    from brain_client.perception.pose_tracking import PoseTracker
    from brain_client.perception.scan_health import ScanHealthMonitor
    from brain_client.skills.roster import SkillRoster
    from brain_client.skills.runner import PrimitiveRunner
    from brain_client.transport.chat import ChatManager, SpeechStreamer
    from innate_proxy import ProxyClient

_NAV_TO_POSITION = "innate-os/navigate_to_position"
_INPUT_INTERRUPT_SKILLS = frozenset({_NAV_TO_POSITION, "innate-os/find_next_person"})
_FRESH_FRAME_SEC = 3.0  # an older camera frame means the feed is broken; don't think blind
_MAX_EVENTS_QUEUED = 30  # the oldest beyond this are dropped as stale stimuli
_MAX_EVENT_IMAGES = 4  # newest event images sent per turn; older ones arrive as text only
_MAX_RERUNS = 2  # nonstop user speech cannot starve the loop
_EVENT_TURN_GAP = 1.0  # floor between event-driven turns (feedback chatter); user speech skips it
_DROP_EVENTS_AFTER = 3  # failed turns before the peeked events are dropped (the batch may be the poison)


class BrainAgent:
    def __init__(
        self,
        node: Node,
        state: BrainState,
        config: BrainConfig,
        *,
        camera: CameraCapture,
        pose_tracker: PoseTracker,
        runner: PrimitiveRunner,
        roster: SkillRoster,
        chat: ChatManager,
        gaze: GazeController,
        proxy: ProxyClient | None = None,
        scan_health: ScanHealthMonitor | None = None,
        battery: BatteryMonitor | None = None,
        identity: IdentityMonitor | None = None,
        trace: Callable[[str], None] | None = None,
    ):
        self._logger = node.get_logger()
        self._state = state
        self._config = config
        self._camera = camera
        self._pose = pose_tracker
        self._battery = battery
        self._identity = identity
        self._runner = runner
        self._roster = roster
        self._chat = chat
        self._gaze = gaze
        self._trace_sink = trace  # publishes one JSON string per event on /brain/trace
        self._lidar = ScanHealthReporter(
            scan_health, pose_tracker, chat, self._logger, enabled=not config.simulator_mode
        )
        # Boot tolerates a bad zone; set_timezone rejects one, so a Settings typo surfaces.
        self._timezone = resolve_timezone(config.timezone)
        if config.timezone.strip() and self._timezone is None:
            self._logger.warn(f"[Brain] Unknown timezone '{config.timezone}' — using the host's local zone")

        self.provider = config.brain_provider
        if self.provider == "openai":
            transport, self.backend = pick_openai_transport(proxy)
            context_class = OpenAIContext
            self.model = config.openai_model
            self.reasoning_effort = config.openai_reasoning_effort
        else:
            transport, self.backend = pick_transport(proxy)
            context_class = GeminiContext
            self.model = config.gemini_model
            self.reasoning_effort = config.gemini_thinking_level
        self._context = (
            context_class(
                transport,
                model=self.model,
                thinking_level=self.reasoning_effort,
                max_history=config.history_max_entries,
                max_image_turns=config.history_max_image_turns,
                reference=self_reference_turns(),
            )
            if transport is not None
            else None
        )

        self._events: list[Event] = []
        self._pose_at_capture: Pose | None = None
        self._frame_at_capture: bytes | None = None
        self._pitch_at_capture = 0.0
        self._tool_map: dict[str, str] = {}  # gemini function name -> skill id
        self._request_stopped = False
        self._request_generation = 0
        self._latest_operator_event: Event | None = None
        self._turn_operator_receipt: Event | None = None
        self._acting_on_user_request = False
        self._request_started_this_turn = False
        self._error_streak = 0
        self._activated_at = 0.0
        self._turn_count = 0
        self._turn_started_at = 0.0
        self._turn_in_flight = False
        self._speaker: SpeechStreamer | None = None  # the in-flight turn's streamer (the racing loop reads it)
        self._pause_until = 0.0  # monotonic deadline of the current between-turns pause
        self._departure_anchor: tuple[float, float, float] | None = None
        self._interaction_guard_started_at: float | None = None
        self._turn_user_spoke = False
        # Input callbacks and dispatch share this lock: speech cannot begin
        # between the final gate check and claiming a new skill.
        self._input_lock = threading.RLock()
        self._incoming_speech: dict[object, int] = {}
        self._incoming_timers = {}
        self._speech_epoch = 0
        self._microphone_tokens = {}
        self._input_generation = 0
        self._accept_incoming = state.is_brain_active

        self._runtime = LoopThread("brain-agent")
        self._new_event = asyncio.Event()  # something was queued (loop thread; set via runtime.post)
        self._user_spoke = asyncio.Event()  # like _new_event, but only user speech sets it

        # Set by the composition root: gates only the HEAVY traces (request
        # bodies, frames) — hundreds of KB per turn, otherwise serialized and
        # published for nobody. Small events always publish.
        self.trace_has_audience: Callable[[], bool] = lambda: True

        if self._context is not None:
            if isinstance(self._context, OpenAIContext):
                self._context.on_native_request = self._trace_request
            else:
                self._context.on_request = self._trace_request  # exact request body

    def set_timezone(self, name: str) -> bool:
        """Point the status-line clock at an IANA zone ("" = the host's own); False if unknown."""
        zone = resolve_timezone(name)
        if name.strip() and zone is None:
            return False
        self._timezone = zone
        return True

    @property
    def available(self) -> bool:
        """Whether the brain can reach the selected provider — true exactly when a context exists."""
        return self._context is not None

    @property
    def error_streak(self) -> int:
        """Consecutive failed turns (0 = healthy); the node's health topic reads it."""
        return self._error_streak

    # ================= lifecycle =================
    def start(self) -> bool:
        """Spawn the agent loop; False when it refused (caller must not report active)."""
        if not self.available:
            self._chat.emit_system(
                f"⚠️ The brain has no way to reach {self.provider} — configure the Innate proxy "
                f"(INNATE_SERVICE_KEY) or set {'OPENAI_API_KEY' if self.provider == 'openai' else 'GEMINI_API_KEY'} "
                "in innate-os/.env and restart."
            )
        if self._runtime.running:
            return True
        if not self._runtime.unwound:
            # A previous loop is stuck mid-unwind (stop() timed out): spawning
            # over it would interleave two conversations.
            self._chat.emit_system("⚠️ The previous brain loop is still unwinding — try activating again shortly.")
            return False
        self._activated_at = time.monotonic()
        self._error_streak = 0
        self._departure_anchor = None
        self._interaction_guard_started_at = None
        with self._input_lock:
            self._accept_incoming = True
        self._runtime.spawn(self._loop())
        return True

    def stop(self) -> bool:
        """Synchronous: when this returns True, no turn is thinking and none will act."""
        with self._input_lock:
            self._accept_incoming = False
            for timer in self._incoming_timers.values():
                timer.cancel()
            self._incoming_timers.clear()
            self._incoming_speech.clear()
            self._microphone_tokens.clear()
            self._speech_epoch += 1
            self._input_generation += 1
        unwound = self._runtime.cancel()
        if not unwound:
            self._logger.error("[Brain] Agent loop did not unwind within 5s")
        self._events.clear()
        return unwound

    def reset(self) -> None:
        """Forget the conversation; a turn thinking under the old one dies with it."""
        was_running = self._runtime.running
        if not self.stop():
            self._chat.emit_system("⚠️ Brain loop is stuck — stop and start the brain to recover.")
            return
        if self._context is not None:
            self._context.clear()
        with self._input_lock:
            self._request_stopped = False
            self._request_generation += 1
            self._latest_operator_event = None
        if was_running and self._state.is_brain_active:
            with self._input_lock:
                self._accept_incoming = True
            self._runtime.spawn(self._loop())

    def shutdown(self) -> None:
        self.stop()
        self._runtime.shutdown()

    # ================= the loop =================
    async def _loop(self) -> None:
        """Root task: turns forever, each racing the user's voice, with the 1 Hz
        telemetry heartbeat alongside. A crash (a bug — transport failures back
        off per turn) is reported in chat and leaves the loop down until restart.
        """
        context = self._context
        if context is None:
            return await self._heartbeat()  # no transport: telemetry only, no turns
        heartbeat = asyncio.ensure_future(self._heartbeat())
        turn = spoke = None
        reruns = 0
        try:
            while True:
                await self._await_camera()
                self._user_spoke.clear()
                turn = asyncio.ensure_future(self._turn(context))
                spoke = asyncio.ensure_future(self._user_spoke.wait())
                await asyncio.wait((turn, spoke), return_when=asyncio.FIRST_COMPLETED)
                spoke.cancel()
                if reruns < _MAX_RERUNS and self._abandon(turn):
                    await asyncio.wait({turn})  # fully unwound before the rerun looks
                    reruns += 1
                    continue
                await turn
                reruns = 0
                if not self._events:
                    await self._pause(self._interval())
                else:
                    await self._pause(_EVENT_TURN_GAP, user_only=True)
        except Exception as error:
            self._logger.error(f"[Brain] Agent loop crashed: {error!r}")
            self._chat.emit_system(f"⚠️ Brain loop crashed: {error!r} — stop and start the brain to recover.")
        finally:
            if self._speaker is not None:
                self._speaker.mute()
            children = [task for task in (turn, spoke, heartbeat) if task is not None]
            for task in children:
                task.cancel()
            # stop() must wait for the turn's finally blocks too. Otherwise an
            # old turn can clear the in-flight flag after a restarted turn sets it.
            await asyncio.gather(*children, return_exceptions=True)

    def _abandon(self, turn: asyncio.Task[None]) -> bool:
        """Cancel a thinking turn the user just talked over — unless it already
        holds the floor. try_abandon is atomic with the reply stream: a plain
        check-then-mute could let the first sentence slip out after the decision.
        """
        speaker = self._speaker  # _turn publishes it before its first await
        if turn.done() or speaker is None or not speaker.try_abandon():
            return False
        self._trace(TraceEvent.TURN_PREEMPTED, turn=self._turn_count, after=self._elapsed())
        turn.cancel()
        return True

    async def _turn(self, context: GeminiContext) -> None:
        """One turn: look at the world, think, commit, act.

        ``events`` is a peek at the queue — consumed only when the turn
        commits, so a failed or abandoned turn re-sends the same events.
        """
        del self._events[:-_MAX_EVENTS_QUEUED]  # bound the backlog after an outage
        with self._input_lock:
            events = list(self._events)
            input_generation = self._input_generation
        self._turn_count += 1
        self._turn_started_at = time.monotonic()
        speaker = self._speaker = self._chat.stream_speech()  # published before the first await, for the racing loop
        try:
            await self._think(context, events, speaker, input_generation)
        except asyncio.CancelledError:
            self._trace(TraceEvent.TURN_DROPPED, turn=self._turn_count, latency=self._elapsed())
            raise
        except Exception as error:
            await self._back_off(error, seen=len(events))

    async def _think(
        self, context: GeminiContext, events: list[Event], speaker: SpeechStreamer, input_generation: int
    ) -> None:
        text, frames = self._look(events)
        if self._frame_at_capture is None:
            return  # the feed died between the loop's freshness check and the look
        wrist_frames = [i for i, (label, _) in enumerate(frames) if label == FrameLabel.WRIST]
        message = GeminiContext.user_message(text, [jpeg for _, jpeg in frames])
        tools = self._build_tools(events)
        directive = self._state.current_directive
        system = build_system_prompt(
            directive.get_prompt() if directive else None,
            identity=self._identity.current if self._identity is not None else None,
            running_guidance=self._running_guidance(self._state.primitive_running),
        )
        if self._request_stopped:
            system += (
                "\nThe previous request and remaining steps were cancelled. Do not resume on world updates, "
                "resident speech, or skill completion. Answer conversation in text. Only a fresh operator "
                "instruction may use start_new_request; then wait for next update's action schemas."
            )
        if self._state.log_everything:
            self._logger.info(f"[Brain] Turn input:\n{text}")
        self._trace_turn_start(text, frames, tools, system, context)

        response = await self._generate(context, message, tools, system, speaker, wrist_frames, input_generation)
        latency = self._elapsed()
        self._report_recovered()
        if not self._state.is_brain_active:
            # Deactivation raced this turn's response: drop the whole exchange.
            self._trace(TraceEvent.TURN_DROPPED, turn=self._turn_count, latency=latency)
            return

        decision = context.absorb(message, response, latest_only_images=wrist_frames)
        committed_events = tuple(events)
        with self._input_lock:
            del self._events[: len(events)]
        events.clear()  # committed: a failure below backs off against an empty peek
        self._request_started_this_turn = False
        try:
            outcomes = self._act(decision, speaker, context, input_generation=input_generation)
        finally:
            with self._input_lock:
                pending_operator = self._latest_operator_event
                if (
                    self._input_blocks(input_generation)
                    and pending_operator is not None
                    and any(event is pending_operator for event in committed_events)
                    and pending_operator.request_generation == self._request_generation
                ):
                    # Decide after dispatch: input can begin between commit and
                    # _act. Accepted start/Stop clear this receipt, so neither is
                    # replayed; fenced turns keep the instruction visible.
                    self._events.insert(0, pending_operator)
                self._acting_on_user_request = False
        self._trace(
            TraceEvent.TURN_END,
            turn=self._turn_count,
            latency=latency,
            provider=self.provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            tokens=context.last_usage,
            thoughts=decision.thoughts,
            speech=decision.speech,
            calls=[{"name": call.name, "args": call.args, "outcome": outcome} for call, outcome in outcomes],
            history=context.history_len,
            next_in=round(self._interval(), 1),
        )

    async def _generate(
        self,
        context: GeminiContext,
        message: dict,
        tools: list[dict],
        system: str,
        speaker: SpeechStreamer,
        wrist_frames: list[int],
        input_generation: int,
    ) -> dict:
        """The only blocking call, on a worker thread. Cancellation unwinds HERE —
        the orphaned HTTP call finishes and its result is dropped."""
        self._turn_in_flight = True

        def feed(text: str) -> None:
            with self._input_lock:
                if not self._input_blocks(input_generation):
                    speaker.feed(text)

        try:
            return await asyncio.to_thread(
                context.generate, message, tools, system, feed, latest_only_images=wrist_frames
            )
        finally:
            self._turn_in_flight = False

    def _report_recovered(self) -> None:
        if not self._error_streak:
            return
        self._error_streak = 0
        self._chat.emit_system("✅ Brain recovered.")

    async def _back_off(self, error: Exception, seen: int) -> None:
        """Inference failures and turn-level bugs alike: retry, never die.

        Events stay queued; only the user speaking ends the backoff early
        (motion and feedback chatter must not turn a failing API into a hot
        retry loop).
        """
        self._error_streak += 1
        self._logger.error(f"[Brain] Turn failed ({self._error_streak}x): {error!r}")
        if self._error_streak == 1:
            self._chat.emit_system(f"⚠️ Brain turn failed: {error} — retrying.")
        backoff = min(5.0 * self._error_streak, 30.0)
        if self._error_streak >= _DROP_EVENTS_AFTER and seen:
            # The batch itself may be what fails (e.g. an oversized request):
            # stop resending it verbatim so the loop can recover on its own.
            self._logger.warn(f"[Brain] Dropping {seen} queued event(s) after {self._error_streak} failed turns")
            del self._events[:seen]
            seen = 0
        self._trace(
            TraceEvent.TURN_ERROR, turn=self._turn_count, error=str(error), streak=self._error_streak, backoff=backoff
        )
        await self._pause(backoff, seen=seen, user_only=True)

    async def _await_camera(self) -> None:
        """Hold turns while the camera feed is down; tell the user if it stays down."""
        for _ in range(25):  # brief grace: the feed may just be starting up
            if self._camera.fresh_image_jpeg(_FRESH_FRAME_SEC) is not None:
                return
            await asyncio.sleep(0.2)
        self._logger.error("[Brain] Camera feed is down; holding turns until it returns")
        self._chat.emit_system("⚠️ No camera frames — the brain is waiting for the feed to return.")
        while self._camera.fresh_image_jpeg(_FRESH_FRAME_SEC) is None:
            del self._events[:-_MAX_EVENTS_QUEUED]  # don't hoard stimuli while blind
            await asyncio.sleep(0.2)
        self._chat.emit_system("✅ Camera feed is back.")

    async def _pause(self, seconds: float, *, seen: int = 0, user_only: bool = False) -> None:
        """Sleep up to ``seconds``; the queue growing past ``seen`` events ends it early.

        ``user_only`` narrows the early wake to user speech (the error backoff).
        """
        wake = self._user_spoke if user_only else self._new_event
        wake.clear()
        if any(not user_only or event.kind == EventKind.USER for event in self._events[seen:]):
            return
        self._pause_until = time.monotonic() + seconds
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake.wait(), seconds)
        finally:
            self._pause_until = 0.0

    def _interval(self) -> float:
        directive = self._state.current_directive
        overrides = directive.get_turn_intervals() if directive is not None else None
        if self._state.primitive_running:
            if overrides is not None and overrides.supervision is not None:
                return overrides.supervision
            return self._config.supervision_turn_interval
        if overrides is not None and overrides.idle is not None:
            return overrides.idle
        return self._config.idle_turn_interval

    def _elapsed(self) -> float:
        return round(time.monotonic() - self._turn_started_at, 2)

    # ================= look =================
    def _look(self, events: list[Event]) -> tuple[str, list[Frame]]:
        """Snapshot the world + the peeked events into one turn input.

        Frame, head pitch, and pose are captured together: go_to_point_in_view
        projections must use the geometry of the exact frame the model saw.
        Returns the input text and the turn's (label, jpeg) frames — only the
        bytes go to the model; the labels feed telemetry and mark which frames
        are latest-only in history (the wrist camera).
        """
        self._pose_at_capture = self._pose.current_pose_xyt()
        frame = self._camera.fresh_frame(_FRESH_FRAME_SEC)  # (jpeg, head pitch at frame arrival)
        head_jpeg = self._frame_at_capture = frame[0] if frame is not None else None
        self._pitch_at_capture = frame[1] if frame is not None else 0.0
        arm_jpeg = self._camera.fresh_arm_jpeg(_FRESH_FRAME_SEC)

        frames: list[Frame] = [(FrameLabel.HEAD, head_jpeg)] if head_jpeg is not None else []
        if arm_jpeg is not None:
            frames.append((FrameLabel.WRIST, arm_jpeg))
        frames += [(FrameLabel.EVENT, event.image) for event in events if event.image][-_MAX_EVENT_IMAGES:]

        running = self._state.primitive_running
        text = observation_text(
            now=self._now(),
            uptime_s=int(time.monotonic() - self._activated_at),
            pose=self._pose_at_capture,
            battery=self._battery.current if self._battery is not None else None,
            running_skill=running.primitive_name if running else None,
            events=events,
            has_wrist_frame=arm_jpeg is not None,
        )
        with self._input_lock:
            if self._incoming_speech:
                text += "\nIncoming speech is being delivered. Wait for its transcript before acting or replying."
        return text, frames

    def _now(self) -> datetime:
        """Wall clock as an aware datetime, so the status line can name the zone."""
        return datetime.now(self._timezone) if self._timezone is not None else datetime.now().astimezone()

    def _running_guidance(self, running: RunningSkill | None) -> str:
        if running is None:
            return ""
        meta = self._state.registry.primitives.get(running.skill_id) or {}
        return (meta.get("guidelines_when_running") or "").strip()

    def _build_tools(self, events: list[Event]) -> list[dict]:
        running = self._state.primitive_running
        user_spoke = any(event.kind == EventKind.USER for event in events)
        self._turn_user_spoke = user_spoke
        self._turn_operator_receipt = next((event for event in reversed(events) if event.source == "operator"), None)
        self._acting_on_user_request = any(
            event.kind == EventKind.USER
            and event.source == "operator"
            and event.request_generation == self._request_generation
            and event is self._latest_operator_event
            for event in events
        )
        with self._input_lock:
            if self._incoming_speech:
                self._tool_map = {}
                return build_tools([], running.primitive_name if running else None, user_spoke=user_spoke)
        active_ids = set(self._roster.active_skill_ids())
        skills = [meta for meta in self._state.registry.metadata if meta["id"] in active_ids]
        blocked_ids = self._interaction_blocked_skill_ids()
        if blocked_ids:
            skills = [meta for meta in skills if meta["id"] not in blocked_ids]
        # One naming pass feeds both the declarations and the dispatch map, so
        # the name the model calls always resolves to the skill it was declared for.
        named = assign_tool_names(skills)
        self._tool_map = {name: meta["id"] for name, meta in named}
        if self._request_stopped:
            return build_tools(
                [],
                running.primitive_name if running else None,
                user_spoke=self._acting_on_user_request,
                request_stopped=True,
                can_stop_running=self._departure_guard_reason() is None,
            )
        if running is not None:
            return build_tools(
                [],
                running.primitive_name,
                user_spoke=user_spoke,
                can_stop_running=self._departure_guard_reason() is None,
            )
        return build_tools(named, None, can_go_to_point_in_view=_NAV_TO_POSITION in active_ids, user_spoke=user_spoke)

    # ================= act =================
    def _act(
        self,
        decision: Decision,
        speaker: SpeechStreamer,
        context: GeminiContext,
        *,
        input_generation: int | None = None,
    ) -> list[tuple[ToolCall, str]]:
        # Execute and answer the calls before any chat I/O: a functionCall
        # left unanswered in history poisons every later request.
        outcomes = [(call, self._execute(call, input_generation=input_generation)) for call in decision.calls]
        context.add_tool_outcomes(outcomes)
        if decision.thoughts:
            self._chat.emit_thoughts(decision.thoughts)
        # Serialize the final eligibility check, buffered audio, and dialogue
        # publication with input arrival. Provider work finished before this lock.
        with self._input_lock:
            if self._input_blocks(input_generation):
                speaker.mute()
                if decision.speech:
                    self.add_event(
                        "(Your previous reply was interrupted by incoming speech. "
                        "Respond after consuming its completed transcript.)"
                    )
                # The simulator also consumes robot chat as spoken dialogue:
                # never publish the full stale reply after muting its audio.
                return outcomes
            speech = decision.speech
            user_waiting = any(event.kind == EventKind.USER for event in self._events)
            if speech and not speaker.spoke and user_waiting:
                self._suppress_reply(speaker, speech)
            speaker.flush()  # the reply's last sentence has no trailing boundary
            if speaker.spoke and speech:
                self._chat.emit(
                    Sender.ROBOT, speech, speak=False
                )  # audio went out per sentence; the panel gets one message
        return outcomes

    def _suppress_reply(self, speaker: SpeechStreamer, speech: str) -> None:
        """A reply that never started speaking loses to a newer user message.

        History already holds it verbatim, but the user never heard it — say
        so, or the "never repeat yourself" rule buries the answer.
        """
        self._logger.info(f"[Brain] Speech suppressed (newer user message pending): {speech[:60]!r}")
        speaker.mute()
        self.add_event(
            "(Your previous reply was not spoken — the user had already said "
            "something newer. Answer their latest message.)"
        )

    def _execute(self, call: ToolCall, *, input_generation: int | None = None) -> str:
        """Run one tool call; the returned string is the model-facing outcome.

        Never raises: the turn has already committed, so an escaping error
        would orphan the model's function calls in history.
        """
        self._logger.info(f"[Brain] Tool call: {call.name}({call.args})")
        try:
            return self._dispatch(call, input_generation=input_generation)
        except Exception as error:
            self._logger.error(f"[Brain] Tool call {call.name} failed: {error!r}")
            return f"failed — {error}"

    def _dispatch(self, call: ToolCall, *, input_generation: int | None = None) -> str:
        if call.name == WAIT:
            return "ok"
        with self._input_lock:
            if call.name == STOP_SKILL:
                if (
                    input_generation is not None
                    and input_generation != self._input_generation
                    and not self._turn_user_spoke
                ):
                    return "rejected — incoming speech changed; reassess stopping after consuming its transcript"
                running = self._state.primitive_running
                if self._incoming_speech and running is not None and running.manual and not self._turn_user_spoke:
                    return "rejected — incoming speech does not interrupt a manual skill"
                if self._departure_guard_reason() is not None and not self._turn_user_spoke:
                    return self._stop_skill()
                if call.args.get("continue_task") is not True:
                    self._request_stopped = True
                    self._request_generation += 1
                    newer = self._latest_operator_event
                    if (
                        newer is not None
                        and newer is not self._turn_operator_receipt
                        and any(event is newer for event in self._events)
                    ):
                        # Stop remains authoritative for the old request, but a
                        # later unseen operator receipt belongs to the new epoch.
                        # Preserve its wording/identity ordering for a fresh turn.
                        preserved = Event(newer.text, newer.image, newer.kind, newer.source, self._request_generation)
                        self._events[:] = [
                            preserved if event is newer else event
                            for event in self._events
                            if event.source != "operator" or event is newer
                        ]
                        self._latest_operator_event = preserved
                    else:
                        self._latest_operator_event = None
                        self._events[:] = [event for event in self._events if event.source != "operator"]
                    self._acting_on_user_request = False
                outcome = self._stop_skill()
                if self._request_stopped:
                    outcome += "; remaining steps cancelled — wait for a new operator instruction"
                return outcome
            if self._input_blocks(input_generation):
                if any(event.kind == EventKind.USER and event.source == "operator" for event in self._events):
                    return "rejected — a newer user message is pending; respond to it before acting"
                return "rejected — incoming speech changed; wait for and consume its transcript before acting"
            return self._dispatch_available(call)

    def _input_blocks(self, generation: int | None) -> bool:
        return bool(self._incoming_speech) or (generation is not None and generation != self._input_generation)

    def _dispatch_available(self, call: ToolCall) -> str:
        if call.name == START_REQUEST:
            if (
                not self._request_stopped
                or not self._acting_on_user_request
                or any(event.kind == EventKind.USER for event in self._events)
            ):
                return "rejected — only a new user instruction can begin a new request"
            self._request_stopped = False
            self._latest_operator_event = None
            self._request_started_this_turn = True
            return "new request accepted — carry out only the user's latest instruction"
        if any(event.kind == EventKind.USER for event in self._events):
            return "rejected — a newer user message is pending; respond to it before acting"
        if self._request_started_this_turn:
            return "rejected — wait for the next update and its action schemas before starting the new request"
        if self._request_stopped:
            return "rejected — the request was cancelled; wait for a new user instruction"
        if not self._state.is_brain_active:
            # Deactivation raced this turn's _act: don't start anything new
            # (a goal that slips through anyway is cancelled by the runner's
            # generation bump, but this keeps the robot from twitching first).
            return "rejected — the brain is deactivating"
        # Only names declared this turn resolve: falling back to the full
        # registry would let a hallucinated call bypass the active-skill allowlist.
        # The map is a turn old by now and the roster can change under it, so
        # the resolved id is re-checked against the live active set.
        skill_id = self._tool_map.get(call.name)
        if self._state.primitive_running is not None:
            return "rejected — another skill is already running; stop it first"
        if call.name == GO_TO_POINT_IN_VIEW:
            outcome = self._go_to_point_in_view(call.args)
            if self._acting_on_user_request and not outcome.startswith("rejected"):
                self._latest_operator_event = None
            return outcome
        if skill_id is None:
            return f"unknown skill '{call.name}'"
        if skill_id not in self._roster.active_skill_ids():
            return f"rejected — {call.name} is no longer available"
        self._start_skill(skill_id, self._adjust_nav_goal(skill_id, dict(call.args)))
        if self._acting_on_user_request:
            self._latest_operator_event = None
        return "started — you will get an event when it finishes"

    def _stop_skill(self) -> str:
        guard_reason = self._departure_guard_reason()
        if guard_reason is not None and not self._turn_user_spoke:
            return f"rejected — {guard_reason}"
        if self._runner.has_active_goal:
            self._runner.cancel_active_goal()
            return "stopping — you will get an event when it has stopped"
        if self._state.primitive_running is None:
            return "no skill is running"
        # A run this client didn't start (webapp/CLI manual run): the skills
        # server's owner-agnostic cancel is the only handle.
        if self._runner.cancel_external():
            return "stopping — you will get an event when it has stopped"
        return "could not stop it — the skills server is unreachable"

    def _departure_guard_reason(self) -> str | None:
        """Why the current skill must continue away from a recent identity anchor."""
        directive = self._state.current_directive
        policy = directive.get_departure_guard() if directive is not None else None
        running = self._state.primitive_running
        anchor = self._departure_anchor
        if policy is None or running is None or anchor is None or running.skill_id not in policy.protected_skill_ids:
            return None
        anchor_x, anchor_y, anchored_at = anchor
        elapsed = time.monotonic() - anchored_at
        if elapsed >= policy.maximum_hold_s:
            self._departure_anchor = None
            return None
        pose = self._pose.current_pose_xyt()
        if pose is not None and math.hypot(pose[0] - anchor_x, pose[1] - anchor_y) >= policy.minimum_departure_m:
            self._departure_anchor = None
            return None
        return (
            f"continue {running.primitive_name} until the robot has departed at least "
            f"{policy.minimum_departure_m:g} m from the already-known person "
            f"(or {policy.maximum_hold_s:g} s elapse)"
        )

    def _interaction_guard_policy(self):
        directive = self._state.current_directive
        getter = getattr(directive, "get_interaction_guard", None) if directive is not None else None
        return getter() if getter is not None else None

    def _interaction_blocked_skill_ids(self) -> frozenset[str]:
        policy = self._interaction_guard_policy()
        started_at = self._interaction_guard_started_at
        if policy is None or started_at is None:
            return frozenset()
        if time.monotonic() - started_at >= policy.maximum_hold_s:
            self._interaction_guard_started_at = None
            return frozenset()
        return frozenset(policy.blocked_skill_ids)

    def _go_to_point_in_view(self, args: dict) -> str:
        """Project the pointed-at floor pixel into a local navigate_to_position goal."""
        # Gate on the ACTIVE set, not the registry: a hallucinated call must
        # not drive the base by resolving through the full registry.
        if _NAV_TO_POSITION not in self._roster.active_skill_ids():
            return "rejected — navigate_to_position is not available"
        if self._frame_at_capture is None:
            return "rejected — no camera frame to ground the point in"
        point = parse_view_point(args)
        if point is None:
            return "rejected — give integer y and x in 0-1000 image coordinates"
        if not in_image(*point):
            return "rejected — y and x must be within 0-1000 image coordinates"
        v_norm, u_norm = point
        standoff = grounding.parse_standoff(args.get("standoff_m", grounding.STANDOFF_M))
        if standoff is None:
            return "rejected — standoff_m must be a finite number from 0.35 to 1.5 meters"
        if standoff != grounding.STANDOFF_M:
            floor = grounding.conversation_floor(
                u_norm, v_norm, frame_jpeg=self._frame_at_capture, pitch_deg=self._pitch_at_capture
            )
        else:
            floor = grounding.pixel_to_floor(
                u_norm,
                v_norm,
                frame_jpeg=self._frame_at_capture,
                vertical_fov_deg=self._config.vertical_fov,
                pitch_deg=self._pitch_at_capture,
                cam_height=self._config.height_cam,
                cam_forward=self._config.x_cam,
            )
        if floor is None:
            if standoff == grounding.STANDOFF_M:
                return "rejected — that point is at or above the horizon; point at the floor"
            return "rejected — that point cannot be projected; use visible floor in the current main camera frame"
        if standoff == grounding.STANDOFF_M:
            inputs = self._adjust_nav_goal(_NAV_TO_POSITION, grounding.approach_goal(*floor))
        else:
            current_pose = self._pose.current_pose_xyt()
            if self._pose_at_capture is None or current_pose is None:
                return "rejected — conversation approach needs capture and current robot poses"
            # Rebase the target, not a previously shortened goal: a robot that
            # moved into conversation range must not drive back to its old pose.
            target = adjust_nav_goal(
                {"x": floor[0], "y": floor[1], "theta_degrees": 0.0, "local_frame": True},
                capture_pose=self._pose_at_capture,
                current_pose=current_pose,
                is_mapfree=self._pose.is_mapfree,
            )
            floor = (target["x"], target["y"])
            goal = grounding.approach_goal(*floor, standoff_m=standoff)
            if goal["x"] == goal["y"] == 0.0 and abs(goal["theta_degrees"]) <= 5.0:
                return "already positioned — no movement needed; continue this encounter without another approach"
            # This goal is already relative to current_pose. Only convert to the
            # static map if needed; applying the capture delta again would drift.
            inputs = adjust_nav_goal(
                goal,
                capture_pose=current_pose,
                current_pose=current_pose,
                is_mapfree=self._pose.is_mapfree,
                use_static_map=getattr(self._pose, "cur_nav_mode", None) == "navigation",
            )
        self._logger.info(
            f"[Brain] go_to_point_in_view ({v_norm:.0f},{u_norm:.0f}) -> floor ({floor[0]:.2f}, {floor[1]:.2f})m, "
            f"goal ({inputs['x']:.2f}, {inputs['y']:.2f}, {inputs['theta_degrees']:.0f}°) "
            f"pitch={self._pitch_at_capture:.0f}°"
        )
        self._start_skill(_NAV_TO_POSITION, inputs)
        if standoff == grounding.STANDOFF_M:
            return (
                f"driving to the floor point {math.hypot(*floor):.1f}m away (stopping ~{grounding.STANDOFF_M}m short) "
                "— you will get an event when it finishes"
            )
        capped = math.hypot(*floor) > grounding.MAX_RANGE_M
        return (
            f"approaching floor point {math.hypot(*floor):.1f}m away with {standoff}m requested standoff"
            + (
                " (capped step; target remains farther away; conversation distance NOT reached — do not greet yet)"
                if capped
                else ""
            )
            + " — you will get an event when it finishes; inspect a fresh main camera image before any next approach"
        )

    def _start_skill(self, skill_id: str, inputs: dict) -> None:
        self._gaze.pause()
        self._runner.start_task(skill_id, f"local-{uuid.uuid4().hex[:8]}", inputs)

    def _adjust_nav_goal(self, skill_id: str, inputs: dict) -> dict:
        if skill_id != _NAV_TO_POSITION:
            return inputs
        adjusted = adjust_nav_goal(
            inputs,
            capture_pose=self._pose_at_capture,
            current_pose=self._pose.current_pose_xyt(),
            is_mapfree=self._pose.is_mapfree,
            use_static_map=getattr(self._pose, "cur_nav_mode", None) == "navigation",
        )
        if adjusted is not inputs:
            self._logger.info(f"[Brain] nav goal re-based to the current pose: {adjusted}")
        return adjusted

    # ================= events (executor thread) =================
    def listening_enabled(self) -> bool:
        directive = self._state.current_directive
        return bool(directive and getattr(directive, "listen_before_acting", lambda: False)())

    def speech_context(self) -> tuple[int, int] | None:
        with self._input_lock:
            if not self._accept_incoming or not self._state.is_brain_active:
                return None
            return self._speech_epoch, self._request_generation

    def complete_speech(self, context, token, text: str, *, source: str) -> None:
        with self._input_lock:
            if context is None or context != self.speech_context() or not self._accept_incoming:
                if token in self._incoming_speech:
                    self.finish_incoming_speech(token, "")
                return
            if token is not None:
                self.finish_incoming_speech(token, text, source=source)
            elif text and self._state.is_brain_active:
                self.on_user_message(text, source=source, request_generation=context[1])

    def on_microphone_speech(self, data: dict) -> None:
        token_id = data.get("utterance_id")
        if not isinstance(token_id, str) or not token_id:
            return
        with self._input_lock:
            if data.get("stage") == "started":
                if not self.listening_enabled() or token_id in self._microphone_tokens:
                    return
                context = self.speech_context()
                token = self.begin_incoming_speech(context)
                if token is not None:
                    self._microphone_tokens[token_id] = context, token
            elif data.get("stage") == "finished":
                entry = self._microphone_tokens.pop(token_id, None)
                if entry is not None:
                    text = data.get("text", "")
                    self.complete_speech(*entry, text if isinstance(text, str) else "", source="operator")

    def begin_incoming_speech(self, context=None) -> object | None:
        """Gate on acoustic/playback onset; no transcript yet."""
        with self._input_lock:
            if (
                not self._accept_incoming
                or not self._state.is_brain_active
                or not self.listening_enabled()
                or (context is not None and context != self.speech_context())
            ):
                return None
            token = object()
            self._incoming_speech[token] = self._request_generation
            # Also bound orphaned holds if the input process disappears entirely.
            timer = threading.Timer(90.0, self.finish_incoming_speech, args=(token, ""))
            timer.daemon = True
            self._incoming_timers[token] = timer
            timer.start()
            self._input_generation += 1
            try:
                running = self._state.primitive_running
                if running is not None and not running.manual and running.skill_id in _INPUT_INTERRUPT_SKILLS:
                    self._runner.interrupt_for_input(_INPUT_INTERRUPT_SKILLS)
                self.add_event("Incoming speech started. Wait for the completed transcript before continuing.")
            except Exception:
                self._incoming_speech.pop(token)
                timer = self._incoming_timers.pop(token, None)
                if timer is not None:
                    timer.cancel()
                self._input_generation += 1
                raise
            return token

    def finish_incoming_speech(self, token: object | None, text: str, *, source: str = "environment") -> None:
        """Queue the transcript before reopening dispatch; stale/duplicate callbacks do nothing."""
        with self._input_lock:
            if token not in self._incoming_speech:
                return
            for key, (_, active_token) in list(self._microphone_tokens.items()):
                if active_token is token:
                    self._microphone_tokens.pop(key)
            try:
                if text and self._accept_incoming and self._state.is_brain_active:
                    self.on_user_message(text, source=source, request_generation=self._incoming_speech[token])
            finally:
                self._incoming_speech.pop(token)
                timer = self._incoming_timers.pop(token, None)
                if timer is not None:
                    timer.cancel()
                self._input_generation += 1

    def add_event(
        self,
        text: str,
        image: bytes | None = None,
        kind: EventKind = EventKind.INFO,
        *,
        source: str = "system",
        request_generation: int | None = None,
    ) -> Event | None:
        """Queue something that happened; the loop wakes for an immediate turn."""
        if not self.available:
            return  # no transport, no loop: these would accumulate forever
        event = Event(text, image, kind, source, request_generation)
        self._events.append(event)
        self._runtime.post(self._wake, kind)
        self._trace(TraceEvent.EVENT, kind=kind, text=text, image=image is not None)
        return event

    def _wake(self, kind: EventKind) -> None:
        """Loop thread: end any pause; user speech also abandons a housekeeping turn."""
        self._new_event.set()
        if kind == EventKind.USER:
            self._user_spoke.set()

    def on_user_message(self, text: str, *, source: str = "operator", request_generation: int | None = None) -> None:
        with self._input_lock:
            if source == "operator":
                self._input_generation += 1  # fence in-flight speech/actions at receipt, before interpretation
            generation = self._request_generation if request_generation is None else request_generation
            label = "The user says" if source == "operator" else "The resident says"
            event = self.add_event(
                f'{label}: "{text}"', kind=EventKind.USER, source=source, request_generation=generation
            )
            if source == "operator":
                self._latest_operator_event = event

    def on_custom_input(self, data: dict) -> None:
        device = data.get("input_device", "unknown")
        self.add_event(f"Input from {device}: {json.dumps(data)}")

    def on_skill_event(
        self, status: str, skill_name: str, detail: str | None = None, image: bytes | None = None
    ) -> None:
        directive = self._state.current_directive
        policy = directive.get_departure_guard() if directive is not None else None
        if (
            policy is not None
            and status == "completed"
            and skill_name in policy.trigger_skill_names
            and detail is not None
            and any(detail.startswith(prefix) for prefix in policy.trigger_result_prefixes)
        ):
            pose = self._pose.current_pose_xyt()
            if pose is not None:
                self._departure_anchor = (pose[0], pose[1], time.monotonic())
        interaction_policy = self._interaction_guard_policy()
        if interaction_policy is not None and status == "completed" and detail is not None:
            if skill_name in interaction_policy.release_skill_names and any(
                detail.startswith(prefix) for prefix in interaction_policy.release_result_prefixes
            ):
                self._interaction_guard_started_at = None
            elif skill_name in interaction_policy.trigger_skill_names and any(
                detail.startswith(prefix) for prefix in interaction_policy.trigger_result_prefixes
            ):
                self._interaction_guard_started_at = time.monotonic()
        line = f"Skill {skill_name} {status}"
        if detail:
            line += f": {detail}"
        self.add_event(line, image=image)

    def on_skill_feedback(self, skill_name: str, feedback: str, image: bytes | None = None) -> None:
        # The running skill is already present in every observation. A bare
        # status adds no information, but queuing it bypasses the supervision
        # pause and spends a model turn on short skills before they finish.
        # Keep visual supervision on its normal timer and all actual feedback.
        if not image and feedback.strip().casefold() in {"", "running"}:
            return
        self.add_event(f"Update from running skill {skill_name}: {feedback}", image=image)

    # ================= telemetry =================
    def _trace(self, event: TraceEvent, *, heavy: bool = False, **fields) -> None:
        """Publish one JSON telemetry event on /brain/trace (no-op when unwired).

        ``heavy`` marks events carrying request bodies or frames — hundreds of
        KB to serialize per turn, published only while something subscribes.
        """
        if self._trace_sink is None or (heavy and not self.trace_has_audience()):
            return
        self._trace_sink(json.dumps({"ev": event, "t": time.time(), **fields}))

    def _trace_request(self, body: dict) -> None:
        self._trace(TraceEvent.TURN_REQUEST, heavy=True, turn=self._turn_count, body=body)

    def _trace_turn_start(
        self, text: str, frames: list[Frame], tools: list[dict], system: str, context: GeminiContext
    ) -> None:
        if self._trace_sink is None or not self.trace_has_audience():
            return  # skip the base64 work entirely, not just the publish
        self._trace(
            TraceEvent.TURN_START,
            heavy=True,
            turn=self._turn_count,
            input=text,
            images=len(frames),
            tools=[d["name"] for d in tools[0]["functionDeclarations"]],
            history=context.history_len,
            history_images=context.image_turn_count,
            system=system,
            frames=[{"label": label, "jpeg": base64.b64encode(jpeg).decode()} for label, jpeg in frames],
        )

    async def _heartbeat(self) -> None:
        while True:
            self._lidar.tick()
            self._snapshot()
            await asyncio.sleep(1.0)

    def _snapshot(self) -> None:
        """Trace the loop's live state (the monitor's heartbeat)."""
        if self._trace_sink is None:
            return
        running = self._state.primitive_running
        self._trace(
            TraceEvent.SNAPSHOT,
            active=self._state.is_brain_active,
            backend=self.backend,
            provider=self.provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            interval=self._interval(),
            turn=self._turn_count,
            in_flight=self._turn_in_flight,
            thinking_for=self._elapsed() if self._turn_in_flight else 0,
            queued=[{"kind": e.kind, "text": e.text[:200]} for e in list(self._events)],
            next_in=max(0.0, round(self._pause_until - time.monotonic(), 1)) if self._pause_until else 0.0,
            streak=self._error_streak,
            running=running.primitive_name if running else None,
            history=self._context.history_len if self._context else 0,
            tokens=self._context.last_usage if self._context else {},
            uptime=round(time.monotonic() - self._activated_at, 0) if self._state.is_brain_active else 0,
            motion=round(self._camera.motion_peak(), 4),
        )
