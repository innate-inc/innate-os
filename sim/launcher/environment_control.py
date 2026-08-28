# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Host-side control plane for browser-triggered simulator environment switches.

The webapp runs inside Docker while MuJoCo runs on the host.  Rather than give
the container a Docker socket or expose another unauthenticated host port, the
two sides exchange small JSON files through a narrowly mounted mailbox:

* the proxy may create ``requests/current.json``;
* the host owns the read-only-in-container ``status`` tree.

The daemon validates every byte again before it touches the runtime, serializes
the operation with launcher lifecycle commands, and publishes atomic progress
records.  Environment selection itself is a transaction: on a failed target
start it attempts to restore the previous descriptor, physics world, and ROS
session; if recovery also fails it leaves ROS stopped rather than mixing worlds.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from config import (
    ENVIRONMENT_CONTROL_CATALOG_PATH,
    ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH,
    ENVIRONMENT_CONTROL_DIR,
    ENVIRONMENT_CONTROL_HEARTBEAT_PATH,
    ENVIRONMENT_CONTROL_JOBS_DIR,
    ENVIRONMENT_CONTROL_LOG_PATH,
    ENVIRONMENT_CONTROL_PID_PATH,
    ENVIRONMENT_CONTROL_REQUEST_DIR,
    ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH,
    ENVIRONMENT_CONTROL_STATUS_DIR,
    OS_SESSION_LOG_PATH,
    SIMULATOR_LIFECYCLE_LOCK_PATH,
    StackError,
    build_os_env,
    ensure_state_dir,
    get_config,
)
from environment import (
    ACTIVE_ENVIRONMENT_FILENAME,
    ENVIRONMENT_ID_RE,
    EnvironmentPack,
    activate_environment,
    available_environment_ids,
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
    world_server_running,
)

CONTROL_SCHEMA_VERSION = 1
CONTROL_POLL_SECONDS = 0.25
CONTROL_HEARTBEAT_SECONDS = 1.0
CONTROL_HEARTBEAT_STALE_SECONDS = 5.0
CONTROL_START_TIMEOUT_SECONDS = 8.0
CONTROL_STOP_TIMEOUT_SECONDS = 5.0
CONTROL_REQUEST_MAX_BYTES = 4096
CONTROL_REQUEST_PARTIAL_GRACE_SECONDS = 1.0
CONTROL_REQUEST_MAX_AGE_SECONDS = 300.0
CONTROL_REQUEST_FUTURE_SKEW_SECONDS = 1.0
CONTROL_JOB_HISTORY_LIMIT = 20
CONTROL_ENVIRONMENT_ID_MAX_LENGTH = 64
CONTROL_ENVIRONMENT_LIMIT = 256

ProgressCallback = Callable[[str, str, int], None]


class InvalidControlRequest(ValueError):
    """A mailbox request was not safe to execute."""

    def __init__(
        self,
        message: str,
        *,
        job_id: str | None = None,
        environment_id: str | None = None,
        file_identity: tuple[int, int] | None = None,
    ):
        super().__init__(message)
        self.job_id = job_id
        self.environment_id = environment_id
        self.file_identity = file_identity


class TransientControlRequest(ValueError):
    """The proxy has created the request inode but has not finished writing."""


class ControllerStopping(StackError):
    """The daemon was asked to stop before it acquired the lifecycle lock."""


class SwitchTransactionError(StackError):
    """Target activation failed, optionally after a successful rollback."""

    def __init__(
        self,
        message: str,
        *,
        recovered_environment: EnvironmentPack | None = None,
        recovery_error: BaseException | None = None,
    ):
        super().__init__(message)
        self.recovered_environment = recovered_environment
        self.recovery_error = recovery_error


@dataclass(frozen=True)
class ControlRequest:
    job_id: str
    environment_id: str
    created_at: float
    file_identity: tuple[int, int]


