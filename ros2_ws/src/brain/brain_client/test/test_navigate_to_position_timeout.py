# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pins the cancellable, terminal-safe Nav2 action controller."""

from __future__ import annotations

import inspect
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPO_ROOT / "workspace"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ros_skill_test_stubs import install_if_ros_is_missing  # noqa: E402

install_if_ros_is_missing()

from action_msgs.msg import GoalStatus  # noqa: E402
from geometry_msgs.msg import PoseStamped  # noqa: E402
from innate_skills import navigate_to_position as navigate_module  # noqa: E402
from innate_skills.navigate_to_position import Nav2Controller, NavigateToPosition  # noqa: E402

from innate import SkillCancelled, SkillFailed  # noqa: E402


class _Future:
    def __init__(self, result=None, *, done: bool = False):
        self._event = threading.Event()
        self._result = result
        self._callbacks = []
        self._lock = threading.Lock()
        if done:
            self._event.set()

    def done(self):
        return self._event.is_set()

    def result(self):
        assert self.done(), "future result read before completion"
        return self._result

    def add_done_callback(self, callback):
        with self._lock:
            if self.done():
                run_now = True
            else:
                self._callbacks.append(callback)
                run_now = False
        if run_now:
            callback(self)

    def set_result(self, result):
        with self._lock:
            self._result = result
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)


def _result(status):
    return SimpleNamespace(status=status, result=SimpleNamespace())


class _GoalHandle:
    def __init__(self, *, accepted=True, result_future=None, terminal_on_cancel=False):
        self.accepted = accepted
        self.result_future = result_future or _Future()
        self.terminal_on_cancel = terminal_on_cancel
        self.cancel_calls = 0
        self.cancel_called = threading.Event()

    def get_result_async(self):
        return self.result_future

    def cancel_goal_async(self):
        self.cancel_calls += 1
        self.cancel_called.set()
        if self.terminal_on_cancel and not self.result_future.done():
            self.result_future.set_result(_result(GoalStatus.STATUS_CANCELED))
        return _Future(SimpleNamespace(goals_canceling=[object()]), done=True)


class _ActionClient:
    def __init__(self, send_future, *, ready=True):
        self.ready = ready
        self.send_future = send_future
        self.sent_goals = []
        self.sent = threading.Event()

    def server_is_ready(self):
        return self.ready

    def send_goal_async(self, goal, feedback_callback=None):
        self.sent_goals.append((goal, feedback_callback))
        self.sent.set()
        return self.send_future


class _Clock:
    def __init__(self):
        self.value = 0.0
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            return self.value

    def advance(self, seconds):
        with self._lock:
            self.value += seconds


class _Node:
    def get_clock(self):
        return SimpleNamespace(now=lambda: SimpleNamespace(to_msg=lambda: PoseStamped().header.stamp))


class _SkillHarness:
    def __init__(self, clock):
        self.node = _Node()
        self.odom = None
        self._clock = clock
        self._cancelled = threading.Event()
        self._cancel_hooks = []
        self.feedback_messages = []

    def sleep(self, seconds):
        self._clock.advance(seconds)
        if self._cancelled.wait(0.001):
            raise SkillCancelled("cancelled")

    def check_cancelled(self):
        if self._cancelled.is_set():
            raise SkillCancelled("cancelled")

    def on_cancel(self, callback):
        self._cancel_hooks.append(callback)

    def cancel(self):
        self._cancelled.set()
        for callback in list(self._cancel_hooks):
            callback()

    def feedback(self, message):
        self.feedback_messages.append(message)


class _Logger:
    def __init__(self):
        self.errors = []

    def info(self, *_args):
        pass

    def warning(self, *_args):
        pass

    def error(self, message):
        self.errors.append(message)


def _controller(client, clock):
    skill = _SkillHarness(clock)
    controller = Nav2Controller.__new__(Nav2Controller)
    controller.skill = skill
    controller.logger = _Logger()
    controller._client = client
    controller._commanded_goal_pub = SimpleNamespace(messages=[], publish=lambda msg: None)
    controller._goal_lock = threading.Lock()
    controller._active_goal_handle = None
    controller._active_result_future = None
    controller._cancel_future = None
    controller._latest_feedback = None
    skill.on_cancel(controller._request_active_cancel)
    return controller, skill


def _run_in_thread(call):
    outcome = []

    def target():
        try:
            outcome.append(call())
        except BaseException as exc:  # SkillCancelled deliberately extends BaseException.
            outcome.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    return worker, outcome


