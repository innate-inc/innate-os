# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Host-side mailbox controller for in-page simulator environment switches."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from config import (
    ENVIRONMENT_CONTROL_CATALOG_PATH,
    ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH,
    ENVIRONMENT_CONTROL_DIR,
    ENVIRONMENT_CONTROL_HEARTBEAT_PATH,
    ENVIRONMENT_CONTROL_INTENT_LOCK_PATH,
    ENVIRONMENT_CONTROL_LOG_PATH,
    ENVIRONMENT_CONTROL_REQUEST_DIR,
    ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH,
    ENVIRONMENT_CONTROL_STATUS_DIR,
    ENVIRONMENT_CONTROL_STOP_PATH,
    OS_SESSION_LOG_PATH,
    SIMULATOR_LIFECYCLE_LOCK_PATH,
    StackError,
    build_os_env,
    ensure_state_dir,
    get_config,
)
from environment import (
    ENVIRONMENT_ID_RE,
    EnvironmentPack,
    activate_environment,
    available_environment_ids,
    load_active_environment,
    load_environment_pack,
    select_environment,
)
from runtime import (
    collect_os_process_status,
    ensure_os_container,
    ensure_world_server,
    ros_environment_is_current,
    stop_os_session,
    stop_world_server_and_wait,
    tail_file,
    wait_for_os_runtime_ready,
    wait_for_virtual_mars,
    world_environment_is_current,
)

ProgressCallback = Callable[[str], None]


class SwitchTransactionError(StackError):
    def __init__(self, message: str, recovered_environment: EnvironmentPack | None = None):
        super().__init__(message)
        self.recovered_environment = recovered_environment


@contextmanager
def simulator_lifecycle_lock(*, stop_event: threading.Event | None = None) -> Iterator[None]:
    ensure_state_dir()
    with SIMULATOR_LIFECYCLE_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise StackError("The simulator is shutting down; the switch was cancelled.")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        yield