@contextmanager
def simulator_lifecycle_lock(*, stop_event: threading.Event | None = None) -> Iterator[None]:
    """Serialize browser switches with every launcher lifecycle mutation."""
    ensure_state_dir()
    SIMULATOR_LIFECYCLE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SIMULATOR_LIFECYCLE_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        if stop_event is None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            while True:
                if stop_event.is_set():
                    raise ControllerStopping("The simulator is shutting down; the switch was cancelled.")
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if stop_event.wait(0.1):
                        raise ControllerStopping("The simulator is shutting down; the switch was cancelled.") from None
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def prepare_environment_control_directories() -> None:
    """Create bind sources before Compose can create them as root-owned dirs."""
    ensure_state_dir()
    for directory in (
        ENVIRONMENT_CONTROL_DIR,
        ENVIRONMENT_CONTROL_REQUEST_DIR,
        ENVIRONMENT_CONTROL_STATUS_DIR,
        ENVIRONMENT_CONTROL_JOBS_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(payload, output, allow_nan=False, separators=(",", ":"), sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bounded_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    safe = "".join(character if ord(character) >= 0x20 or character in "\n\t" else "?" for character in text)
    return safe[:2000]


def _pack_identity(pack: EnvironmentPack) -> dict[str, str]:
    return {"id": pack.id, "display_name": pack.display_name, "fingerprint": pack.fingerprint}


def _valid_environment_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= CONTROL_ENVIRONMENT_ID_MAX_LENGTH
        and ENVIRONMENT_ID_RE.fullmatch(value) is not None
    )


def _valid_display_name(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 160 and not any(ord(character) < 0x20 for character in value)


def _active_descriptor(sim_repo: Path) -> dict[str, object] | None:
    path = sim_repo / "assets" / ACTIVE_ENVIRONMENT_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    environment_id = value.get("id")
    display_name = value.get("display_name")
    fingerprint = value.get("fingerprint")
    if not isinstance(environment_id, str) or not ENVIRONMENT_ID_RE.fullmatch(environment_id):
        return None
    if not isinstance(display_name, str) or not display_name.strip():
        return None
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        return None
    return value


def write_environment_catalog(config: dict[str, object]) -> None:
    """Publish only installed packs whose complete asset sets validate."""
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    installed: dict[str, EnvironmentPack] = {}
    for environment_id in sorted(available_environment_ids(sim_repo)):
        if not _valid_environment_id(environment_id):
            continue
        try:
            pack = load_environment_pack(sim_repo, environment_id, validate_assets=True)
        except StackError:
            continue
        if not _valid_display_name(pack.display_name):
            continue
        installed[pack.id] = pack

    descriptor = _active_descriptor(sim_repo)
    active: dict[str, str] | None = None
    active_pack: EnvironmentPack | None = None
    if descriptor is not None:
        active_pack = installed.get(str(descriptor["id"]))
        if (
            active_pack is not None
            and descriptor.get("display_name") == active_pack.display_name
            and descriptor.get("fingerprint") == active_pack.fingerprint
        ):
            active = _pack_identity(active_pack)
        else:
            active_pack = None

    selected_ids = list(installed)[:CONTROL_ENVIRONMENT_LIMIT]
    if active_pack is not None and active_pack.id not in selected_ids:
        selected_ids[-1] = active_pack.id
        selected_ids.sort()
    environments = [
        {"id": installed[environment_id].id, "display_name": installed[environment_id].display_name}
        for environment_id in selected_ids
    ]
    _atomic_write_json(
        ENVIRONMENT_CONTROL_CATALOG_PATH,
        {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "active": active,
            "environments": environments,
        },
    )


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value!r}")


def _read_control_request(path: Path, *, now: float | None = None) -> ControlRequest:
    """Read one bounded request from the inode opened without following links."""
    now = time.time() if now is None else now
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        try:
            metadata = path.lstat()
        except OSError:
            raise TransientControlRequest(str(exc)) from exc
        file_identity = (metadata.st_dev, metadata.st_ino)
        if stat.S_ISLNK(metadata.st_mode):
            raise InvalidControlRequest(
                "environment switch request may not be a symlink",
                file_identity=file_identity,
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise InvalidControlRequest(
                "environment switch request must be a regular file",
                file_identity=file_identity,
            ) from exc
        if max(0.0, now - metadata.st_mtime) >= CONTROL_REQUEST_PARTIAL_GRACE_SECONDS:
            raise InvalidControlRequest(
                "environment switch request could not be opened",
                file_identity=file_identity,
            ) from exc
        raise TransientControlRequest(str(exc)) from exc

    try:
        metadata = os.fstat(descriptor)
        file_identity = (metadata.st_dev, metadata.st_ino)
        age = max(0.0, now - metadata.st_mtime)
        if not stat.S_ISREG(metadata.st_mode):
            raise InvalidControlRequest(
                "environment switch request must be a regular file",
                file_identity=file_identity,
            )
        if metadata.st_size > CONTROL_REQUEST_MAX_BYTES:
            raise InvalidControlRequest(
                "environment switch request is too large",
                file_identity=file_identity,
            )
        if metadata.st_size == 0 and age < CONTROL_REQUEST_PARTIAL_GRACE_SECONDS:
            raise TransientControlRequest("environment switch request is still being written")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(CONTROL_REQUEST_MAX_BYTES + 1)
    finally:
        os.close(descriptor)

    if len(raw) > CONTROL_REQUEST_MAX_BYTES:
        raise InvalidControlRequest(
            "environment switch request is too large",
            file_identity=file_identity,
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if age < CONTROL_REQUEST_PARTIAL_GRACE_SECONDS:
            raise TransientControlRequest("environment switch request is still being written") from exc
        raise InvalidControlRequest(
            "environment switch request is not valid JSON",
            file_identity=file_identity,
        ) from exc
    if not isinstance(value, dict):
        raise InvalidControlRequest(
            "environment switch request must be a JSON object",
            file_identity=file_identity,
        )

    raw_job_id = value.get("job_id")
    raw_environment_id = value.get("environment_id")
    job_id = _canonical_uuid(raw_job_id)
    environment_id = raw_environment_id if _valid_environment_id(raw_environment_id) else None
    allowed = {"schema_version", "job_id", "environment_id", "created_at"}
    if set(value) != allowed:
        raise InvalidControlRequest(
            "environment switch request has unexpected or missing fields",
            job_id=job_id,
            environment_id=environment_id,
            file_identity=file_identity,
        )
    if type(value.get("schema_version")) is not int or value.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise InvalidControlRequest(
            "environment switch request schema is unsupported",
            job_id=job_id,
            environment_id=environment_id,
            file_identity=file_identity,
        )
    if job_id is None:
        raise InvalidControlRequest(
            "environment switch request job_id is not a canonical UUID",
            file_identity=file_identity,
        )
    if environment_id is None:
        raise InvalidControlRequest(
            "environment switch request environment_id is invalid",
            job_id=job_id,
            file_identity=file_identity,
        )
    created_at = value.get("created_at")
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)) or not math.isfinite(float(created_at)):
        raise InvalidControlRequest(
            "environment switch request created_at is invalid",
            job_id=job_id,
            environment_id=environment_id,
            file_identity=file_identity,
        )
    created = float(created_at)
    if now - created > CONTROL_REQUEST_MAX_AGE_SECONDS or created - now > CONTROL_REQUEST_FUTURE_SKEW_SECONDS:
        raise InvalidControlRequest(
            "environment switch request is stale",
            job_id=job_id,
            environment_id=environment_id,
            file_identity=file_identity,
        )
    return ControlRequest(
        job_id=job_id,
        environment_id=environment_id,
        created_at=created,
        file_identity=file_identity,
    )


def _remove_control_request(path: Path, file_identity: tuple[int, int] | None) -> None:
    """Remove only the exact request inode that was validated or rejected."""
    if file_identity is None:
        return
    try:
        metadata = path.lstat()
    except OSError:
        return
    if (metadata.st_dev, metadata.st_ino) != file_identity:
        return
    try:
        if stat.S_ISDIR(metadata.st_mode):
            path.rmdir()
        else:
            path.unlink()
    except OSError:
        return


def cancel_pending_environment_control_request() -> None:
    """Cancel the fixed mailbox inode after its only writer has stopped."""
    try:
        metadata = ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH.lstat()
    except OSError:
        return
    _remove_control_request(
        ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH,
        (metadata.st_dev, metadata.st_ino),
    )


class JobStatus:
    def __init__(
        self,
        *,
        job_id: str,
        environment_id: str,
        display_name: str,
        started_at: float,
        replace_existing: bool = False,
    ) -> None:
        self.path = ENVIRONMENT_CONTROL_JOBS_DIR / f"{job_id}.json"
        if not replace_existing and (self.path.exists() or self.path.is_symlink()):
            raise FileExistsError(f"environment switch job {job_id} already exists")
        now = time.time()
        self.payload: dict[str, object] = {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "job_id": job_id,
            "target": {"id": environment_id, "display_name": display_name},
            "state": "queued",
            "phase": "queued",
            "message": f"Waiting to switch to {display_name}...",
            "progress": 0,
            "started_at": started_at,
            "updated_at": max(started_at, now),
        }
        self._lock = threading.Lock()
        self._write()

    def _write(self) -> None:
        _atomic_write_json(self.path, self.payload)

    def update(
        self,
        *,
        state: str = "running",
        phase: str,
        message: str,
        progress: int,
        fingerprint: str | None = None,
        error: str | None = None,
        recovered_environment: EnvironmentPack | None = None,
        terminal: bool = False,
    ) -> None:
        if state not in {"queued", "running", "ready", "failed"}:
            raise ValueError(f"invalid environment-control job state: {state}")
        with self._lock:
            updated_at = max(float(self.payload["updated_at"]), time.time())
            next_progress = max(int(self.payload["progress"]), max(0, min(100, int(progress))))
            self.payload.update(
                {
                    "state": state,
                    "phase": phase,
                    "message": message,
                    "progress": next_progress,
                    "updated_at": updated_at,
                }
            )
            if fingerprint:
                self.payload["fingerprint"] = fingerprint
            if error:
                self.payload["error"] = error[:2000]
            if recovered_environment is not None:
                self.payload["recovered_environment"] = _pack_identity(recovered_environment)
            if terminal:
                self.payload["finished_at"] = updated_at
            self._write()


def _require_running_stack(config: dict[str, object]) -> None:
    status = collect_os_process_status(config)
    if not status.get("os_running"):
        raise StackError("The simulator container is not running. Start it with `./innate-sim up` first.")
    if not status.get("os_session_running"):
        raise StackError("The simulator ROS session is not running. Start it with `./innate-sim up` first.")
    if not world_server_running():
        raise StackError("The simulator physics world is not running. Start it with `./innate-sim up` first.")


def _wait_for_refreshed_runtime(config: dict[str, object], report: ProgressCallback) -> None:
    report("waiting_ros", "Waiting for the refreshed ROS bridge and brain client...", 78)
    if not wait_for_os_runtime_ready(config, timeout_seconds=120.0):
        raise StackError(
            "The ROS session did not become ready after switching environments.\n"
            f"Recent OS log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
        )
    report("waiting_sim", "Waiting for the refreshed simulator driver...", 90)
    if not wait_for_virtual_mars(config):
        raise StackError(
            "The sim driver did not publish /odom after switching environments.\n"
            f"Recent OS log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
        )
    if not ros_environment_is_current(config) or not world_environment_is_current(config):
        raise StackError("The refreshed ROS session and physics world do not agree on the selected environment.")


def _start_environment_runtime(
    config: dict[str, object],
    pack: EnvironmentPack,
    report: ProgressCallback,
) -> None:
    select_environment(config, pack.id)
    report("starting_physics", f"Starting physics for {pack.display_name}...", 42)
    config["world_endpoint"], _world_restarted = ensure_world_server(config)
    report("starting_ros", f"Starting navigation and robot services for {pack.display_name}...", 62)
    ensure_os_container(config, build_os_env(config), offline=True, preserve_container=True)
    _wait_for_refreshed_runtime(config, report)


def _recover_previous_environment(
    config: dict[str, object],
    previous: EnvironmentPack,
    report: ProgressCallback,
) -> None:
    report("rolling_back", f"Restoring {previous.display_name}...", 45)
    # Stop any partial target consumers before publishing the previous
    # descriptor.  Failure here aborts recovery rather than mixing them.
    stop_os_session(config)
    stop_world_server_and_wait()
    previous = select_environment(config, previous.id)
    previous.validate_assets()
    activate_environment(previous)
    _start_environment_runtime(config, previous, report)


def switch_running_environment(
    config: dict[str, object],
    environment_id: str,
    report: ProgressCallback,
) -> EnvironmentPack:
    """Switch the live stack, restoring the previous pack on target failure."""
    report("validating", f"Checking environment {environment_id!r}...", 5)
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    target = load_environment_pack(sim_repo, environment_id, validate_assets=True)
    if not _valid_display_name(target.display_name):
        raise StackError(f"Environment {environment_id!r} has an invalid display name.")

    descriptor = _active_descriptor(sim_repo)
    if descriptor is None:
        raise StackError("The running simulator has no valid active environment descriptor.")
    previous = load_environment_pack(sim_repo, str(descriptor["id"]), validate_assets=True)
    if not _valid_display_name(previous.display_name):
        raise StackError("The active environment has an invalid display name.")
    if descriptor.get("fingerprint") != previous.fingerprint:
        raise StackError(
            "The active environment descriptor does not match the installed asset layers. "
            "Run `./innate-sim up` once to reconcile them before switching in the browser."
        )

    select_environment(config, previous.id)
    _require_running_stack(config)
    if (
        target.id == previous.id
        and target.fingerprint == previous.fingerprint
        and ros_environment_is_current(config)
        and world_environment_is_current(config)
    ):
        report("ready", f"{target.display_name} is already active.", 100)
        return target

    mutation_started = False
    committed = False
    try:
        report("stopping_runtime", "Pausing robot and navigation services...", 18)
        mutation_started = True
        stop_os_session(config)
        stop_world_server_and_wait()

        target = select_environment(config, target.id)
        target.validate_assets()
        report("activating", f"Activating {target.display_name}...", 30)
        activate_environment(target)
        committed = True
        _start_environment_runtime(config, target, report)
        return target
    except Exception as target_error:
        if not mutation_started and not committed:
            raise
        try:
            _recover_previous_environment(config, previous, report)
        except Exception as recovery_error:
            # Recovery is intentionally fail-closed: make one final best effort
            # to stop partial ROS consumers, but never tear down the container
            # (the page and its diagnostic/status endpoint stay alive).
            with contextlib.suppress(Exception):
                stop_os_session(config)
            raise SwitchTransactionError(
                f"Could not switch to {target.display_name}, and restoring {previous.display_name} also failed: "
                f"{_bounded_error(recovery_error)}",
                recovery_error=recovery_error,
            ) from target_error
        raise SwitchTransactionError(
            f"Could not switch to {target.display_name}; restored {previous.display_name}.",
            recovered_environment=previous,
        ) from target_error


def _target_display_name(config: dict[str, object], environment_id: str) -> str:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    try:
        display_name = load_environment_pack(sim_repo, environment_id).display_name
        return display_name if _valid_display_name(display_name) else environment_id
    except StackError:
        return environment_id


def _existing_job_state(job_id: str, environment_id: str) -> tuple[str, str] | None:
    path = ENVIRONMENT_CONTROL_JOBS_DIR / f"{job_id}.json"
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    target = value.get("target")
    state = value.get("state")
    phase = value.get("phase")
    if (
        value.get("schema_version") != CONTROL_SCHEMA_VERSION
        or value.get("job_id") != job_id
        or not isinstance(target, dict)
        or target.get("id") != environment_id
        or state not in {"queued", "running", "ready", "failed"}
        or not isinstance(phase, str)
    ):
        return None
    return state, phase


def _healthy_active_environment(config: dict[str, object]) -> EnvironmentPack | None:
    """Return the active pack only when every live consumer proves coherence."""
    try:
        sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
        descriptor = _active_descriptor(sim_repo)
        if descriptor is None:
            return None
        pack = load_environment_pack(sim_repo, str(descriptor["id"]), validate_assets=True)
        if (
            descriptor.get("display_name") != pack.display_name
            or descriptor.get("fingerprint") != pack.fingerprint
            or not _valid_display_name(pack.display_name)
        ):
            return None
        select_environment(config, pack.id)
        process_status = collect_os_process_status(config)
        required_processes = (
            "os_running",
            "os_session_running",
            "rosbridge_process_live",
            "brain_process_live",
            "sim_driver_process_live",
        )
        if not all(process_status.get(key) for key in required_processes):
            return None
        if not world_server_running():
            return None
        if not ros_environment_is_current(config) or not world_environment_is_current(config):
            return None
        return pack
    except (KeyError, OSError, StackError):
        return None


def _verified_recovery_environment(
    config: dict[str, object],
    *,
    stop_event: threading.Event | None,
) -> EnvironmentPack | None:
    """Prove recovery while serialized against CLI lifecycle operations."""
    try:
        with simulator_lifecycle_lock(stop_event=stop_event):
            return _healthy_active_environment(config)
    except ControllerStopping:
        return None


def _fail_interrupted_job(
    config: dict[str, object],
    request: ControlRequest,
    *,
    stop_event: threading.Event | None,
) -> None:
    """Fail closed after a crash left evidence that mutation had begun."""
    status = JobStatus(
        job_id=request.job_id,
        environment_id=request.environment_id,
        display_name=_target_display_name(config, request.environment_id),
        started_at=time.time(),
        replace_existing=True,
    )
    repaired = _healthy_active_environment(config)
    if repaired is not None:
        with contextlib.suppress(OSError):
            write_environment_catalog(config)
        if repaired.id == request.environment_id:
            status.update(
                state="ready",
                phase="ready",
                message=("The controller restarted, then verified that the requested environment is ready."),
                progress=100,
                fingerprint=repaired.fingerprint,
                terminal=True,
            )
        else:
            status.update(
                state="failed",
                phase="failed",
                message=(f"The switch was interrupted. {repaired.display_name} is healthy and was left running."),
                progress=100,
                error="The environment controller restarted while this switch was running.",
                recovered_environment=repaired,
                terminal=True,
            )
        _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, request.file_identity)
        return

    errors: list[str] = []
    try:
        with simulator_lifecycle_lock(stop_event=stop_event):
            try:
                stop_os_session(config)
            except Exception as exc:
                errors.append(_bounded_error(exc))
            try:
                stop_world_server_and_wait()
            except Exception as exc:
                errors.append(_bounded_error(exc))
    except ControllerStopping as exc:
        errors.append(_bounded_error(exc))

    with contextlib.suppress(OSError):
        write_environment_catalog(config)
    error = "The environment controller restarted while this switch was running."
    if errors:
        error += " Cleanup errors: " + "; ".join(errors)
    status.update(
        state="failed",
        phase="failed",
        message=(
            "The switch was interrupted and simulator services were stopped for safety. "
            "Run `./innate-sim up` to restore the selected environment."
        ),
        progress=100,
        error=error,
        terminal=True,
    )
    _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, request.file_identity)


