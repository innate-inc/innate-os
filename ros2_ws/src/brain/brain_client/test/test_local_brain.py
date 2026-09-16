# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the local brain's pure core (no ROS, no network).

Covers the model layer the agent loop depends on: skill metadata -> tool
definitions, reply -> Decision (speech / thoughts / calls), and the history
pruning that keeps requests small (the "image cache"). ChatContext only
touches the network in generate(), so everything else is exercised against a
scripted Replay provider and the assertions read the canonical Request it was
handed — the monitor's dialect is asserted only where the monitor is the
subject.
"""

import json

import pytest
from innate_llm import (
    Finish,
    LlmError,
    Message,
    Part,
    Reply,
    Request,
    Role,
    Text,
    TextDelta,
    Thinking,
    Thought,
    ThoughtDelta,
    ToolCall,
    ToolResult,
    Usage,
)
from innate_llm.replay import Replay

from brain_client.brain.context import ChatContext, decision_from
from brain_client.brain.prompt import build_system_prompt
from brain_client.brain.tools import (
    STOP_SKILL,
    WAIT,
    assign_tool_names,
    build_tools,
    tool_name,
)

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"

NAV_SKILL = {
    "id": "innate-os/navigate_to_position",
    "name": "navigate_to_position",
    "guidelines": "Navigate to x, y (meters).",
    "inputs": {
        "x": {"type": "float", "required": True},
        "y": {"type": "float", "required": True},
        "local_frame": {"type": "bool", "required": False, "default": False},
        "mode": {"type": "str", "required": False, "enum": ["fast", "safe"]},
    },
}
WAVE_SKILL = {"id": "local/wave", "name": "wave", "guidelines": "Wave the arm.", "inputs": {}}


def reply(*parts: Part, finish: Finish = Finish.STOP) -> Reply:
    return Reply(Message(Role.ASSISTANT, parts), Usage(), finish)


def call_reply(name: str, args: dict | None = None, call_id: str = "c1") -> Reply:
    return reply(ToolCall(call_id, name, args or {}), finish=Finish.TOOL_CALLS)


def make_context(
    replay: Replay | None = None,
    max_history: int = 60,
    max_image_turns: int = 2,
    thinking: Thinking = Thinking.DEFAULT,
) -> ChatContext:
    return ChatContext(
        replay or Replay([reply(Text("ok"))]),
        thinking=thinking,
        max_history=max_history,
        max_image_turns=max_image_turns,
    )


# ---------- tool building ----------


def test_tool_name_sanitizes_invalid_characters():
    assert tool_name("navigate_to_position") == "navigate_to_position"
    assert tool_name("Wave Hello!") == "Wave_Hello_"
    assert len(tool_name("x" * 100)) == 64


def test_tool_name_never_starts_with_a_digit():
    # Vendors require function names to start with a letter or underscore; a
    # digit-leading skill would 400 every request while it is active.
    assert tool_name("3d_scan") == "_3d_scan"
    assert tool_name("-dash") == "_-dash"
    assert len(tool_name("3" + "x" * 100)) == 64


def test_assign_tool_names_disambiguates_collisions_and_builtins():
    colliding = [
        {"id": "a", "name": "Wave Hello!"},
        {"id": "b", "name": "Wave Hello?"},  # sanitizes to the same name as 'a'
        {"id": "c", "name": "wait"},  # shadows the built-in wait tool
        {"id": "d", "name": "y" * 100},
        {"id": "e", "name": "y" * 100},  # truncates to the same name as 'd'
    ]
    named = assign_tool_names(colliding)
    names = [name for name, _ in named]
    assert names[0] == "Wave_Hello_"
    assert names[1] == "Wave_Hello__2"
    assert names[2] == "wait_2"
    assert len(names) == len(set(names)) and WAIT not in names
    assert all(len(name) <= 64 for name in names)
    # The definitions use the same disambiguated names.
    declared = [d.name for d in build_tools(named, None)]
    assert declared[:5] == names


def test_build_tools_declares_one_function_per_skill_plus_wait():
    declarations = build_tools(assign_tool_names([NAV_SKILL, WAVE_SKILL]), None)
    assert [d.name for d in declarations] == ["navigate_to_position", "wave", "wait"]

    nav = declarations[0]
    assert nav.description == NAV_SKILL["guidelines"]
    params = nav.parameters
    assert params["type"] == "object"
    assert set(params["properties"]) == {"x", "y", "local_frame", "mode"}
    assert params["required"] == ["x", "y"]
    assert params["properties"]["x"]["type"] == "number"
    assert params["properties"]["local_frame"]["type"] == "boolean"
    assert params["properties"]["mode"]["enum"] == ["fast", "safe"]
    # No-input skills carry an empty object schema (a tool must have one).
    assert declarations[1].parameters == {"type": "object", "properties": {}}


def test_build_tools_while_running_offers_only_stop_and_wait():
    declarations = build_tools([], "navigate_to_position")
    assert [d.name for d in declarations] == [STOP_SKILL, "wait"]
    assert "navigate_to_position" in declarations[0].description


def test_build_tools_while_running_with_user_speech_offers_stop_alone():
    # Offered any no-op tool the model calls it and goes silent, so a turn
    # carrying a user message gets stop alone — text becomes the reply channel.
    declarations = build_tools([], "wave", user_spoke=True)
    assert [d.name for d in declarations] == [STOP_SKILL]
    assert "NOT reasons to stop" in declarations[0].description


def test_build_tools_with_no_skills_still_offers_wait():
    assert [d.name for d in build_tools([], None)] == ["wait"]


def test_unknown_param_type_falls_back_to_annotated_string():
    skill = {
        "id": "s",
        "name": "s",
        "guidelines": "g",
        "inputs": {"blob": {"type": "dict[str, float]", "required": True}},
    }
    schema = build_tools(assign_tool_names([skill]), None)[0].parameters["properties"]["blob"]
    assert schema["type"] == "string"
    assert "dict[str, float]" in schema["description"]


@pytest.mark.parametrize("declared", ["float | None", "None | float", "Optional[float]", "typing.Optional[float]"])
def test_optional_numeric_tool_parameters_remain_numbers(declared):
    skill = {"id": "s", "name": "s", "inputs": {"x": {"type": declared, "required": False, "default": None}}}
    params = build_tools(assign_tool_names([skill]), None)[0].parameters
    assert params["properties"]["x"]["anyOf"] == [{"type": "number"}, {"type": "null"}]
    assert params["required"] == []


@pytest.mark.parametrize("declared", ["list[float] | None", "Optional[List[float]]"])
def test_optional_joint_list_is_a_native_numeric_array(declared):
    skill = {"id": "s", "name": "s", "inputs": {"joints": {"type": declared, "required": False}}}
    params = build_tools(assign_tool_names([skill]), None)[0].parameters
    assert params["properties"]["joints"]["anyOf"] == [{"type": "array", "items": {"type": "number"}}, {"type": "null"}]


# ---------- decisions ----------


def test_decision_separates_speech_thoughts_and_calls():
    message = Message(
        Role.ASSISTANT,
        (Thought("I see a person."), Text("Hello there!"), ToolCall("c1", "wave", {})),
    )
    decision = decision_from(message)
    assert decision.speech == "Hello there!"
    assert decision.thoughts == "I see a person."
    assert [(c.id, c.name, c.args) for c in decision.calls] == [("c1", "wave", {})]


def test_decision_tolerates_an_empty_or_contentless_reply():
    assert decision_from(Message(Role.ASSISTANT, ())).calls == []
    assert decision_from(Message(Role.ASSISTANT, ())).speech is None
    # Unspeakable text (no letters or digits) is not speech either.
    assert decision_from(Message(Role.ASSISTANT, (Text("--- "),))).speech is None
    assert decision_from(Message(Role.ASSISTANT, (ToolCall("c", "wave", {}),))).calls[0].args == {}


# ---------- history / image pruning ----------


def user_turn(text: str, with_image: bool) -> Message:
    return ChatContext.user_message(text, [JPEG] if with_image else [])


def images_in(message: Message) -> int:
    return len(message.image_indexes())


def traced_images_in(content: dict) -> int:
    """Frames in one turn of the monitor's request body."""
    return sum(1 for part in content["parts"] if "inlineData" in part)