def test_timeout_cancels_and_waits_for_terminal_result(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    goal_handle = _GoalHandle(terminal_on_cancel=True)
    client = _ActionClient(_Future(goal_handle, done=True))
    controller, _skill = _controller(client, clock)

    with pytest.raises(SkillFailed, match="timed out"):
        controller.go_to_position(1.0, 2.0, 0.0, False, timeout_s=0.25)

    assert goal_handle.cancel_calls == 1
    assert goal_handle.result_future.done()


def test_cancel_ack_is_not_mistaken_for_terminal_result(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    goal_handle = _GoalHandle()
    client = _ActionClient(_Future(goal_handle, done=True))
    controller, skill = _controller(client, clock)

    worker, outcome = _run_in_thread(lambda: controller.go_to_position(1.0, 2.0, 0.0, False))
    assert client.sent.wait(1.0)
    skill.cancel()
    assert goal_handle.cancel_called.wait(1.0)

    # cancel_goal_async already returned an acknowledgement, but the outer
    # result remains pending until the router confirms internal Nav2 terminal.
    worker.join(0.05)
    assert worker.is_alive()
    goal_handle.result_future.set_result(_result(GoalStatus.STATUS_CANCELED))
    worker.join(1.0)

    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], SkillCancelled)
    assert goal_handle.cancel_calls == 1


def test_cancel_during_pending_dispatch_settles_then_cancels(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    send_future = _Future()
    goal_handle = _GoalHandle()
    client = _ActionClient(send_future)
    controller, skill = _controller(client, clock)

    worker, outcome = _run_in_thread(lambda: controller.go_to_position(1.0, 2.0, 0.0, False))
    assert client.sent.wait(1.0)
    skill.cancel()
    worker.join(0.05)
    assert worker.is_alive(), "controller returned before the pending goal response settled"

    send_future.set_result(goal_handle)
    assert goal_handle.cancel_called.wait(1.0)
    worker.join(0.05)
    assert worker.is_alive(), "controller returned after cancel acknowledgement but before terminal result"

    goal_handle.result_future.set_result(_result(GoalStatus.STATUS_CANCELED))
    worker.join(1.0)
    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], SkillCancelled)
    assert goal_handle.cancel_calls == 1


def test_rejected_goal_fails_without_consulting_prior_result(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    rejected = _GoalHandle(accepted=False, result_future=_Future(_result(GoalStatus.STATUS_SUCCEEDED), done=True))
    client = _ActionClient(_Future(rejected, done=True))
    controller, _skill = _controller(client, clock)

    with pytest.raises(SkillFailed, match="rejected"):
        controller.go_to_position(1.0, 2.0, 0.0, False)

    assert rejected.cancel_calls == 0


def test_safety_change_before_dispatch_sends_no_goal(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    client = _ActionClient(_Future())
    controller, _skill = _controller(client, clock)

    def unsafe():
        raise SkillCancelled("mode changed")

    with pytest.raises(SkillCancelled, match="mode changed"):
        controller.go_to_position(1.0, 2.0, 0.0, False, safety_check=unsafe)

    assert client.sent_goals == []


def test_safety_change_after_dispatch_cancels_and_waits_terminal(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    goal_handle = _GoalHandle(terminal_on_cancel=True)
    client = _ActionClient(_Future(goal_handle, done=True))
    controller, _skill = _controller(client, clock)
    checks = 0

    def unsafe_after_dispatch():
        nonlocal checks
        checks += 1
        if checks >= 5:
            raise SkillCancelled("joystick takeover")

    with pytest.raises(SkillCancelled, match="joystick takeover"):
        controller.go_to_position(1.0, 2.0, 0.0, False, safety_check=unsafe_after_dispatch)

    assert client.sent_goals
    assert goal_handle.cancel_calls == 1
    assert goal_handle.result_future.done()


def test_waiting_for_action_server_remains_cancellable(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    client = _ActionClient(_Future(), ready=False)
    controller, skill = _controller(client, clock)

    worker, outcome = _run_in_thread(lambda: controller.go_to_position(1.0, 2.0, 0.0, False))
    skill.cancel()
    worker.join(1.0)

    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], SkillCancelled)
    assert client.sent_goals == []


def test_success_uses_map_selector_and_returns(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    goal_handle = _GoalHandle(result_future=_Future(_result(GoalStatus.STATUS_SUCCEEDED), done=True))
    client = _ActionClient(_Future(goal_handle, done=True))
    controller, _skill = _controller(client, clock)

    controller.go_to_position(1.0, 2.0, 0.25, False)

    assert len(client.sent_goals) == 1
    goal, _feedback_callback = client.sent_goals[0]
    assert goal.behavior_tree == "navigation"
    assert goal.pose.header.frame_id == "map"
    assert goal_handle.cancel_calls == 0


def test_external_navigation_cancel_stops_skill_instead_of_retrying(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(navigate_module.time, "monotonic", clock.now)
    goal_handle = _GoalHandle(result_future=_Future(_result(GoalStatus.STATUS_CANCELED), done=True))
    client = _ActionClient(_Future(goal_handle, done=True))
    controller, _skill = _controller(client, clock)

    with pytest.raises(SkillCancelled, match="navigation was canceled"):
        controller.go_to_position(1.0, 2.0, 0.25, False)

    assert goal_handle.cancel_calls == 0


def test_public_skill_schema_no_longer_exposes_timeout_string_union():
    assert "timeout_s" not in inspect.signature(NavigateToPosition.execute).parameters