def _process_request(
    config: dict[str, object],
    request: ControlRequest,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    job_id = request.job_id
    environment_id = request.environment_id
    existing = _existing_job_state(job_id, environment_id)
    job_path = ENVIRONMENT_CONTROL_JOBS_DIR / f"{job_id}.json"
    if existing is not None and existing[0] in {"ready", "failed"}:
        # Crash after terminal publication but before mailbox cleanup: the job
        # is authoritative, so only release the old request inode.
        _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, request.file_identity)
        return
    if existing is None and (job_path.exists() or job_path.is_symlink()):
        _fail_interrupted_job(config, request, stop_event=stop_event)
        return
    if existing is not None and existing[0] == "running":
        _fail_interrupted_job(config, request, stop_event=stop_event)
        return
    try:
        status = JobStatus(
            job_id=job_id,
            environment_id=environment_id,
            display_name=_target_display_name(config, environment_id),
            started_at=time.time(),
            replace_existing=existing is not None,
        )
    except FileExistsError:
        # A canonical UUID is the immutable status key. Never overwrite an old
        # job if a malformed/replayed request reuses it.
        return

    def report(phase: str, message: str, progress: int) -> None:
        status.update(state="running", phase=phase, message=message, progress=progress)

    try:
        with simulator_lifecycle_lock(stop_event=stop_event):
            selected = switch_running_environment(config, environment_id, report)
    except SwitchTransactionError as exc:
        if exc.recovered_environment is not None:
            message = f"Switch failed. {exc.recovered_environment.display_name} was restored."
        else:
            message = "Switch failed and the simulator was left stopped for safety."
        with contextlib.suppress(OSError):
            write_environment_catalog(config)
        status.update(
            state="failed",
            phase="failed",
            message=message,
            progress=100,
            error=_bounded_error(exc),
            recovered_environment=exc.recovered_environment,
            terminal=True,
        )
        _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, request.file_identity)
    except Exception as exc:
        recovered = _verified_recovery_environment(config, stop_event=stop_event)
        with contextlib.suppress(OSError):
            write_environment_catalog(config)
        status.update(
            state="failed",
            phase="failed",
            message=(
                f"Switch failed. {recovered.display_name} is still ready."
                if recovered is not None
                else "Environment switch failed before activation; no running environment was verified."
            ),
            progress=100,
            error=_bounded_error(exc),
            recovered_environment=recovered,
            terminal=True,
        )
        _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, request.file_identity)
    else:
        with contextlib.suppress(OSError):
            write_environment_catalog(config)
        status.update(
            state="ready",
            phase="ready",
            message=f"{selected.display_name} is ready.",
            progress=100,
            fingerprint=selected.fingerprint,
            terminal=True,
        )
        _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, request.file_identity)