def user_turns(context: ChatContext) -> list[Message]:
    return [m for m in context.history if m.role == Role.USER]


def test_prune_keeps_images_only_in_newest_turns():
    context = make_context(max_image_turns=2)
    for i in range(5):
        context.absorb(user_turn(f"turn {i}", with_image=True), reply(Text("ok")))

    turns = user_turns(context)
    assert [images_in(m) for m in turns] == [0, 0, 0, 1, 1]
    assert context.image_turn_count == 2  # what turn_start traces as history_images
    # Stripped frames leave a placeholder so the transcript still reads coherently.
    assert any("removed" in text for text in turns[0].texts())


def test_absorb_keeps_only_the_newest_wrist_frame():
    # Head frames follow the image-turn window; wrist frames are latest-only —
    # a stale gripper close-up reads as current grasp state.
    context = make_context(max_image_turns=3)
    for i in range(3):
        message = ChatContext.user_message(f"turn {i}", [JPEG, JPEG])  # head + wrist
        context.absorb(message, reply(Text("ok")), latest_only_images=[1])

    turns = user_turns(context)
    assert [images_in(m) for m in turns] == [1, 1, 2]
    assert any("wrist camera frame removed" in text for text in turns[0].texts())


def test_wrist_frame_survives_turns_without_one():
    # The arm camera going stale must not orphan-prune the one wrist frame left.
    context = make_context(max_image_turns=3)
    context.absorb(
        ChatContext.user_message("with wrist", [JPEG, JPEG]),
        reply(Text("ok")),
        latest_only_images=[1],
    )
    context.absorb(ChatContext.user_message("head only", [JPEG]), reply(Text("ok")))
    assert [images_in(m) for m in user_turns(context)] == [2, 1]


def test_generate_ships_exactly_one_wrist_frame():
    # Absorb's prune runs only after the response, so without send-time masking
    # every request would carry the previous turn's wrist frame plus the new one.
    replay = Replay([reply(Text("ok"))])
    context = make_context(replay, max_image_turns=3)
    context.absorb(
        ChatContext.user_message("turn 1", [JPEG, JPEG]),
        reply(Text("ok")),
        latest_only_images=[1],
    )
    context.generate(ChatContext.user_message("turn 2", [JPEG, JPEG]), [], "S", latest_only_images=[1])

    messages = replay.last.messages
    assert images_in(messages[0]) == 1  # previous turn on the wire: head frame only
    assert any("wrist camera frame removed" in text for text in messages[0].texts())
    assert images_in(messages[-1]) == 2  # the new message: head + wrist
    # Stored history is untouched until absorb commits the exchange.
    assert images_in(context.history[0]) == 2


def test_generate_keeps_the_old_wrist_frame_when_this_turn_has_none():
    replay = Replay([reply(Text("ok"))])
    context = make_context(replay, max_image_turns=3)
    context.absorb(
        ChatContext.user_message("turn 1", [JPEG, JPEG]),
        reply(Text("ok")),
        latest_only_images=[1],
    )
    context.generate(ChatContext.user_message("turn 2", [JPEG]), [], "S", latest_only_images=[])
    assert images_in(replay.last.messages[0]) == 2  # arm camera stale: last wrist frame still shown


def test_generate_taps_the_exact_request_body():
    # The on_request hook must see the request verbatim, in the monitor's own
    # dialect: system, full history (images and all), and the new message — it
    # feeds the /brain/trace monitor.
    context = make_context()
    seen = []
    context.on_request = seen.append
    context.absorb(user_turn("earlier", True), reply(Text("old")))
    context.generate(user_turn("now", True), tools=[], system="sys")
    (body,) = seen
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert traced_images_in(body["contents"][0]) == 1 and traced_images_in(body["contents"][-1]) == 1


def test_generate_pins_the_prefix_the_caches_need():
    # Breakpoint hints: the first turn (it fronts the system prompt and tools)
    # and the newest one; the turns between move too often to be worth pinning.
    replay = Replay([reply(Text("ok"))])
    context = make_context(replay)
    context.absorb(user_turn("earlier", False), reply(Text("old")))
    context.generate(user_turn("now", False), [], "S")

    messages = replay.last.messages
    assert messages[0].pin and messages[-1].pin
    assert not messages[1].pin


