# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Automatic recovery from latched arm hardware errors (overcurrent trips).

Watches ``/mars/arm/status`` (~0.2 Hz). When a servo latches a hardware error
— overload is the overcurrent trip; electrical/encoder/voltage faults latch
the same way — the servo drops torque and stays limp until rebooted, so while
the brain is active this: stops whatever skill is running (a pick flailing on
a limp arm helps nobody), tells the model what happened, calls
``/mars/arm/fix_error`` (reboots exactly the errored servos, then reconfigures
and re-torques them), folds the arm to rest (``/mars/arm/rest``, so it is not
left sagging where the limp joint fell), and reports the outcome. The same
recovery the webapp's "Reboot arm" + torque buttons do by hand.

Transient warnings in the same status stream ("high load", "high temperature",
comm hiccups) are NOT latched errors and never trigger a reboot.

Threading: the status callback (node spin thread) only decides and spawns; the
recovery sequence runs on a daemon thread because fix_error takes seconds —
its call_async future is resolved by the main node's spin loop, so the thread
just polls it. One recovery at a time; per-episode attempts are capped and the
counter re-arms only after the arm reports healthy again, so a physically
jammed arm gets a few tries and then a human, not a reboot loop.
"""

from __future__ import annotations

import json
import threading
import time

from mars_msgs.msg import ArmStatus
from std_srvs.srv import Trigger

ARM_STATUS_TOPIC = "/mars/arm/status"
FIX_ERROR_SERVICE = "/mars/arm/fix_error"
REST_SERVICE = "/mars/arm/rest"

# The latched-error marker in ArmStatus.error (see arm_services.cpp,
# describeHardwareError) — "high load"/"high temperature" don't carry it.
_HARDWARE_ERROR_MARKER = "hardware error"

_MAX_ATTEMPTS_PER_EPISODE = 3
_RETRY_GAP_SEC = 10.0  # between attempts while the error persists
_FIX_TIMEOUT_SEC = 25.0  # reboot walks the servos and "takes a few seconds"
_REST_TIMEOUT_SEC = 12.0  # lift + fold is ~5 s


class ArmRecovery:
    def __init__(self, node, state, *, runner, chat, brain):
        self._logger = node.get_logger()
        self._state = state
        self._runner = runner
        self._chat = chat
        self._brain = brain
        self._fix_client = node.create_client(Trigger, FIX_ERROR_SERVICE)
        self._rest_client = node.create_client(Trigger, REST_SERVICE)
        self._in_flight = False
        self._attempts = 0
        self._last_attempt_at = 0.0
        node.create_subscription(ArmStatus, ARM_STATUS_TOPIC, self._on_status, 10)

    # --- policy (node spin thread) ---
    def _on_status(self, msg: ArmStatus) -> None:
        if msg.is_ok:
            if self._attempts:
                self._logger.info("[ArmRecovery] Arm healthy again; re-arming auto-recovery.")
            self._attempts = 0
            return
        if _HARDWARE_ERROR_MARKER not in msg.error:
            return  # transient warning (load/temperature/comms) — not ours to reboot
        if not self._state.is_brain_active:
            return  # manual sessions keep the webapp's explicit reboot flow
        if self._in_flight or self._attempts >= _MAX_ATTEMPTS_PER_EPISODE:
            return
        if time.monotonic() - self._last_attempt_at < _RETRY_GAP_SEC:
            return
        self._in_flight = True
        self._attempts += 1
        self._last_attempt_at = time.monotonic()
        threading.Thread(target=self._recover, args=(msg.error, self._attempts), daemon=True).start()

    # --- recovery sequence (daemon thread) ---
    def _recover(self, error: str, attempt: int) -> None:
        try:
            self._logger.error(f"[ArmRecovery] {error} — auto-recovery attempt {attempt}")
            stopped = self._stop_running_skill()
            self._chat.emit_system(f"⚠️ Arm protection tripped: {error}. Rebooting the arm servos…")
            self._brain.add_event(
                f"Arm hardware protection tripped ({error}). "
                + ("The running skill was stopped. " if stopped else "")
                + "The arm servos are being rebooted and torque re-enabled automatically — "
                "the arm is unusable for the next few seconds; wait for the recovery result."
            )
            ok, detail = self._call_fix_error()
            if ok:
                self._attempts = 0  # this episode is over; a new trip starts fresh
                rested, rest_detail = self._call_rest()
                self._chat.emit_system(f"{'✅' if rested else '⚠️'} Arm recovered: {detail}; {rest_detail}")
                self._brain.add_event(
                    f"Arm recovery succeeded: {detail}; {rest_detail}. "
                    + ("The arm is at rest and usable again." if rested else "The arm is usable but not at rest.")
                )
            else:
                self._logger.error(f"[ArmRecovery] fix_error failed: {detail}")
                final = attempt >= _MAX_ATTEMPTS_PER_EPISODE
                self._chat.emit_system(
                    f"⚠️ Arm recovery failed ({detail})"
                    + (" — giving up; reboot the arm from the webapp or power-cycle it." if final else ", retrying…")
                )
                self._brain.add_event(
                    f"Arm recovery attempt {attempt} failed: {detail}. "
                    + (
                        "No more automatic attempts — tell the user the arm needs a manual reboot "
                        "or power-cycle, and do not use arm skills."
                        if final
                        else "Another attempt follows shortly; do not use arm skills yet."
                    )
                )
        except Exception as e:  # a recovery crash must never take the node down
            self._logger.error(f"[ArmRecovery] recovery crashed: {e!r}")
        finally:
            self._in_flight = False

    def _stop_running_skill(self) -> bool:
        """Mirror the STOP_SKILL tool: brain-owned goals cancel directly, manual runs via the server."""
        if self._runner.has_active_goal:
            self._runner.cancel_active_goal()
            return True
        if self._state.primitive_running is not None:
            return self._runner.cancel_external()
        return False

    def _call_fix_error(self) -> tuple[bool, str]:
        response, failure = self._call(self._fix_client, FIX_ERROR_SERVICE, _FIX_TIMEOUT_SEC)
        if response is None:
            return False, failure
        if not response.success:
            return False, response.message
        # message is JSON: {"error_ids": [...], "status": "fixed" | "no_errors"}
        try:
            result = json.loads(response.message)
        except (json.JSONDecodeError, TypeError):
            return True, response.message or "servos rebooted, torque re-enabled"
        if result.get("status") == "no_errors":
            return True, "no latched errors found — the trip cleared on its own"
        ids = ", ".join(str(i) for i in result.get("error_ids", []))
        return True, f"rebooted servo(s) {ids} and re-enabled their torque"

    def _call_rest(self) -> tuple[bool, str]:
        response, failure = self._call(self._rest_client, REST_SERVICE, _REST_TIMEOUT_SEC)
        if response is None:
            return False, failure
        return response.success, response.message

    def _call(self, client, service: str, timeout_sec: float):
        """Call a Trigger service and wait (the main spin loop resolves the
        future); the response, or None with the reason."""
        if not client.service_is_ready():
            return None, f"{service} unavailable"
        future = client.call_async(Trigger.Request())
        deadline = time.monotonic() + timeout_sec
        while not future.done():
            if time.monotonic() > deadline:
                future.cancel()
                return None, f"timed out waiting for {service}"
            time.sleep(0.2)
        response = future.result()
        return (response, "") if response is not None else (None, f"no response from {service}")