def _write_invalid_request_status(
    config: dict[str, object],
    exc: InvalidControlRequest,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    if exc.job_id is None or exc.environment_id is None:
        return
    job_path = ENVIRONMENT_CONTROL_JOBS_DIR / f"{exc.job_id}.json"
    if job_path.exists() or job_path.is_symlink():
        existing = _existing_job_state(exc.job_id, exc.environment_id)
        if existing is not None and existing[0] in {"ready", "failed"}:
            _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, exc.file_identity)
            return
        if exc.file_identity is not None:
            _fail_interrupted_job(
                config,
                ControlRequest(
                    job_id=exc.job_id,
                    environment_id=exc.environment_id,
                    created_at=time.time(),
                    file_identity=exc.file_identity,
                ),
                stop_event=stop_event,
            )
        return
    try:
        status = JobStatus(
            job_id=exc.job_id,
            environment_id=exc.environment_id,
            display_name=_target_display_name(config, exc.environment_id),
            started_at=time.time(),
        )
    except FileExistsError:
        return
    status.update(
        state="failed",
        phase="failed",
        message="The environment switch request was rejected.",
        progress=100,
        error=_bounded_error(exc),
        terminal=True,
    )
    _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, exc.file_identity)


def _prune_job_history() -> None:
    try:
        jobs = [
            path for path in ENVIRONMENT_CONTROL_JOBS_DIR.glob("*.json") if path.is_file() and not path.is_symlink()
        ]
        jobs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in jobs[CONTROL_JOB_HISTORY_LIMIT:]:
        with contextlib.suppress(OSError):
            path.unlink()


