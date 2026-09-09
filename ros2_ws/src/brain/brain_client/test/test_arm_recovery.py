# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for ArmRecovery's policy: what triggers a recovery, what never
does, the per-episode attempt cap, and the fix_error response handling.
Exercised on bare instances (no ROS node) like the runner tests.
"""

from types import SimpleNamespace

from brain_client.core.state import BrainState, RunningSkill
from brain_client.robot import arm_recovery
from brain_client.robot.arm_recovery import ArmRecovery

HW_ERROR = "Servo 3 hardware error: overload — present load is low, use 'Reboot arm'"


def status(is_ok: bool, error: str = "All servos nominal"):
    return SimpleNamespace(is_ok=is_ok, error=error)


def make_recovery(active=True, running=None, has_goal=False):
    r = ArmRecovery.__new__(ArmRecovery)
    r._logger = SimpleNamespace(info=lambda *a: None, warn=lambda *a: None, error=lambda *a: None)
    r._state = BrainState()
    r._state.is_brain_active = active
    r._state.primitive_running = running
    log = {"cancelled": [], "chat": [], "events": [], "spawned": []}
    r._runner = SimpleNamespace(
        has_active_goal=has_goal,
        cancel_active_goal=lambda on_done=None: log["cancelled"].append("goal"),
        cancel_external=lambda: (log["cancelled"].append("external"), True)[1],
    )
    r._chat = SimpleNamespace(emit_system=lambda t: log["chat"].append(t))
    r._brain = SimpleNamespace(add_event=lambda t, **k: log["events"].append(t))
    r._fix_client = SimpleNamespace(service_is_ready=lambda: False)
    r._in_flight = False
    r._attempts = 0
    r._last_attempt_at = 0.0
    return r, log


class ThreadRecorder:
    """Stands in for threading.Thread: records spawns, never runs the target."""

    spawned = []

    def __init__(self, target=None, args=(), daemon=None):
        ThreadRecorder.spawned.append((target, args))

    def start(self):
        pass


def test_hardware_error_triggers_one_recovery(monkeypatch):
    monkeypatch.setattr(arm_recovery.threading, "Thread", ThreadRecorder)
    ThreadRecorder.spawned = []
    r, _ = make_recovery()
    r._on_status(status(False, HW_ERROR))
    r._on_status(status(False, HW_ERROR))  # in flight: no second spawn
    assert len(ThreadRecorder.spawned) == 1
    assert r._attempts == 1


def test_transient_warnings_and_inactive_brain_never_trigger(monkeypatch):
    monkeypatch.setattr(arm_recovery.threading, "Thread", ThreadRecorder)
    ThreadRecorder.spawned = []
    r, _ = make_recovery()
    r._on_status(status(False, "Servo 2 high load (75.0%)"))
    r._on_status(status(False, "Servo 4 high temperature (70 C)"))
    r._on_status(status(False, "Health check error: port died"))
    inactive, _ = make_recovery(active=False)
    inactive._on_status(status(False, HW_ERROR))
    assert ThreadRecorder.spawned == []


def test_attempts_cap_and_rearm_on_healthy(monkeypatch):
    monkeypatch.setattr(arm_recovery.threading, "Thread", ThreadRecorder)
    ThreadRecorder.spawned = []
    r, _ = make_recovery()
    for _ in range(5):  # error persists across many status ticks
        r._in_flight = False  # each attempt "finished"
        r._last_attempt_at = 0.0  # and its retry gap elapsed
        r._on_status(status(False, HW_ERROR))
    assert len(ThreadRecorder.spawned) == 3  # capped per episode
    r._on_status(status(True))  # arm healthy: episode over
    r._on_status(status(False, HW_ERROR))
    assert len(ThreadRecorder.spawned) == 4  # a new trip recovers again


def test_recover_stops_the_running_skill_and_reports(monkeypatch):
    r, log = make_recovery(running=RunningSkill(primitive_name="pick_any_object", skill_id="local/pick"), has_goal=True)
    r._call_fix_error = lambda: (True, "rebooted servo(s) 3 and re-enabled their torque")
    r._recover(HW_ERROR, attempt=1)
    assert log["cancelled"] == ["goal"]
    assert any("rebooted servo(s) 3" in e for e in log["events"])
    assert any("✅" in c for c in log["chat"])
    assert r._attempts == 0  # success closes the episode
    assert r._in_flight is False


def test_recover_final_failure_tells_model_to_stand_down():
    r, log = make_recovery()
    r._call_fix_error = lambda: (False, "timed out waiting for the arm to reboot")
    r._recover(HW_ERROR, attempt=3)
    assert any("manual reboot" in e for e in log["events"])
    assert any("giving up" in c for c in log["chat"])


def test_call_fix_error_paths():
    r, _ = make_recovery()
    assert r._call_fix_error() == (False, "/mars/arm/fix_error unavailable")

    def client(message, success=True):
        future = SimpleNamespace(done=lambda: True, result=lambda: SimpleNamespace(success=success, message=message))
        return SimpleNamespace(service_is_ready=lambda: True, call_async=lambda req: future)

    r._fix_client = client('{"error_ids": [3, 5], "status": "fixed"}')
    assert r._call_fix_error() == (True, "rebooted servo(s) 3, 5 and re-enabled their torque")
    r._fix_client = client('{"error_ids": [], "status": "no_errors"}')
    ok, detail = r._call_fix_error()
    assert ok and "cleared on its own" in detail
    r._fix_client = client("dynamixel port wedged", success=False)
    assert r._call_fix_error() == (False, "dynamixel port wedged")
