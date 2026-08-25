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

Threading contract: ROS callbacks (executor thread) only queue events and wake
the loop; observing, history, and acting all happen in the coroutine.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from brain_client.brain import grounding
from brain_client.brain.context import Decision, GeminiContext, ToolCall
from brain_client.brain.loop import LoopThread
from brain_client.brain.prompt import build_system_prompt, self_reference_turns
from brain_client.brain.tools import GO_TO_POINT_IN_VIEW, STOP_SKILL, WAIT, assign_tool_names, build_tools
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

        transport, self.backend = pick_transport(proxy)
        self._context = (
            GeminiContext(
                transport,
                model=config.gemini_model,
                thinking_level=config.gemini_thinking_level,
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
        self._error_streak = 0
        self._activated_at = 0.0
        self._turn_count = 0
        self._turn_started_at = 0.0
        self._turn_in_flight = False
        self._speaker: SpeechStreamer | None = None  # the in-flight turn's streamer (the racing loop reads it)
        self._pause_until = 0.0  # monotonic deadline of the current between-turns pause

        self._runtime = LoopThread("brain-agent")
        self._new_event = asyncio.Event()  # something was queued (loop thread; set via runtime.post)
        self._user_spoke = asyncio.Event()  # like _new_event, but only user speech sets it

        # Set by the composition root: gates only the HEAVY traces (request
        # bodies, frames) — hundreds of KB per turn, otherwise serialized and
        # published for nobody. Small events always publish.
        self.trace_has_audience: Callable[[], bool] = lambda: True

        if self._context is not None:
            self._context.on_request = self._trace_request  # the monitor renders the exact request body

    def set_timezone(self, name: str) -> bool:
        """Point the status-line clock at an IANA zone ("" = the host's own); False if unknown."""
        zone = resolve_timezone(name)
        if name.strip() and zone is None:
            return False
        self._timezone = zone
        return True

    @property
    def available(self) -> bool:
        """Whether the brain can reach Gemini — true exactly when a context exists."""
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
                "⚠️ The brain has no way to reach Gemini — configure the Innate proxy "
                "(INNATE_SERVICE_KEY) or set GEMINI_API_KEY in innate-os/.env and restart."
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
        self._runtime.spawn(self._loop())
        return True

    def stop(self) -> bool:
        """Synchronous: when this returns True, no turn is thinking and none will act."""
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
        if was_running and self._state.is_brain_active:
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
            heartbeat.cancel()
            if self._speaker is not None:
                self._speaker.mute()
            for task in (turn, spoke):
                if task is not None:
                    task.cancel()  # stop() can land mid-race; the turn dies with the loop

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
        """One turn: look at the world, think with Gemini, commit, act.

        ``events`` is a peek at the queue — consumed only when the turn
        commits, so a failed or abandoned turn re-sends the same events.
        """
        del self._events[:-_MAX_EVENTS_QUEUED]  # bound the backlog after an outage
        events = list(self._events)
        self._turn_count += 1
        self._turn_started_at = time.monotonic()
        speaker = self._speaker = self._chat.stream_speech()  # published before the first await, for the racing loop
        try:
            await self._think(context, events, speaker)
        except asyncio.CancelledError:
            self._trace(TraceEvent.TURN_DROPPED, turn=self._turn_count, latency=self._elapsed())
            raise
        except Exception as error:
            await self._back_off(error, seen=len(events))

    async def _think(self, context: GeminiContext, events: list[Event], speaker: SpeechStreamer) -> None:
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
        )
        if self._state.log_everything:
            self._logger.info(f"[Brain] Turn input:\n{text}")
        self._trace_turn_start(text, frames, tools, system, context)

        response = await self._generate(context, message, tools, system, speaker, wrist_frames)
        latency = self._elapsed()
        self._report_recovered()
        if not self._state.is_brain_active:
            # Deactivation raced this turn's response: drop the whole exchange.
            self._trace(TraceEvent.TURN_DROPPED, turn=self._turn_count, latency=latency)
            return

        decision = context.absorb(message, response, latest_only_images=wrist_frames)
        del self._events[: len(events)]
        events.clear()  # committed: a failure below backs off against an empty peek
        outcomes = self._act(decision, speaker, context)
        self._trace(
            TraceEvent.TURN_END,
            turn=self._turn_count,
            latency=latency,
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
    ) -> dict:
        """The only blocking call, on a worker thread. Cancellation unwinds HERE —
        the orphaned HTTP call finishes and its result is dropped."""
        self._turn_in_flight = True
        try:
            return await asyncio.to_thread(
                context.generate, message, tools, system, speaker.feed, latest_only_images=wrist_frames
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
        if self._state.primitive_running:
            return self._config.supervision_turn_interval
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
            guidance=self._running_guidance(running),
            events=events,
            has_wrist_frame=arm_jpeg is not None,
        )
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
        active_ids = set(self._roster.active_skill_ids())
        skills = [meta for meta in self._state.registry.metadata if meta["id"] in active_ids]
        # One naming pass feeds both the declarations and the dispatch map, so
        # the name the model calls always resolves to the skill it was declared for.
        named = assign_tool_names(skills)
        self._tool_map = {name: meta["id"] for name, meta in named}
        if running is not None:
            return build_tools([], running.primitive_name, user_spoke=user_spoke)
        return build_tools(named, None, can_go_to_point_in_view=_NAV_TO_POSITION in active_ids)

    # ================= act =================
    def _act(self, decision: Decision, speaker: SpeechStreamer, context: GeminiContext) -> list[tuple[ToolCall, str]]:
        # Execute and answer the calls before any chat I/O: a functionCall
        # left unanswered in history poisons every later request.
        outcomes = [(call, self._execute(call)) for call in decision.calls]
        context.add_tool_outcomes(outcomes)
        if decision.thoughts:
            self._chat.emit_thoughts(decision.thoughts)
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

    def _execute(self, call: ToolCall) -> str:
        """Run one tool call; the returned string is the model-facing outcome.

        Never raises: the turn has already committed, so an escaping error
        would orphan the model's function calls in history.
        """
        self._logger.info(f"[Brain] Tool call: {call.name}({call.args})")
        try:
            return self._dispatch(call)
        except Exception as error:
            self._logger.error(f"[Brain] Tool call {call.name} failed: {error!r}")
            return f"failed — {error}"

    def _dispatch(self, call: ToolCall) -> str:
        if call.name == WAIT:
            return "ok"
        if call.name == STOP_SKILL:
            return self._stop_skill()
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
            return self._go_to_point_in_view(call.args)
        if skill_id is None:
            return f"unknown skill '{call.name}'"
        if skill_id not in self._roster.active_skill_ids():
            return f"rejected — {call.name} is no longer available"
        self._start_skill(skill_id, self._adjust_nav_goal(skill_id, dict(call.args)))
        return "started — you will get an event when it finishes"

    def _stop_skill(self) -> str:
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
            return "rejected — that point is at or above the horizon; point at the floor"
        inputs = self._adjust_nav_goal(_NAV_TO_POSITION, grounding.approach_goal(*floor))
        self._logger.info(
            f"[Brain] go_to_point_in_view ({v_norm:.0f},{u_norm:.0f}) -> floor ({floor[0]:.2f}, {floor[1]:.2f})m, "
            f"goal ({inputs['x']:.2f}, {inputs['y']:.2f}, {inputs['theta_degrees']:.0f}°) "
            f"pitch={self._pitch_at_capture:.0f}°"
        )
        self._start_skill(_NAV_TO_POSITION, inputs)
        return (
            f"driving to the floor point {math.hypot(*floor):.1f}m away (stopping ~{grounding.STANDOFF_M}m short) "
            "— you will get an event when it finishes"
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
        )
        if adjusted is not inputs:
            self._logger.info(f"[Brain] nav goal re-based to the current pose: {adjusted}")
        return adjusted

    # ================= events (executor thread) =================
    def add_event(self, text: str, image: bytes | None = None, kind: EventKind = EventKind.INFO) -> None:
        """Queue something that happened; the loop wakes for an immediate turn."""
        if not self.available:
            return  # no transport, no loop: these would accumulate forever
        self._events.append(Event(text, image, kind))
        self._runtime.post(self._wake, kind)
        self._trace(TraceEvent.EVENT, kind=kind, text=text, image=image is not None)

    def _wake(self, kind: EventKind) -> None:
        """Loop thread: end any pause; user speech also abandons a housekeeping turn."""
        self._new_event.set()
        if kind == EventKind.USER:
            self._user_spoke.set()

    def on_user_message(self, text: str) -> None:
        self.add_event(f'The user says: "{text}"', kind=EventKind.USER)

    def on_custom_input(self, data: dict) -> None:
        device = data.get("input_device", "unknown")
        self.add_event(f"Input from {device}: {json.dumps(data)}")

    def on_skill_event(
        self, status: str, skill_name: str, detail: str | None = None, image: bytes | None = None
    ) -> None:
        line = f"Skill {skill_name} {status}"
        if detail:
            line += f": {detail}"
        self.add_event(line, image=image)

    def on_skill_feedback(self, skill_name: str, feedback: str, image: bytes | None = None) -> None:
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
            model=self._config.gemini_model,
            turn=self._turn_count,
            in_flight=self._turn_in_flight,
            thinking_for=self._elapsed() if self._turn_in_flight else 0,
            queued=[{"kind": e.kind, "text": e.text[:200]} for e in list(self._events)],
            next_in=max(0.0, round(self._pause_until - time.monotonic(), 1)) if self._pause_until else 0.0,
            streak=self._error_streak,
            running=running.primitive_name if running else None,
            history=self._context.history_len if self._context else 0,
            uptime=round(time.monotonic() - self._activated_at, 0) if self._state.is_brain_active else 0,
            motion=round(self._camera.motion_peak(), 4),
        )