class EnvironmentControlDaemon:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            with contextlib.suppress(OSError):
                _atomic_write_json(
                    ENVIRONMENT_CONTROL_HEARTBEAT_PATH,
                    {
                        "schema_version": CONTROL_SCHEMA_VERSION,
                        "pid": os.getpid(),
                        "updated_at": time.time(),
                    },
                )
            self.stop_event.wait(CONTROL_HEARTBEAT_SECONDS)

    def stop(self, _signum: int | None = None, _frame: object | None = None) -> None:
        self.stop_event.set()

    def run(self) -> int:
        prepare_environment_control_directories()
        with ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH.open("a+", encoding="utf-8") as singleton_lock:
            try:
                fcntl.flock(singleton_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Another simulator environment controller owns this checkout.", file=sys.stderr)
                return 1
            return self._run_as_owner()

    def _run_as_owner(self) -> int:
        _atomic_write_json(
            ENVIRONMENT_CONTROL_PID_PATH,
            {"schema_version": CONTROL_SCHEMA_VERSION, "pid": os.getpid()},
        )
        write_environment_catalog(self.config)
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="sim-control-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        last_catalog_at = time.monotonic()
        try:
            while not self.stop_event.is_set():
                if ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH.exists():
                    try:
                        request = _read_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH)
                    except TransientControlRequest:
                        pass
                    except InvalidControlRequest as exc:
                        with contextlib.suppress(Exception):
                            _write_invalid_request_status(self.config, exc, stop_event=self.stop_event)
                        _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, exc.file_identity)
                    else:
                        try:
                            _process_request(self.config, request, stop_event=self.stop_event)
                        except Exception as exc:  # keep the controller alive after a status-filesystem failure
                            print(f"Environment switch controller error: {_bounded_error(exc)}", file=sys.stderr)
                        _remove_control_request(ENVIRONMENT_CONTROL_CURRENT_REQUEST_PATH, request.file_identity)
                        _prune_job_history()
                        last_catalog_at = time.monotonic()
                if time.monotonic() - last_catalog_at >= 2.0:
                    with contextlib.suppress(OSError):
                        write_environment_catalog(self.config)
                    last_catalog_at = time.monotonic()
                self.stop_event.wait(CONTROL_POLL_SECONDS)
        finally:
            self.stop_event.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join(timeout=2.0)
            _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_PID_PATH, os.getpid())
            _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_HEARTBEAT_PATH, os.getpid())
        return 0