def test_prune_with_zero_image_turns_strips_every_frame():
    context = make_context(max_image_turns=0)
    for i in range(3):
        context.absorb(user_turn(f"turn {i}", with_image=True), reply(Text("ok")))
    assert all(images_in(m) == 0 for m in user_turns(context))


def test_prune_caps_history_and_never_starts_on_orphaned_tool_return():
    context = make_context(max_history=4)
    for i in range(6):
        decision = context.absorb(user_turn(f"turn {i}", False), call_reply("wave", call_id=f"c{i}"))
        context.add_tool_outcomes([(decision.calls[0], "started")])

    history = context.history
    assert len(history) <= 4
    assert history[0].role == Role.USER
    assert not any(isinstance(p, ToolResult) for p in history[0].parts)


def test_absorb_keeps_thought_parts_for_replay():
    # Vendors sign their thinking (Gemini 3, Claude) and demand it back on the
    # next request, so the model turn is stored whole; thoughts still surface.
    context = make_context()
    decision = context.absorb(user_turn("hi", False), reply(Thought("planning..."), Text("Hello!")))
    model_turn = context.history[-1]
    assert model_turn.role == Role.ASSISTANT
    assert [type(p) for p in model_turn.parts] == [Thought, Text]
    assert decision.thoughts == "planning..." and decision.speech == "Hello!"


def test_thoughts_are_stripped_once_the_prefix_they_were_signed_against_changes():
    # Claude signs thinking against the system prompt, tool set and earlier
    # turns and 400s on a replay after any of them changed — a skill starting
    # swaps both. Same prefix: replayed; new prefix: stripped, in the request
    # and durably, so the history matches what the wire accepted.
    thoughtful = Replay(script=lambda _: [reply(Thought("hmm"), Text("ok"))])
    context = make_context(thoughtful)
    context.absorb(user_turn("one", False), context.generate(user_turn("one", False), [], "S"))
    context.absorb(user_turn("two", False), context.generate(user_turn("two", False), [], "S"))
    assert [type(p) for p in thoughtful.last.messages[1].parts] == [Thought, Text]
    assert [type(p) for p in context.history[1].parts] == [Thought, Text]

    context.absorb(user_turn("three", False), context.generate(user_turn("three", False), [], "S + skill"))
    assert [type(p) for p in thoughtful.last.messages[1].parts] == [Text]
    assert [type(p) for p in context.history[1].parts] == [Text]
    assert [type(p) for p in context.history[-1].parts] == [Thought, Text]  # the new turn's own signature stands


def test_clear_empties_history():
    context = make_context()
    context.absorb(user_turn("hi", False), reply(Text("hello")))
    context.clear()
    assert context.history == ()


def test_tool_outcomes_are_recorded_as_tool_returns():
    context = make_context()
    decision = context.absorb(
        user_turn("go", False),
        call_reply("navigate_to_position", {"x": 1}, call_id="abc"),
    )
    context.add_tool_outcomes([(decision.calls[0], "started")])
    (part,) = context.history[-1].parts
    assert part == ToolResult("abc", "navigate_to_position", "started")


def test_generate_sends_the_system_prompt_tools_and_thinking():
    replay = Replay([reply(Text("ok"))])
    context = make_context(replay, thinking=Thinking.HIGH)
    traced = []
    context.on_request = traced.append
    tools = build_tools(assign_tool_names([WAVE_SKILL]), None)
    context.generate(user_turn("hello", True), tools, "SYSTEM")

    request = replay.last
    assert request.system == "SYSTEM"
    assert [t.name for t in request.tools] == ["wave", "wait"]
    assert request.thinking == Thinking.HIGH
    assert images_in(request.messages[-1]) == 1
    # The monitor renders the same request in its own dialect, thinking included.
    assert traced[0]["generationConfig"]["thinking"] == "high"


def test_generate_streams_speech_deltas_and_assembles_the_response():
    replay = Replay(
        [
            ThoughtDelta("thinking..."),
            TextDelta("One. "),
            TextDelta("Two"),
            Reply(
                Message(Role.ASSISTANT, (Thought("thinking..."), Text("One. Two"), ToolCall("c9", "wave", {}))),
                Usage(),
                Finish.TOOL_CALLS,
            ),
        ]
    )
    heard = []
    context = make_context(replay)
    answer = context.generate(user_turn("hi", False), [], "S", on_speech=heard.append)

    assert heard == ["One. ", "Two"]  # thoughts never reach the speech stream
    assert [type(p) for p in answer.message.parts] == [Thought, Text, ToolCall]
    assert answer.message.text() == "One. Two"
    decision = decision_from(answer.message)
    assert decision.speech == "One. Two"
    assert decision.thoughts == "thinking..."
    assert [(c.name, c.id) for c in decision.calls] == [("wave", "c9")]


def test_generate_raises_on_an_empty_reply_instead_of_committing_silence():
    # A reply with no parts (safety block, malformed function call, empty
    # candidate) must fail the turn — committing it would record a silent,
    # answerless exchange and consume the user's message with no reply.
    context = make_context(Replay([Reply(Message(Role.ASSISTANT, ()), Usage(), Finish.STOP)]))
    with pytest.raises(RuntimeError, match="no content"):
        context.generate(user_turn("hi", False), [], "S")


def test_generate_surfaces_a_stream_that_never_replied():
    context = make_context(Replay([]))
    with pytest.raises(LlmError, match="stream ended without a reply"):
        context.generate(user_turn("hi", False), [], "S")


# ---------- visual grounding (pixel -> floor target) ----------

from brain_client.brain import grounding  # noqa: E402


def fake_jpeg(width: int, height: int) -> bytes:
    """Minimal JPEG header: SOI + SOF0 carrying the given dimensions."""
    return b"\xff\xd8" + b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x00" * 12


CAM = dict(vertical_fov_deg=80.0, cam_height=0.19663, cam_forward=0.0197)
FRAME = fake_jpeg(640, 480)


def test_jpeg_dimensions_reads_sof():
    assert grounding.jpeg_dimensions(fake_jpeg(1280, 800)) == (1280, 800)
    assert grounding.jpeg_dimensions(b"not a jpeg") is None