def prepare_environment_control_directories() -> None:
    ensure_state_dir()
    for directory in (ENVIRONMENT_CONTROL_DIR, ENVIRONMENT_CONTROL_REQUEST_DIR, ENVIRONMENT_CONTROL_STATUS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


@contextmanager
def _environment_control_intent_lock() -> Iterator[None]:
    with ENVIRONMENT_CONTROL_INTENT_LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        temporary.write_text(
            json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _summary(pack: EnvironmentPack, *, fingerprint: bool = False) -> dict[str, str]:
    value = {"id": pack.id, "display_name": pack.display_name}
    if fingerprint:
        value["fingerprint"] = pack.fingerprint
    return value


def _request_summary(environment_id: str, pack: EnvironmentPack | None = None) -> dict[str, str]:
    return _summary(pack) if pack is not None else {"id": environment_id, "display_name": environment_id}


def _installed_packs(config: dict[str, object]) -> dict[str, EnvironmentPack]:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    packs: dict[str, EnvironmentPack] = {}
    for environment_id in available_environment_ids(sim_repo):
        try:
            packs[environment_id] = load_environment_pack(sim_repo, environment_id, validate_assets=True)
        except StackError:
            continue
    return packs


def _catalog(
    config: dict[str, object],
    active: EnvironmentPack | None,
    switch: dict[str, object] | None,
) -> dict[str, object]:
    packs = _installed_packs(config)
    if active is not None:
        installed = packs.get(active.id)
        if installed is None or installed.fingerprint != active.fingerprint:
            active = None
    return {
        "schema_version": 1,
        "active": _summary(active, fingerprint=True) if active else None,
        "environments": [_summary(pack) for pack in packs.values()],
        "switch": switch,
    }


def _read_request() -> tuple[str, str]:
    path = ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("invalid environment switch request file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if time.time() - path.stat().st_mtime < 1.0:
            raise BlockingIOError("environment switch request is still being written") from None
        raise
    if not isinstance(value, dict) or set(value) != {"request_id", "id"}:
        raise ValueError("invalid environment switch request")
    request_id, environment_id = value.get("request_id"), value.get("id")
    try:
        canonical_id = str(uuid.UUID(str(request_id)))
    except ValueError as exc:
        raise ValueError("invalid request_id") from exc
    if request_id != canonical_id:
        raise ValueError("invalid request_id")
    if (
        not isinstance(environment_id, str)
        or len(environment_id) > 64
        or not ENVIRONMENT_ID_RE.fullmatch(environment_id)
    ):
        raise ValueError("invalid environment id")
    return canonical_id, environment_id


def cancel_pending_environment_control_request() -> None:
    ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH.unlink(missing_ok=True)


def _message(error: BaseException) -> str:
    printable = "".join(character if character.isprintable() else " " for character in str(error))
    return " ".join(printable.split())[:1000] or error.__class__.__name__


def _start_environment_runtime(config: dict[str, object], pack: EnvironmentPack, report: ProgressCallback) -> None:
    select_environment(config, pack.id)
    report(f"Starting physics for {pack.display_name}...")
    config["world_endpoint"], _restarted = ensure_world_server(config)
    report(f"Starting simulator services for {pack.display_name}...")
    ensure_os_container(config, build_os_env(config), offline=True, preserve_container=True)
    report("Waiting for the refreshed simulator...")
    if not wait_for_os_runtime_ready(config, timeout_seconds=120.0) or not wait_for_virtual_mars(config):
        raise StackError(
            "The simulator did not become ready after switching environments.\n"
            f"Recent OS log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
        )
    if not ros_environment_is_current(config) or not world_environment_is_current(config):
        raise StackError("The refreshed runtime does not agree on the selected environment.")


def switch_running_environment(
    config: dict[str, object],
    environment_id: str,
    report: ProgressCallback,
) -> EnvironmentPack:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    target = load_environment_pack(sim_repo, environment_id, validate_assets=True)
    previous = load_active_environment(sim_repo, validate_assets=True)
    if previous is None:
        raise StackError("The running simulator has no valid active environment.")
    select_environment(config, previous.id)
    status = collect_os_process_status(config)
    if not status.get("os_running") or not status.get("os_session_running"):
        raise StackError("The simulator is not fully running. Start it with `./innate-sim up` first.")
    if (
        target.id == previous.id
        and target.fingerprint == previous.fingerprint
        and ros_environment_is_current(config)
        and world_environment_is_current(config)
    ):
        return target

    report("Pausing robot and navigation services...")
    try:
        stop_os_session(config)
        stop_world_server_and_wait()
        target = select_environment(config, target.id)
        report(f"Activating {target.display_name}...")
        activate_environment(target)
        _start_environment_runtime(config, target, report)
        return target
    except (Exception, KeyboardInterrupt) as target_error:
        try:
            with contextlib.suppress(Exception):
                stop_os_session(config)
            with contextlib.suppress(Exception):
                stop_world_server_and_wait()
            previous = select_environment(config, previous.id)
            activate_environment(previous)
            report(f"Restoring {previous.display_name}...")
            _start_environment_runtime(config, previous, report)
        except (Exception, KeyboardInterrupt) as recovery_error:
            with contextlib.suppress(Exception):
                stop_os_session(config)
            with contextlib.suppress(Exception):
                stop_world_server_and_wait()
            raise SwitchTransactionError(
                f"Could not switch to {target.display_name}; restoring {previous.display_name} also failed: "
                f"{_message(recovery_error)}"
            ) from target_error
        raise SwitchTransactionError(
            f"Could not switch to {target.display_name}: {_message(target_error)}; restored {previous.display_name}.",
            previous,
        ) from target_error


class EnvironmentControlDaemon:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.active: EnvironmentPack | None = None
        self.switch: dict[str, object] | None = None

    def _publish(self) -> None:
        _write_json(ENVIRONMENT_CONTROL_CATALOG_PATH, _catalog(self.config, self.active, self.switch))

    def _heartbeat(self) -> None:
        while not self.stop_event.is_set():
            if ENVIRONMENT_CONTROL_STOP_PATH.exists():
                self.stop_event.set()
                break
            with contextlib.suppress(OSError):
                _write_json(
                    ENVIRONMENT_CONTROL_HEARTBEAT_PATH,
                    {"schema_version": 1, "updated_at": time.time()},
                )
            if ENVIRONMENT_CONTROL_STOP_PATH.exists():
                ENVIRONMENT_CONTROL_HEARTBEAT_PATH.unlink(missing_ok=True)
                self.stop_event.set()
                break
            self.stop_event.wait(1.0)

    def _set_switch(
        self,
        request_id: str,
        target: EnvironmentPack | dict[str, str],
        **values: object,
    ) -> None:
        self.switch = {
            "request_id": request_id,
            "target": target if isinstance(target, dict) else _summary(target),
            **values,
        }
        self._publish()

    def _active_runtime_is_coherent(self) -> bool:
        if self.active is None:
            return False
        self.config["environment"] = self.active
        self.config["environment_id"] = self.active.id
        try:
            self.active.validate_assets()
            status = collect_os_process_status(self.config)
            return bool(
                status.get("os_running")
                and status.get("os_session_running")
                and ros_environment_is_current(self.config)
                and world_environment_is_current(self.config)
            )
        except Exception:
            return False

    def _fail_leftover_request(self) -> None:
        if not ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH.exists():
            return
        while True:
            try:
                request_id, environment_id = _read_request()
                break
            except BlockingIOError:
                # This is startup recovery, not the normal poll loop. Let an
                # in-flight atomic write finish, but never replay it as a new
                # request after this recovery check returns.
                if self.stop_event.wait(0.05):
                    return
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                cancel_pending_environment_control_request()
                return
        sim_repo: Path = self.config["sim_repo"]  # type: ignore[assignment]
        try:
            target: EnvironmentPack | dict[str, str] = load_environment_pack(sim_repo, environment_id)
        except StackError:
            # The proxy accepted this syntactically valid request while the
            # target was catalogued. Preserve its request ID even if the pack
            # disappeared before a replacement controller could inspect it.
            target = _request_summary(environment_id)
        with simulator_lifecycle_lock(stop_event=self.stop_event):
            recovered = self.active if self._active_runtime_is_coherent() else None
            if recovered is None:
                with contextlib.suppress(Exception):
                    stop_os_session(self.config)
                with contextlib.suppress(Exception):
                    stop_world_server_and_wait()
                self.active = None
            self._set_switch(
                request_id,
                target,
                state="failed",
                message=(
                    f"The previous switch was interrupted; kept verified {recovered.display_name}."
                    if recovered is not None
                    else "The previous switch was interrupted; simulator services were stopped for safety."
                ),
            )
            cancel_pending_environment_control_request()

    def _process_request(self, request_id: str, environment_id: str) -> None:
        sim_repo: Path = self.config["sim_repo"]  # type: ignore[assignment]
        try:
            target = load_environment_pack(sim_repo, environment_id, validate_assets=True)
        except StackError as exc:
            self._set_switch(
                request_id,
                _request_summary(environment_id),
                state="failed",
                message=_message(exc),
            )
            cancel_pending_environment_control_request()
            return
        self._set_switch(request_id, target, state="queued", message="Waiting to switch...")

        def report(message: str) -> None:
            self._set_switch(request_id, target, state="running", message=message)

        try:
            with simulator_lifecycle_lock(stop_event=self.stop_event):
                selected = switch_running_environment(self.config, target.id, report)
        except SwitchTransactionError as exc:
            self.active = exc.recovered_environment
            self._set_switch(
                request_id,
                target,
                state="failed",
                message=_message(exc),
            )
            cancel_pending_environment_control_request()
        except Exception as exc:
            self._set_switch(request_id, target, state="failed", message=_message(exc))
            cancel_pending_environment_control_request()
        else:
            self.active = selected
            self._set_switch(
                request_id,
                target,
                state="ready",
                message=f"{selected.display_name} is ready.",
            )
            cancel_pending_environment_control_request()

    def run(self, *, acquire_singleton: bool = True) -> int:
        prepare_environment_control_directories()
        if not acquire_singleton:
            return self._run()
        with ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH.open("a+", encoding="utf-8") as singleton:
            try:
                fcntl.flock(singleton.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 2
            return self._run()

    def _run(self) -> int:
        self.active = load_active_environment(self.config["sim_repo"], validate_assets=True)  # type: ignore[arg-type]
        heartbeat = threading.Thread(target=self._heartbeat, daemon=True)
        heartbeat.start()
        try:
            self._fail_leftover_request()
            self._publish()
            while not self.stop_event.wait(0.25):
                if not ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH.exists():
                    continue
                try:
                    request = _read_request()
                except BlockingIOError:
                    continue
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    cancel_pending_environment_control_request()
                    continue
                self._process_request(*request)
        finally:
            self.stop_event.set()
            heartbeat.join(timeout=2.0)
            ENVIRONMENT_CONTROL_HEARTBEAT_PATH.unlink(missing_ok=True)
        return 0


def _supervise_environment_control_daemon(config: dict[str, object]) -> int:
    """Keep the singleton lock while replacing an abnormally exited worker."""
    prepare_environment_control_directories()
    with ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH.open("a+", encoding="utf-8") as singleton:
        try:
            fcntl.flock(singleton.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2

        worker = [sys.executable, str(Path(__file__).resolve()), "serve"]
        while not ENVIRONMENT_CONTROL_STOP_PATH.exists():
            result = subprocess.run(
                worker,
                cwd=config["os_repo"],  # type: ignore[arg-type]
                stdin=subprocess.DEVNULL,
                check=False,
                pass_fds=(singleton.fileno(),),
            )
            if result.returncode == 0:
                return 0
            deadline = time.monotonic() + 0.5
            while not ENVIRONMENT_CONTROL_STOP_PATH.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        ENVIRONMENT_CONTROL_HEARTBEAT_PATH.unlink(missing_ok=True)
        return 0


def _lock_is_held() -> bool:
    prepare_environment_control_directories()
    with ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def _heartbeat_is_fresh() -> bool:
    try:
        value = json.loads(ENVIRONMENT_CONTROL_HEARTBEAT_PATH.read_text(encoding="utf-8"))
        updated_at = value.get("updated_at") if isinstance(value, dict) else None
        timestamp = float(updated_at)
        age = time.time() - timestamp
        return value.get("schema_version") == 1 and math.isfinite(timestamp) and -1.0 <= age <= 5.0
    except (AttributeError, OSError, OverflowError, RecursionError, TypeError, ValueError):
        return False


def _ensure_environment_control_stop_requested() -> None:
    """Stop a failed launch without replacing a newer user lifecycle intent."""
    prepare_environment_control_directories()
    with _environment_control_intent_lock():
        if not ENVIRONMENT_CONTROL_STOP_PATH.exists():
            _write_json(ENVIRONMENT_CONTROL_STOP_PATH, {"request_id": str(uuid.uuid4())})
    ENVIRONMENT_CONTROL_HEARTBEAT_PATH.unlink(missing_ok=True)


def _stop_spawned_supervisor(process: subprocess.Popen[bytes]) -> None:
    _ensure_environment_control_stop_requested()
    try:
        process.wait(timeout=5.0)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2.0)


def ensure_environment_control_daemon(config: dict[str, object]) -> None:
    prepare_environment_control_directories()
    deadline = time.monotonic() + 8.0
    while _lock_is_held():
        if _heartbeat_is_fresh():
            return
        if ENVIRONMENT_CONTROL_STOP_PATH.exists():
            raise StackError("Simulator startup was cancelled by another lifecycle command.")
        if time.monotonic() >= deadline:
            raise StackError("The simulator environment controller is running but unresponsive.")
        time.sleep(0.05)
    if ENVIRONMENT_CONTROL_STOP_PATH.exists():
        raise StackError("Simulator startup was cancelled by another lifecycle command.")
    ENVIRONMENT_CONTROL_HEARTBEAT_PATH.unlink(missing_ok=True)
    with ENVIRONMENT_CONTROL_LOG_PATH.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "supervise"],
            cwd=config["os_repo"],  # type: ignore[arg-type]
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if ENVIRONMENT_CONTROL_STOP_PATH.exists():
            _stop_spawned_supervisor(process)
            raise StackError("Simulator startup was cancelled by another lifecycle command.")
        if _lock_is_held() and _heartbeat_is_fresh():
            return
        status = process.poll()
        if status is not None and status != 2:
            break
        time.sleep(0.05)
    if _lock_is_held() and _heartbeat_is_fresh():
        return
    if process.poll() is None:
        _stop_spawned_supervisor(process)
    raise StackError(
        "The simulator environment controller did not start.\n"
        f"Recent controller log output:\n{tail_file(ENVIRONMENT_CONTROL_LOG_PATH, limit=40)}"
    )


def request_environment_control_daemon_stop() -> str:
    prepare_environment_control_directories()
    request_id = str(uuid.uuid4())
    with _environment_control_intent_lock():
        _write_json(ENVIRONMENT_CONTROL_STOP_PATH, {"request_id": request_id})
    ENVIRONMENT_CONTROL_HEARTBEAT_PATH.unlink(missing_ok=True)
    return request_id


def _validate_environment_control_intent(request_id: str) -> None:
    try:
        value = json.loads(ENVIRONMENT_CONTROL_STOP_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict) or value.get("request_id") != request_id:
        raise StackError("Simulator lifecycle command was superseded by a newer command.")


def validate_environment_control_stop_request(request_id: str) -> None:
    prepare_environment_control_directories()
    with _environment_control_intent_lock():
        _validate_environment_control_intent(request_id)


@contextmanager
def environment_control_stop_request_lock(request_id: str) -> Iterator[None]:
    """Keep a destructive command current until its mutation is committed."""
    prepare_environment_control_directories()
    with _environment_control_intent_lock():
        _validate_environment_control_intent(request_id)
        yield


def authorize_environment_control_daemon_start(request_id: str) -> None:
    """Consume only this `up` command's stop request, never a newer intent."""
    prepare_environment_control_directories()
    with _environment_control_intent_lock():
        _validate_environment_control_intent(request_id)
        ENVIRONMENT_CONTROL_STOP_PATH.unlink()
        ENVIRONMENT_CONTROL_HEARTBEAT_PATH.unlink(missing_ok=True)


def wait_for_environment_control_daemon_stop() -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _lock_is_held():
            return
        time.sleep(0.05)
    if _lock_is_held():
        raise StackError("Timed out stopping the simulator environment controller.")


def main() -> int:
    config = get_config()
    if sys.argv[1:] == ["supervise"]:
        return _supervise_environment_control_daemon(config)
    if sys.argv[1:] == ["serve"]:
        return EnvironmentControlDaemon(config).run(acquire_singleton=False)
    return EnvironmentControlDaemon(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