def _read_controller_file(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def _read_pid() -> int | None:
    value = _read_controller_file(ENVIRONMENT_CONTROL_PID_PATH)
    if value is None or set(value) != {"schema_version", "pid"}:
        return None
    pid = value.get("pid")
    if value.get("schema_version") != CONTROL_SCHEMA_VERSION or type(pid) is not int:
        return None
    return pid if 1 < pid < 2**31 else None


def _unlink_controller_file_if_owned(path: Path, pid: int) -> None:
    value = _read_controller_file(path)
    if value is not None and value.get("pid") == pid:
        with contextlib.suppress(OSError):
            path.unlink()


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _controller_singleton_lock_is_held() -> bool:
    """Prove that some live controller still owns the per-checkout lock."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(ENVIRONMENT_CONTROL_SINGLETON_LOCK_PATH, flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _controller_process_matches(pid: int) -> bool:
    """Confirm the PID is this checkout's exact ``environment_control.py serve``."""
    expected_tail = [str(Path(__file__).resolve()), "serve"]
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes()
            if len(raw) <= 64 * 1024:
                arguments = [os.fsdecode(value) for value in raw.rstrip(b"\0").split(b"\0") if value]
                return arguments[-2:] == expected_tail
        except OSError:
            return False
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "args="],
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=2.0,
        )
        arguments = shlex.split(result.stdout.strip()) if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return arguments[-2:] == expected_tail