def test_center_pixel_with_head_down_projects_ahead():
    # Head 10° down, image center: floor hit at cam_forward + h/tan(10°) along +x.
    x, y = grounding.pixel_to_floor(500, 500, frame_jpeg=FRAME, pitch_deg=-10.0, **CAM)
    assert abs(x - 1.135) < 0.01
    assert abs(y) < 1e-6


def test_center_pixel_level_camera_is_horizon():
    assert grounding.pixel_to_floor(500, 500, frame_jpeg=FRAME, pitch_deg=0.0, **CAM) is None
    # Above the horizon even more so.
    assert grounding.pixel_to_floor(500, 100, frame_jpeg=FRAME, pitch_deg=0.0, **CAM) is None


def test_bottom_edge_is_close_and_left_is_positive_y():
    x, y = grounding.pixel_to_floor(500, 1000, frame_jpeg=FRAME, pitch_deg=0.0, **CAM)
    assert abs(x - 0.254) < 0.01
    left_x, left_y = grounding.pixel_to_floor(250, 900, frame_jpeg=FRAME, pitch_deg=0.0, **CAM)
    assert left_y > 0  # left half of the image -> +y (robot's left)


def test_near_horizon_pixel_is_range_capped():
    x, y = grounding.pixel_to_floor(500, 510, frame_jpeg=FRAME, pitch_deg=0.0, **CAM)
    import math

    assert abs(math.hypot(x, y) - grounding.MAX_RANGE_M) < 1e-6


def test_approach_goal_stops_short_and_faces_the_point():
    goal = grounding.approach_goal(2.0, 0.0)
    assert abs(goal["x"] - (2.0 - grounding.STANDOFF_M)) < 1e-6
    assert goal["y"] == 0.0
    assert goal["theta_degrees"] == 0.0
    assert goal["local_frame"] is True
    # Point already inside the standoff: no travel, just turn to face it.
    close = grounding.approach_goal(0.0, 0.2)
    assert close["x"] == 0.0 and close["y"] == 0.0
    assert abs(close["theta_degrees"] - 90.0) < 1e-6


# ---------- agent loop (fake node, no network) ----------

import asyncio  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from innate_llm import Backend, Llm  # noqa: E402

from brain_client.brain.agent import BrainAgent  # noqa: E402
from brain_client.brain.utils import Event, EventKind  # noqa: E402
from brain_client.core.state import BrainState, RunningSkill  # noqa: E402
from brain_client.transport.chat import SpeechStreamer  # noqa: E402


@pytest.fixture
def agent_factory(monkeypatch):
    """Build agents against stub collaborators; shut their loop threads down after."""
    created = []

    def make(trace=None, on_thinking_changed=None) -> tuple[BrainAgent, BrainState]:
        logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
        node = SimpleNamespace(get_logger=lambda: logger)
        config = SimpleNamespace(
            llm_model="replay:m",
            llm_thinking="",
            llm_base_url="",
            llm_extra_body="",
            history_max_entries=60,
            history_max_image_turns=2,
            idle_turn_interval=3.0,
            supervision_turn_interval=5.0,
            simulator_mode=False,
            timezone="",
        )
        state = BrainState()
        state.is_brain_active = True
        camera = SimpleNamespace(
            fresh_image_jpeg=lambda max_age: JPEG,
            fresh_frame=lambda max_age: (JPEG, -10.0),
            fresh_arm_jpeg=lambda max_age: None,
            current_head_pitch=-10.0,
            motion_peak=lambda: 0.0,
        )
        pose = SimpleNamespace(current_pose_xyt=lambda: None, is_mapfree=False)
        spoken = []
        chat = SimpleNamespace(
            emit_system=lambda *a, **k: None,
            emit=lambda *a, **k: None,
            emit_thoughts=lambda *a, **k: None,
            speak=lambda text, replace_pending=False, reply_id=None: spoken.append((text, replace_pending)),
            spoken=spoken,
        )
        chat.stream_speech = lambda: SpeechStreamer(chat)
        agent = BrainAgent(
            node,
            state,
            config,
            camera=camera,
            pose_tracker=pose,
            runner=SimpleNamespace(),
            roster=SimpleNamespace(active_skill_ids=lambda: []),
            chat=chat,
            gaze=SimpleNamespace(pause=lambda: None),
            llm=Llm("replay:m", Replay([reply(Text("ok"))]), Backend.DIRECT),
            trace=trace,
            on_thinking_changed=on_thinking_changed,
        )
        created.append(agent)
        return agent, state

    yield make
    for agent in created:
        agent.shutdown()


def answers(agent: BrainAgent, replay: Replay) -> Replay:
    """Point the agent's context at a scripted provider; returns it for its records."""
    agent._context._provider = replay
    return replay


def run_turn(agent: BrainAgent) -> None:
    """Run one turn to completion on the agent's own loop thread."""
    assert agent._context is not None  # the loop hands _turn the context it guarded on
    asyncio.run_coroutine_threadsafe(agent._turn(agent._context), agent._runtime.loop).result(timeout=5)


def no_pause(agent: BrainAgent, monkeypatch) -> None:
    """Skip the between-turns / backoff pauses so tests don't sleep."""

    async def skip(seconds, *, seen=0, user_only=False):
        pass

    monkeypatch.setattr(agent, "_pause", skip)


# ---------- switching model ----------


def fake_configure(monkeypatch, **providers):
    """Stand in for configure(): each spec answers with the provider named for it, or none."""
    from brain_client.brain import agent as agent_module

    def configure(spec, proxy, **kwargs):
        return Llm(spec, providers.get(spec), Backend.DIRECT if providers.get(spec) else Backend.UNCONFIGURED)

    monkeypatch.setattr(agent_module, "configure", configure)


def test_a_model_with_no_way_in_is_refused_and_the_running_one_keeps_thinking(agent_factory, monkeypatch):
    # A mistyped setting, or Claude before its key is pasted, must not leave the robot
    # with no brain at all — the switch fails and says how to fix it.
    agent, _ = agent_factory()
    serving = agent._context
    fake_configure(monkeypatch)  # nothing is reachable

    ok, detail = agent.use_model("anthropic:claude-sonnet-5", agent=False)

    assert ok is False and "Keys" in detail
    assert agent._context is serving and agent.model == "replay:m"


