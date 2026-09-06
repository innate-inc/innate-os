# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for PrimitiveRunner's release paths: deactivation, reset, the
late callbacks of a disowned goal, and the slot claim's collision handling.
Exercised on a bare instance (no ROS node or action server) — these paths only
touch ``_state``, ``_goal_handle``, ``_generation``, and the slot lock.
"""

import threading
from concurrent.futures import Future
from types import SimpleNamespace

from brain_client.core.state import BrainState, RunningSkill
from brain_client.skills.runner import PrimitiveRunner


def make_runner(running: RunningSkill | None, goal_handle=None):
    runner = PrimitiveRunner.__new__(PrimitiveRunner)
    state = BrainState()
    state.primitive_running = running
    runner._state = state
    runner._goal_handle = goal_handle
    runner._generation = 0
    runner._slot_lock = threading.Lock()
    runner._input_interrupted = None
    runner._logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    runner._stop_robot = lambda: None
    runner._on_task_finished = lambda: None
    runner._chat = SimpleNamespace(publish_task_status=lambda **kwargs: None)
    runner.on_event = lambda status, name, detail=None, image=None: None
    return runner, state


def make_handle(cancelled: list, accepted: bool = True):
    return SimpleNamespace(accepted=accepted, cancel_goal_async=lambda: cancelled.append(True))


def as_future(value):
    return SimpleNamespace(result=lambda: value)


BRAIN_RUN = RunningSkill(primitive_name="wave", skill_id="local/wave", primitive_id="p1")
MANUAL_RUN = RunningSkill(primitive_name="wave", skill_id="local/wave", primitive_id="m1", manual=True)


def test_deactivation_preserves_manual_running_state():
    # A manual (webapp/CLI) run has no local goal handle and keeps running on
    # the skills server: deactivation must not erase the mirrored state, or a
    # reactivated brain would start a second skill alongside it.
    runner, state = make_runner(MANUAL_RUN)
    runner.interrupt_for_deactivation()
    assert state.primitive_running is not None
    assert state.primitive_running.manual is True


def test_reset_preserves_manual_running_state_and_leaves_the_robot_alone():
    stopped = []
    runner, state = make_runner(MANUAL_RUN)
    runner._stop_robot = lambda: stopped.append(True)
    runner.abort_running()
    assert state.primitive_running is not None
    assert stopped == []


def test_deactivation_cancels_and_clears_brain_owned_run():
    cancelled = []
    runner, state = make_runner(BRAIN_RUN, make_handle(cancelled))
    runner.interrupt_for_deactivation()
    assert cancelled == [True]
    assert runner._goal_handle is None
    assert state.primitive_running is None


def test_reset_cancels_the_goal_and_halts_the_robot():
    cancelled, stopped = [], []
    runner, state = make_runner(BRAIN_RUN, make_handle(cancelled))
    runner._stop_robot = lambda: stopped.append(True)
    runner.abort_running()
    assert cancelled == [True]
    assert stopped == [True]
    assert state.primitive_running is None


def test_disowned_pending_goal_is_cancelled_when_its_handle_arrives():
    # The agent loop sent the goal moments before deactivation: no handle
    # exists yet, so the release bumps the generation and the goal response
    # callback cancels the run on arrival instead of retaining it.
    runner, state = make_runner(BRAIN_RUN, goal_handle=None)
    sent_generation = runner._generation
    runner.interrupt_for_deactivation()
    assert state.primitive_running is None

    cancelled = []
    runner._on_goal_response(as_future(make_handle(cancelled)), sent_generation)
    assert cancelled == [True]
    assert runner._goal_handle is None


def test_stale_result_neither_clears_newer_state_nor_reports_to_the_brain():
    # Reset disowns the running goal; a manual run starts before the old
    # goal's terminal result lands. That result must not clear the newer run's
    # state or inject a false event into the fresh context.
    cancelled = []
    runner, state = make_runner(BRAIN_RUN, make_handle(cancelled))
    events = []
    runner.on_event = lambda status, name, detail=None: events.append(status)
    sent_generation = runner._generation
    runner.abort_running()
    state.primitive_running = MANUAL_RUN

    result = SimpleNamespace(
        result=SimpleNamespace(success=False, success_type="CANCELLED", skill_type="local/wave", message="")
    )
    runner._on_result(as_future(result), sent_generation)
    assert state.primitive_running == MANUAL_RUN
    assert events == []


def test_stale_feedback_is_dropped():
    runner, state = make_runner(BRAIN_RUN, goal_handle=None)
    forwarded = []
    runner.on_feedback = lambda name, feedback, image=None: forwarded.append(feedback)
    sent_generation = runner._generation
    runner.interrupt_for_deactivation()

    feedback = SimpleNamespace(feedback=SimpleNamespace(feedback="halfway there", image_b64=""))
    runner._on_feedback_msg(feedback, sent_generation)
    assert forwarded == []


def test_deactivation_with_nothing_running_is_a_no_op():
    runner, state = make_runner(None)
    runner.interrupt_for_deactivation()
    assert state.primitive_running is None
    assert runner._goal_handle is None


def test_brain_claim_loses_to_a_manual_run_that_took_the_slot_first():
    # The agent's _dispatch check and the runner's claim run on the loop
    # thread; a manual run can be mirrored from the executor thread in
    # between. The claim must find the slot taken and report failure instead
    # of clobbering the manual run and double-driving the robot.
    runner, state = make_runner(None)
    events, sent = [], []
    runner.on_event = lambda status, name, detail=None: events.append((status, detail))
    runner._send_goal = lambda *a: sent.append(a) or True

    runner.mirror_manual_event("running", primitive_name="wave", primitive_id="m1", skill_id="local/wave")
    runner.start_task("local/dance", "p1", {})

    assert state.primitive_running == MANUAL_RUN
    assert sent == []
    assert events and events[0][0] == "failed"


def test_manual_running_never_clobbers_an_existing_slot():
    runner, state = make_runner(BRAIN_RUN)
    runner.mirror_manual_event("running", primitive_name="dance", primitive_id="m1", skill_id="local/dance")
    assert state.primitive_running == BRAIN_RUN


def test_manual_terminal_only_clears_a_manual_occupant():
    # A late manual terminal event must not erase a brain-owned run that has
    # since taken the slot.
    runner, state = make_runner(BRAIN_RUN)
    runner.mirror_manual_event("completed", primitive_name="wave", primitive_id="m1", skill_id="local/wave")
    assert state.primitive_running == BRAIN_RUN

    runner2, state2 = make_runner(MANUAL_RUN)
    runner2.mirror_manual_event("interrupted", primitive_name="wave", primitive_id="m1", skill_id="local/wave")
    assert state2.primitive_running is None


def test_stale_manual_terminal_with_a_different_id_keeps_the_newer_manual_run():
    # A delayed terminal from an OLDER manual run must not clear the mirror of
    # a newer manual run that has since claimed the slot.
    runner, state = make_runner(None)
    runner.mirror_manual_event("running", primitive_name="wave", primitive_id="m2", skill_id="local/wave")
    runner.mirror_manual_event("completed", primitive_name="wave", primitive_id="m1", skill_id="local/wave")
    assert state.primitive_running is not None
    runner.mirror_manual_event("completed", primitive_name="wave", primitive_id="m2", skill_id="local/wave")
    assert state.primitive_running is None


def test_manual_terminal_without_ids_still_clears_the_mirror():
    # Publishers that omit primitive_id fall back to any-manual clearing —
    # requiring a match there would wedge the slot shut forever.
    runner, state = make_runner(None)
    runner.mirror_manual_event("running", primitive_name="wave", primitive_id=None, skill_id="local/wave")
    runner.mirror_manual_event("completed", primitive_name="wave", primitive_id=None, skill_id="local/wave")
    assert state.primitive_running is None


def test_generation_is_captured_at_claim_time_so_a_mid_send_disown_orphans_the_goal():
    # A reset can land while start_task is blocked in wait_for_server. The
    # goal's callbacks must carry the claim-time generation: the disown bumps
    # it, so the late acceptance cancels the goal instead of adopting it into
    # the fresh context.
    runner, state = make_runner(None)
    cancelled = []

    def send_goal(skill_id, inputs, generation):
        runner.interrupt_for_deactivation()  # the disown lands mid-send
        runner._on_goal_response(as_future(make_handle(cancelled)), generation)
        return True

    runner._send_goal = send_goal
    runner.start_task("local/wave", "p1", {})

    assert cancelled == [True]
    assert state.primitive_running is None
    assert runner._goal_handle is None


def test_input_cancels_pending_navigation_once_and_keeps_normal_cleanup():
    navigation = RunningSkill("navigate_to_position", "innate-os/navigate_to_position", "nav1")
    runner, state = make_runner(navigation)
    result_future = Future()
    cancellations, events, cleanup = [], [], []

    def cancel():
        cancellations.append(True)
        future = Future()
        future.set_result(SimpleNamespace(goals_canceling=[1]))
        return future

    handle = SimpleNamespace(accepted=True, cancel_goal_async=cancel, get_result_async=lambda: result_future)
    runner.on_event = lambda status, name, detail=None, image=None: events.append((status, detail))
    runner._stop_robot = lambda: cleanup.append("stop")
    runner._on_task_finished = lambda: cleanup.append("finished")
    runner.interrupt_for_input(frozenset({navigation.skill_id}))
    runner.interrupt_for_input(frozenset({navigation.skill_id}))  # overlapping utterance
    assert state.primitive_running is navigation and cancellations == []
    runner._on_goal_response(as_future(handle), runner._generation)
    assert cancellations == [True] and state.primitive_running is navigation
    result_future.set_result(
        SimpleNamespace(
            result=SimpleNamespace(
                success=False, success_type="cancelled", skill_type=navigation.skill_id, message="", image_b64=""
            )
        )
    )
    assert state.primitive_running is None and runner._goal_handle is None
    assert cleanup == ["stop", "finished"]
    assert events[0] == ("interrupted", "Paused for incoming speech; the navigation goal was cancelled.")


def test_input_never_cancels_manual_or_unrelated_skills_and_stop_disowns_pending_navigation():
    nav_id = "innate-os/navigate_to_position"
    manual = RunningSkill("navigate_to_position", nav_id, manual=True)
    for running in (manual, BRAIN_RUN):
        cancellations = []
        runner, state = make_runner(running, make_handle(cancellations))
        runner.interrupt_for_input(frozenset({nav_id}))
        assert cancellations == [] and state.primitive_running is running
    navigation = RunningSkill("navigate_to_position", nav_id)
    runner, state = make_runner(navigation)
    runner.interrupt_for_input(frozenset({nav_id}))
    old_generation = runner._generation
    runner.interrupt_for_deactivation()
    cancellations = []
    runner._on_goal_response(as_future(make_handle(cancellations)), old_generation)
    assert cancellations == [True]
    assert state.primitive_running is None and runner._input_interrupted is None