def _heartbeat_is_fresh(pid: int) -> bool:
    value = _read_controller_file(ENVIRONMENT_CONTROL_HEARTBEAT_PATH)
    if value is None or set(value) != {"schema_version", "pid", "updated_at"}:
        return False
    updated_at = value.get("updated_at")
    if (
        value.get("schema_version") != CONTROL_SCHEMA_VERSION
        or value.get("pid") != pid
        or isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or not math.isfinite(float(updated_at))
    ):
        return False
    age = time.time() - float(updated_at)
    return -1.0 <= age <= CONTROL_HEARTBEAT_STALE_SECONDS


def ensure_environment_control_daemon(config: dict[str, object]) -> None:
    """Start the per-checkout controller and wait until its heartbeat is live."""
    prepare_environment_control_directories()
    pid = _read_pid()
    if pid is not None and _process_alive(pid) and _heartbeat_is_fresh(pid):
        try:
            if _controller_singleton_lock_is_held() and _controller_process_matches(pid):
                return
        except OSError:
            pass
    # Never signal a live PID from stale metadata: the OS may have reused that
    # number for an unrelated process. A new daemon proves exclusivity by
    # acquiring controller.lock; if the old controller really is hung, startup
    # fails safely instead of creating two lifecycle actors.
    if pid is not None and not _process_alive(pid):
        _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_PID_PATH, pid)
        _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_HEARTBEAT_PATH, pid)

    ENVIRONMENT_CONTROL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ENVIRONMENT_CONTROL_LOG_PATH.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "serve"],
            cwd=config["os_repo"],  # type: ignore[arg-type]
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + CONTROL_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _heartbeat_is_fresh(process.pid):
            return
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        process.terminate()
    raise StackError(
        "The simulator environment controller did not start.\n"
        f"Recent controller log output:\n{tail_file(ENVIRONMENT_CONTROL_LOG_PATH, limit=40)}"
    )