def test_switching_starts_a_fresh_conversation_on_the_new_model(agent_factory, monkeypatch):
    agent, _ = agent_factory()
    agent._context.absorb(user_turn("hi", False), reply(Text("hello")))
    fake_configure(monkeypatch, **{"google:gemini-3.6-flash": Replay([reply(Text("ok"))])})

    ok, spec = agent.use_model("google:gemini-3.6-flash", agent=False)

    assert (ok, spec) == (True, "google:gemini-3.6-flash")
    assert agent.model == "google:gemini-3.6-flash"  # what the trace chip reports
    assert agent._context is not None and agent._context.history == ()


def test_the_active_agents_model_outranks_the_robots_setting(agent_factory, monkeypatch):
    agent, _ = agent_factory()
    fake_configure(
        monkeypatch,
        **{"anthropic:claude-opus-5": Replay([reply(Text("ok"))]), "replay:m": Replay([reply(Text("ok"))])},
    )

    agent.use_model("anthropic:claude-opus-5", agent=True)
    # The Settings model changes under it: the agent asked for its own, so it keeps it.
    assert agent.use_model("replay:m", agent=False) == (True, "anthropic:claude-opus-5")
    assert agent.model == "anthropic:claude-opus-5"
    # An agent that names none falls back to the setting, as it stood when it changed.
    assert agent.use_model(None, agent=True) == (True, "replay:m")


def test_a_server_url_change_reaches_the_next_request(agent_factory, monkeypatch):
    # The URL is the whole configuration of a LAN model: applying the model name live while
    # the route stayed at boot's value would send the new name to the old server.
    agent, _ = agent_factory()
    seen = {}
    from brain_client.brain import agent as agent_module

    def configure(spec, proxy, **kwargs):
        seen.update(kwargs)
        return Llm(spec, Replay([reply(Text("ok"))]), Backend.DIRECT)

    monkeypatch.setattr(agent_module, "configure", configure)

    assert agent.use_llm_setting("llm_base_url", "http://10.0.0.5:8000/v1")[0] is True
    assert seen["base_url"] == "http://10.0.0.5:8000/v1"
    # And it stays the route the next model switch is configured against.
    agent.use_model("openai-chat:nemotron", agent=False)
    assert seen["base_url"] == "http://10.0.0.5:8000/v1"


def test_failed_turn_leaves_events_queued_for_the_retry(agent_factory, monkeypatch):
    agent, state = agent_factory()

    def boom(request: Request):
        raise RuntimeError("boom")

    answers(agent, Replay(script=boom))
    no_pause(agent, monkeypatch)
    agent.on_user_message("bring me a snack")
    run_turn(agent)

    # Nothing was consumed (turns are transactional): the retry re-sends them.
    assert [e.text for e in agent._events] == ['The user says: "bring me a snack"']
    assert agent._context.history == ()  # the failed exchange never entered history
    assert agent._error_streak == 1


def test_error_backoff_ignores_chatter_but_wakes_for_user_speech(agent_factory):
    # The backoff must stay a backoff under motion/feedback chatter — only the
    # user speaking earns a failing API an immediate retry.
    agent, state = agent_factory()
    future = asyncio.run_coroutine_threadsafe(agent._pause(10.0, seen=0, user_only=True), agent._runtime.loop)
    time.sleep(0.1)
    agent.add_event("Motion detected in the camera view", kind=EventKind.MOTION)
    time.sleep(0.3)
    assert not future.done()
    agent.on_user_message("are you there?")
    future.result(timeout=2)


def test_turn_start_drops_the_oldest_backlog_beyond_the_cap(agent_factory):
    # An outage plus a chatty scene must not grow the turn input without
    # bound: the oldest stimuli are dropped at the cap (they are stale).
    agent, state = agent_factory()
    replay = answers(agent, Replay([call_reply(WAIT)]))
    for i in range(40):
        agent.add_event(f"stimulus {i}")
    run_turn(agent)

    text = replay.last.messages[-1].text()
    assert "stimulus 9" not in text  # the 10 oldest were dropped
    assert "stimulus 10" in text and "stimulus 39" in text
    assert agent._events == []  # the survivors were consumed by the commit


def test_committed_turn_consumes_exactly_the_events_it_saw(agent_factory):
    agent, state = agent_factory()
    answers(agent, Replay([call_reply(WAIT)]))
    agent.on_user_message("hello")
    run_turn(agent)

    assert agent._events == []
    # History: the user turn, the model turn, and the wait call's tool result.
    assert agent._context.history_len == 3


def test_a_call_outside_the_active_skill_set_is_rejected(agent_factory):
    # The registry knows every installed skill, but only names declared this
    # turn may dispatch — a hallucinated call must not bypass the directive's
    # active-skill allowlist.
    from brain_client.skills.registry import SkillRegistry

    agent, state = agent_factory()
    state.registry = SkillRegistry.from_metadata([WAVE_SKILL, {**WAVE_SKILL, "id": "local/pick", "name": "pick"}])
    started = []
    agent._runner.start_task = lambda *a, **k: started.append(a)
    agent._roster.active_skill_ids = lambda: ["local/wave"]
    answers(agent, Replay([call_reply("pick")]))
    run_turn(agent)

    assert started == []
    assert agent._context.history[-1].parts[0].text == "unknown skill 'pick'"


def test_a_stale_tool_name_is_rechecked_against_the_live_active_set(agent_factory):
    # The dispatch map is built at the top of the turn, so it can outlive a
    # roster change that lands while the model is thinking: a name resolved
    # through it must still clear the live active set before dispatch.
    from brain_client.skills.registry import SkillRegistry

    agent, state = agent_factory()
    state.registry = SkillRegistry.from_metadata([WAVE_SKILL])
    started = []
    agent._runner.start_task = lambda *a, **k: started.append(a)
    agent._roster.active_skill_ids = lambda: ["local/wave"]
    answers(agent, Replay([call_reply("wave")]))
    run_turn(agent)  # populates the dispatch map with wave
    assert len(started) == 1

    # Next turn: wave is deactivated while the model is thinking — after the
    # dispatch map was built from the roster that still held it.
    def deactivate(request: Request):
        agent._roster.active_skill_ids = lambda: []
        return [call_reply("wave")]

    answers(agent, Replay(script=deactivate))
    run_turn(agent)

    assert len(started) == 1  # the stale map entry did not dispatch
    assert agent._context.history[-1].parts[0].text == "rejected — wave is no longer available"


