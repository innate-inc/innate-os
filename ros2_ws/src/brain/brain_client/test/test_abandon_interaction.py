# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Real skill + provider dispatch integration; provider choices are scripted, not ASR/model benchmarks."""

import json
from pathlib import Path

import pytest
import test_local_brain
from test_local_brain import call_part, model_response, run_turn
from test_openai_context import call_item, completed, context

from brain_client.core.state import RunningSkill
from brain_client.skills.registry import SkillRegistry

agent_factory = test_local_brain.agent_factory
ABANDON = "abandon_interaction"
SEARCH = "find_next_person"


@pytest.fixture
def scenario(agent_factory, monkeypatch, tmp_path):
    pytest.importorskip("rclpy")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[5] / "workspace"))
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    from innate_agents.household_orders_agent import HouseholdOrdersAgent
    from innate_skills.abandon_interaction import AbandonInteraction
    from innate_skills.mission_notes import MissionNotes
    from innate_skills.mission_run import start_run

    start_run("household_orders_agent")
    notes = MissionNotes(None)
    notes.execute("set", "resident-001", '{"name":"Sam","confirmed_order":"salad; no onions"}')
    agent, state = agent_factory()
    state.current_directive = HouseholdOrdersAgent()
    state.registry = SkillRegistry.from_metadata(
        [{"id": "innate-os/" + name, "name": name, "inputs": {}} for name in [ABANDON, SEARCH, "mission_notes"]]
    )
    agent._roster.active_skill_ids = lambda: list(state.registry.primitives)
    agent.on_skill_event("completed", "mission_notes", 'NOTE_MISSING {"key":"resident-002"}')
    starts = []

    def start(skill_id, task_id, inputs):
        starts.append((skill_id, inputs))
        state.primitive_running = RunningSkill(skill_id.rsplit("/", 1)[-1], skill_id)

    agent._runner.start_task = start
    return agent, state, starts, AbandonInteraction(None), notes


def install_transport(agent, provider, decide):
    requests = []

    def transport(model, body):
        requests.append(body)
        calls = decide(len(requests), body)
        if provider == "openai":
            yield completed(*(call_item(str(i), name, json.dumps(args)) for i, (name, args) in enumerate(calls)))
        else:
            yield model_response(*(call_part(name, args, str(i)) for i, (name, args) in enumerate(calls)))

    if provider == "openai":
        agent._context = context(transport)
    else:
        agent._context._transport = transport
    return requests


@pytest.mark.parametrize("provider", ["gemini", "openai"])
def test_completed_abandon_releases_only_guard_and_next_turn_search(scenario, provider):
    agent, state, starts, skill, notes = scenario
    saved = notes.execute("list").data
    guard = agent._interaction_guard_started_at
    requests = install_transport(
        agent, provider,
        lambda turn, body: [(ABANDON, {"reason": "visual_target_unavailable"}), (SEARCH, {})]
        if turn == 1 else [(SEARCH, {})],
    )
    run_turn(agent)
    assert [x[0] for x in starts] == ["innate-os/" + ABANDON]
    assert agent._interaction_guard_started_at == guard  # starting the skill is not success
    result = skill.execute(**starts[0][1])
    state.primitive_running = None
    agent.on_skill_event("completed", ABANDON, result.message)
    assert agent._interaction_guard_started_at is None
    assert notes.execute("list").data == saved
    run_turn(agent)
    assert [x[0] for x in starts] == ["innate-os/" + ABANDON, "innate-os/" + SEARCH]
    # Both histories retain outcomes for the started and rejected calls. The
    # second request gets the successful completion event and fresh search schema.
    if provider == "openai":
        outcomes = [x for x in requests[1]["input"] if x.get("type") == "function_call_output"]
        assert {x['call_id'] for x in outcomes} == {"0", "1"}
    else:
        outcomes = [p['functionResponse'] for x in requests[1]['contents'] for p in x['parts'] if 'functionResponse' in p]
        assert {x['id'] for x in outcomes} == {"0", "1"}
    assert "rejected" in json.dumps(outcomes)
    assert "INTERACTION_ABANDONED" in json.dumps(requests[1])


@pytest.mark.parametrize("block", ["running", "manual", "speech", "pending_reply", "operator_stop", "stale_roster"])
def test_inflight_abandon_obeys_existing_dispatch_fences(scenario, block):
    agent, state, starts, _, _ = scenario
    guard = agent._interaction_guard_started_at

    def decide(turn, body):
        if block in {"running", "manual"}:
            state.primitive_running = RunningSkill("other", "innate-os/other", manual=block == "manual")
        elif block in {"speech", "pending_reply"}:
            token = agent.begin_incoming_speech()
            assert token is not None
            if block == "pending_reply":
                agent.finish_incoming_speech(token, "That's correct. Thank you.")
        elif block == "operator_stop":
            agent._request_stopped = True
        else:
            agent._roster.active_skill_ids = lambda: ["innate-os/" + SEARCH]
        return [(ABANDON, {"reason": "visual_target_unavailable"}), (SEARCH, {})]

    install_transport(agent, "gemini", decide)
    run_turn(agent)
    assert not starts
    assert agent._interaction_guard_started_at == guard
    assert not any("INTERACTION_ABANDONED" in event.text for event in agent._events)


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_unsuccessful_or_cancelled_skill_cannot_release_guard(scenario, status):
    agent, _, _, skill, _ = scenario
    guard = agent._interaction_guard_started_at
    result = skill.execute("visual_target_unavailable")
    agent.on_skill_event(status, ABANDON, result.message)
    assert agent._interaction_guard_started_at == guard


def test_invalid_or_cancelled_execution_emits_no_success(scenario, monkeypatch):
    from innate import SkillCancelled, SkillFailed

    _, _, _, skill, _ = scenario
    with pytest.raises(SkillFailed):
        skill.execute("skip_order")
    def cancelled():
        raise SkillCancelled()
    monkeypatch.setattr(skill, "check_cancelled", cancelled)
    with pytest.raises(SkillCancelled):
        skill.execute("visual_target_unavailable")


@pytest.mark.parametrize("provider", ["gemini", "openai"])
def test_late_confirmation_fences_departure_and_preserves_notes(scenario, provider):
    agent, _, starts, _, notes = scenario
    before = notes.execute("list").data

    def decide(turn, body):
        if turn == 1:
            token = agent.begin_incoming_speech()
            agent.finish_incoming_speech(token, "That's correct. Thank you.")
            return [(ABANDON, {"reason": "visual_target_unavailable"}), (SEARCH, {})]
        assert "That's correct. Thank you." in json.dumps(body)
        return [("wait", {})]  # speaker association is unresolved, never guess a note

    install_transport(agent, provider, decide)
    run_turn(agent)
    assert not starts
    assert any("That's correct" in event.text for event in agent._events)
    run_turn(agent)
    assert not starts and notes.execute("list").data == before
    # This proves pending input is preserved/fences stale departure. Fresh
    # speaker attribution and abandoning an association remain prompt guidance.


def test_only_participating_directive_configures_abandon_release(scenario):
    from innate_agents.basic_agent import BasicAgent
    from innate_skills.abandon_interaction import AbandonInteraction

    agent, state, _, skill, _ = scenario
    assert AbandonInteraction in state.current_directive.get_skills()
    assert AbandonInteraction not in BasicAgent().get_skills()
    state.current_directive = BasicAgent()
    guard = agent._interaction_guard_started_at
    agent.on_skill_event("completed", ABANDON, skill.execute("visual_target_unavailable").message)
    assert agent._interaction_guard_started_at == guard