def request_environment_control_daemon_stop() -> int | None:
    """Signal only a PID proven by heartbeat, singleton lock, and argv."""
    pid = _read_pid()
    if pid is None:
        return None
    if _process_alive(pid) and _heartbeat_is_fresh(pid):
        try:
            lock_is_held = _controller_singleton_lock_is_held()
        except OSError as exc:
            raise StackError("Could not verify the simulator environment controller lock.") from exc
        if not lock_is_held:
            # The recorded process exited and its PID was reused while the
            # heartbeat was still within the freshness window.
            _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_PID_PATH, pid)
            _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_HEARTBEAT_PATH, pid)
            return None
        if not _controller_process_matches(pid):
            raise StackError(
                "The simulator environment controller PID does not match this checkout's controller command. "
                "Refusing to signal it."
            )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return pid
    elif _process_alive(pid):
        raise StackError(
            "The simulator environment controller PID is alive but its heartbeat is stale. "
            "Refusing to signal a potentially reused PID; inspect the environment-control log."
        )
    _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_PID_PATH, pid)
    _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_HEARTBEAT_PATH, pid)
    return None


def wait_for_environment_control_daemon_stop(pid: int | None) -> None:
    if pid is None:
        return
    deadline = time.monotonic() + CONTROL_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline and _process_alive(pid):
        time.sleep(0.05)
    if _process_alive(pid):
        raise StackError("Timed out stopping the simulator environment controller.")
    _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_PID_PATH, pid)
    _unlink_controller_file_if_owned(ENVIRONMENT_CONTROL_HEARTBEAT_PATH, pid)


def stop_environment_control_daemon() -> None:
    wait_for_environment_control_daemon_stop(request_environment_control_daemon_stop())


def main() -> int:
    if sys.argv[1:] != ["serve"]:
        print("usage: environment_control.py serve", file=sys.stderr)
        return 2
    return EnvironmentControlDaemon(get_config()).run()


if __name__ == "__main__":
    raise SystemExit(main())