def test_go_to_point_outside_the_active_skill_set_is_rejected(agent_factory):
    # navigate_to_position is installed (registry) but not active for the
    # directive: a hallucinated go_to_point_in_view call must not drive the
    # base by resolving through the full registry.
    from brain_client.skills.registry import SkillRegistry

    agent, state = agent_factory()
    state.registry = SkillRegistry.from_metadata([NAV_SKILL])
    started = []
    agent._runner.start_task = lambda *a, **k: started.append(a)
    answers(agent, Replay([call_reply("go_to_point_in_view", {"y": 800, "x": 500})]))
    run_turn(agent)

    assert started == []
    assert agent._context.history[-1].parts[0].text == "rejected — navigate_to_position is not available"


def test_go_to_point_rejects_out_of_range_coordinates(agent_factory):
    # Out-of-range pixels still produce finite tan() rays that ground a wrong
    # goal; they must bounce back to the model instead of driving the robot.
    from brain_client.skills.registry import SkillRegistry

    agent, state = agent_factory()
    state.registry = SkillRegistry.from_metadata([NAV_SKILL])
    agent._roster.active_skill_ids = lambda: [NAV_SKILL["id"]]
    started = []
    agent._runner.start_task = lambda *a, **k: started.append(a)
    answers(agent, Replay([call_reply("go_to_point_in_view", {"y": 2000, "x": 500})]))
    run_turn(agent)

    assert started == []
    outcome = agent._context.history[-1].parts[0].text
    assert outcome == "rejected — y and x must be within 0-1000 image coordinates"


def test_chat_failure_after_commit_still_answers_the_models_calls(agent_factory, monkeypatch):
    # emit_thoughts raising after absorb() must not leave the tool call
    # unanswered in history — that would poison every later request.
    agent, state = agent_factory()
    no_pause(agent, monkeypatch)

    def explode(*a, **k):
        raise RuntimeError("publisher torn down")

    agent._chat.emit_thoughts = explode
    answers(agent, Replay([reply(Thought("hmm"), ToolCall("c1", WAIT, {}), finish=Finish.TOOL_CALLS)]))
    run_turn(agent)

    assert agent._context.history[-1].parts[0].name == WAIT


def test_tool_failure_becomes_an_outcome_instead_of_failing_the_committed_turn(agent_factory):
    agent, state = agent_factory()
    state.primitive_running = RunningSkill(primitive_name="wave", skill_id="local/wave")
    # The runner stub has no attributes, so stopping the skill raises.
    answers(agent, Replay([call_reply(STOP_SKILL)]))
    agent.on_user_message("stop that")
    run_turn(agent)

    assert agent._events == []  # the turn committed; nothing is rerun
    assert agent._error_streak == 0
    assert agent._context.history[-1].parts[0].text.startswith("failed —")


def test_turn_finishing_after_deactivation_is_dropped_entirely(agent_factory):
    agent, state = agent_factory()

    def deactivate(request: Request):  # deactivation lands while the turn is thinking
        state.is_brain_active = False
        return [reply(Text("stale"))]

    answers(agent, Replay(script=deactivate))
    run_turn(agent)

    assert agent._context.history == ()  # no stale observation survives into the next activation
    assert agent._turn_in_flight is False


@pytest.mark.parametrize("fails", [False, True])
def test_thinking_status_covers_request_and_clears_before_backoff(agent_factory, monkeypatch, fails):
    statuses = []
    agent, _ = agent_factory(on_thinking_changed=lambda: statuses.append(agent.thinking))
    no_pause(agent, monkeypatch)

    def script(request: Request):
        assert statuses == [True]  # visible before any response or thought text
        if fails:
            raise RuntimeError("offline")
        return [reply(Text("done"))]

    answers(agent, Replay(script=script))
    assert not agent.thinking
    run_turn(agent)
    assert statuses == [True, False]
    assert not agent.thinking


def test_stop_cancels_a_turn_mid_think_and_absorbs_nothing(agent_factory):
    statuses = []
    agent, state = agent_factory(on_thinking_changed=lambda: statuses.append(agent.thinking))
    thinking, release = threading.Event(), threading.Event()

    def hang(request: Request):
        thinking.set()
        release.wait(timeout=5)
        return [reply(Text("stale"))]

    answers(agent, Replay(script=hang))
    agent.on_user_message("hi")
    agent.start()
    assert thinking.wait(timeout=5)
    assert statuses == [True]

    agent.stop()  # synchronous: the turn has unwound at its await when this returns
    release.set()  # the orphaned HTTP call finishes on its worker thread...
    time.sleep(0.2)
    assert statuses == [True, False]
    assert agent._context.history == ()  # ...and its response is dropped
    assert not agent._runtime.running


def test_reset_mid_turn_restarts_the_loop_with_empty_history(agent_factory):
    agent, state = agent_factory()
    thinking, release = threading.Event(), threading.Event()

    def hang(request: Request):
        thinking.set()
        release.wait(timeout=5)
        return [reply(Text("stale"))]

    answers(agent, Replay(script=hang))
    agent.start()
    assert thinking.wait(timeout=5)
    thinking.clear()

    agent.reset()  # cancels the old turn, clears history, respawns the loop
    assert agent._runtime.running
    assert agent._context.history == ()
    assert thinking.wait(timeout=5)  # the restarted loop is already thinking again
    agent.stop()
    release.set()


def test_user_speech_preempts_a_thinking_housekeeping_turn(agent_factory):
    traces = []
    agent, state = agent_factory(trace=lambda payload: traces.append(json.loads(payload)))
    thinking, release = threading.Event(), threading.Event()
    turn_inputs = []

    def script(request: Request):
        turn_inputs.append(request.messages[-1].text())
        thinking.set()
        if len(turn_inputs) == 1:
            release.wait(timeout=5)  # the heartbeat turn hangs mid-think
        return [call_reply(WAIT)]

    answers(agent, Replay(script=script))
    agent.start()  # empty queue: the first turn is a preemptible heartbeat
    assert thinking.wait(timeout=5)
    thinking.clear()

    agent.on_user_message("hello")
    # The rerun starts thinking without waiting for the hung heartbeat call.
    assert thinking.wait(timeout=5)
    assert 'The user says: "hello"' in turn_inputs[1]
    assert "turn_preempted" in [t["ev"] for t in traces]

    release.set()  # the orphaned heartbeat response unblocks and is dropped
    deadline = time.time() + 5
    while agent._events and time.time() < deadline:
        time.sleep(0.02)
    assert agent._events == []  # the rerun committed the user's event
    agent.stop()
    # The aborted heartbeat exchange never entered history: the first stored
    # user turn is the rerun's, which carries the user's message.
    first_user_turn = next(m for m in agent._context.history if m.role == Role.USER)
    assert any("hello" in text for text in first_user_turn.texts())


def test_a_second_message_reruns_an_unspoken_user_turn(agent_factory):
    agent, state = agent_factory()
    thinking, release = threading.Event(), threading.Event()
    turn_inputs = []

    def script(request: Request):
        turn_inputs.append(request.messages[-1].text())
        thinking.set()
        if len(turn_inputs) == 1:
            release.wait(timeout=5)
        return [call_reply(WAIT)]

    answers(agent, Replay(script=script))
    agent.on_user_message("first request")
    agent.start()
    assert thinking.wait(timeout=5)
    thinking.clear()

    agent.on_user_message("actually, cancel that")  # lands before any speech
    assert thinking.wait(timeout=5)  # rerun starts without waiting for the first call
    assert 'The user says: "first request"' in turn_inputs[1]
    assert 'The user says: "actually, cancel that"' in turn_inputs[1]
    release.set()
    agent.stop()


def test_a_turn_that_started_speaking_finishes(agent_factory):
    traces = []
    agent, state = agent_factory(trace=lambda payload: traces.append(json.loads(payload)))
    thinking, release = threading.Event(), threading.Event()
    turn_inputs = []

    def script(request: Request):
        turn_inputs.append(request.messages[-1].text())
        if len(turn_inputs) > 1:
            return [call_reply(WAIT)]

        def events():
            yield TextDelta("One moment. ")  # spoken: the turn now holds the floor
            thinking.set()
            release.wait(timeout=5)
            yield TextDelta("There.")
            yield reply(Text("One moment. There."))

        return events()

    answers(agent, Replay(script=script))
    agent.on_user_message("first")
    agent.start()
    assert thinking.wait(timeout=5)  # the first sentence has streamed

    agent.on_user_message("second")
    time.sleep(0.3)  # a wrong implementation would abandon in this window
    assert not any(t["ev"] == "turn_preempted" for t in traces)
    release.set()

    deadline = time.time() + 5
    while len(turn_inputs) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert 'The user says: "second"' in turn_inputs[1]
    assert ("One moment.", True) in agent._chat.spoken
    agent.stop()


def test_nonstop_speech_cannot_starve_the_loop(agent_factory):
    traces = []
    agent, state = agent_factory(trace=lambda payload: traces.append(json.loads(payload)))
    thinking, release = threading.Event(), threading.Event()
    calls = []

    def script(request: Request):
        calls.append(request.messages[-1].text())
        thinking.set()
        release.wait(timeout=5)
        return [call_reply(WAIT)]

    answers(agent, Replay(script=script))
    agent.on_user_message("one")
    agent.start()
    assert thinking.wait(timeout=5)
    thinking.clear()
    agent.on_user_message("two")  # abandons run 1
    assert thinking.wait(timeout=5)
    thinking.clear()
    agent.on_user_message("three")  # abandons run 2
    assert thinking.wait(timeout=5)
    thinking.clear()

    agent.on_user_message("four")  # cap reached: run 3 completes regardless
    time.sleep(0.3)
    assert sum(t["ev"] == "turn_preempted" for t in traces) == 2
    release.set()

    deadline = time.time() + 5
    while len(calls) < 4 and time.time() < deadline:
        time.sleep(0.02)
    assert all(f'"{word}"' in calls[2] for word in ("one", "two", "three"))  # the capped run carried it all
    assert '"four"' in calls[3]
    agent.stop()


def test_speech_streamer_speaks_sentence_by_sentence():
    spoken = []
    chat = SimpleNamespace(
        speak=lambda text, replace_pending=False, reply_id=None: spoken.append((text, replace_pending, reply_id))
    )
    streamer = SpeechStreamer(chat)
    streamer.feed("I see a ball. It is ")
    streamer.feed("red! And")
    streamer.feed(" close.")
    streamer.flush()
    # First sentence supersedes any stale queue; the rest append in order.
    assert [(text, replace) for text, replace, _ in spoken] == [
        ("I see a ball.", True),
        ("It is red!", False),
        ("And close.", False),
    ]
    assert spoken[0][2] is not None
    assert {reply_id for _, _, reply_id in spoken} == {spoken[0][2]}


def test_speech_streamer_mutes_leaked_tool_narration_and_skips_noise():
    spoken = []
    chat = SimpleNamespace(speak=lambda text, replace_pending=False, reply_id=None: spoken.append(text))
    streamer = SpeechStreamer(chat)
    streamer.feed("Done. Calling tool default_api. This must not be spoken.")
    streamer.flush()
    assert spoken == ["Done."]

    silent = SpeechStreamer(chat)
    silent.feed("--- ")
    silent.flush()
    assert spoken == ["Done."] and silent.spoke is False


def test_speech_streamer_mute_drops_everything_not_yet_spoken():
    spoken = []
    chat = SimpleNamespace(speak=lambda text, replace_pending=False, reply_id=None: spoken.append(text))
    streamer = SpeechStreamer(chat)
    streamer.feed("First. Sec")
    streamer.mute()
    streamer.feed("ond. Third.")
    streamer.flush()
    assert spoken == ["First."]


def test_speech_streamer_try_abandon_is_atomic_with_spoke():
    # The loop's preemption check: abandon must succeed only while nothing has
    # been voiced, and a successful abandon must silence the rest of the reply.
    spoken = []
    chat = SimpleNamespace(speak=lambda text, replace_pending=False, reply_id=None: spoken.append(text))
    unspoken = SpeechStreamer(chat)
    unspoken.feed("Not yet a full sentence")
    assert unspoken.try_abandon() is True
    unspoken.feed(". The rest.")
    unspoken.flush()
    assert spoken == []

    talking = SpeechStreamer(chat)
    talking.feed("Already said. ")
    assert talking.try_abandon() is False  # holds the floor: finish the reply
    talking.flush()
    assert spoken == ["Already said."]


def test_user_turn_streams_sentences_to_tts_before_commit(agent_factory):
    agent, state = agent_factory()
    answers(
        agent,
        Replay([TextDelta("First sentence. "), TextDelta("Second."), reply(Text("First sentence. Second."))]),
    )
    agent.on_user_message("talk to me")
    run_turn(agent)
    assert agent._chat.spoken == [("First sentence.", True), ("Second.", False)]


def test_housekeeping_turn_speech_streams_like_any_other(agent_factory):
    agent, state = agent_factory()
    answers(agent, Replay([TextDelta("One. "), TextDelta("Two."), reply(Text("One. Two."))]))
    run_turn(agent)  # the started-speaking guard protects it, so it may stream too
    assert agent._chat.spoken == [("One.", True), ("Two.", False)]


def test_suppressed_reply_tells_the_model_it_went_unspoken(agent_factory):
    # A reply that never started speaking is suppressed when a newer user
    # message is pending — but history already holds it verbatim, so the model
    # must learn it went unspoken or "never repeat yourself" buries the answer.
    agent, state = agent_factory()

    def script(request: Request):
        # The user speaks again while the model is thinking.
        agent._events.append(Event('The user says: "wait, actually…"', kind=EventKind.USER))
        return [TextDelta("Here is my answer."), reply(Text("Here is my answer."))]

    answers(agent, Replay(script=script))
    agent.on_user_message("question?")
    run_turn(agent)

    assert agent._chat.spoken == []  # muted: nothing reached TTS
    texts = [e.text for e in agent._events]
    assert texts[0] == 'The user says: "wait, actually…"'
    assert "was not spoken" in texts[1]


def test_running_skill_guidance_reads_registry_metadata(agent_factory):
    # Regression: registry.primitives holds plain metadata dicts (not stub
    # objects) since the SkillRegistry slimming — the supervision turn must
    # read guidance with dict access, not a method call. The guidance rides
    # the system instruction, never the per-turn observation text (stored per
    # turn it would be re-billed in every history entry).
    from brain_client.skills.registry import SkillRegistry

    agent, state = agent_factory()
    state.registry = SkillRegistry.from_metadata(
        [{**WAVE_SKILL, "type": "code", "guidelines_when_running": "  do not block the arm  "}]
    )
    state.primitive_running = RunningSkill(primitive_name="wave", skill_id="local/wave", primitive_id="p1")
    replay = answers(agent, Replay([reply(Text("ok"))]))
    run_turn(agent)

    request = replay.last
    assert "do not block the arm" in request.system
    assert "do not block the arm" not in request.messages[-1].text()


def test_a_turn_bug_backs_off_instead_of_killing_the_loop(agent_factory, monkeypatch):
    # Regression: an unexpected exception anywhere in the turn (not just the
    # network call) must take the backoff path, not unwind the whole loop —
    # a crashed brain stays dead until a human stops and starts it.
    agent, state = agent_factory()
    answers(agent, Replay([reply(Text("hi"))]))
    no_pause(agent, monkeypatch)

    def broken_tools():
        raise AttributeError("'dict' object has no attribute 'guidelines_when_running'")

    monkeypatch.setattr(agent, "_build_tools", broken_tools)
    run_turn(agent)  # raises nothing: the failure is absorbed
    assert agent._error_streak == 1
    assert agent._context.history == ()


def test_trace_reports_the_turn_lifecycle(agent_factory, monkeypatch):
    traces = []
    agent, state = agent_factory(trace=lambda payload: traces.append(json.loads(payload)))
    no_pause(agent, monkeypatch)

    outcomes = iter([RuntimeError("boom"), call_reply(WAIT)])

    def script(request: Request):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return [outcome]

    answers(agent, Replay(script=script))
    agent.on_user_message("hello")
    run_turn(agent)  # fails...
    run_turn(agent)  # ...retries the same still-queued event and commits
    state.is_brain_active = False
    agent._snapshot()  # the telemetry heartbeat reports even while inactive

    events = [t["ev"] for t in traces]
    # turn_request fires from the generate path itself, before the transport
    # can fail — so even the erroring turn reports the request it tried to send.
    assert events == [
        "event",
        "turn_start",
        "turn_request",
        "turn_error",
        "turn_start",
        "turn_request",
        "turn_end",
        "snapshot",
    ]
    assert traces[0]["kind"] == "user"
    assert traces[3]["streak"] == 1 and traces[3]["backoff"] == 5.0
    assert traces[5]["body"]["contents"][-1]["role"] == "user"
    assert traces[6]["calls"] == [{"name": "wait", "args": {}, "outcome": "ok"}]
    snapshot = traces[7]
    # History: the user turn, the model turn, and the wait call's tool result.
    assert snapshot["active"] is False and snapshot["backend"] == "direct" and snapshot["history"] == 3
    assert snapshot["interval"] == 3.0

    # The heartbeat follows the current agent and skill state, including unset overrides.
    for intervals, expected in [
        ((0.01, None), (0.01, 5.0)),
        ((None, 0.02), (3.0, 0.02)),
        (None, (3.0, 5.0)),
    ]:
        state.current_directive = SimpleNamespace(_turn_intervals=intervals) if intervals else None
        for running, interval in zip((None, RunningSkill("wave", "innate-os/wave")), expected, strict=True):
            state.primitive_running = running
            agent._snapshot()
            assert traces[-1]["interval"] == interval


# ---------- skill events ----------


def test_skill_completion_event_carries_the_result_image(agent_factory):
    agent, _ = agent_factory()
    agent.on_skill_event("completed", "inspect_shelf", "found the mug", image=JPEG)
    (event,) = agent._events
    assert event.image == JPEG and "found the mug" in event.text


# ---------- prompt ----------


def test_system_prompt_embeds_directive_and_defaults_when_empty():
    assert "Patrol the house" in build_system_prompt("Patrol the house")
    assert "helpful home robot" in build_system_prompt(None)
    assert "helpful home robot" in build_system_prompt("   ")


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
