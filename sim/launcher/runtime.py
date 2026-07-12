# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from config import (
    CLI_SIM,
    CLOUD_AGENT_DIR_NAME,
    CLOUD_AGENT_LOG_PATH,
    COMPOSE_LOG_PATH,
    DOWN_LOG_PATH,
    GENERATED_OS_ENV_PATH,
    HOSTED_MODE,
    LOCAL_BRAIN_COMPOSE_PROFILE,
    LOCAL_IMAGE_MODE,
    LOCAL_MODES,
    LOCAL_OS_IMAGE_REPO,
    NONE_MODE,
    OS_BUILD_LOG_PATH,
    OS_CONTAINER_SERVICE,
    OS_CONTAINER_TMUX_CMD,
    OS_SESSION_LOG_PATH,
    OS_SESSION_READY_POLL_SECONDS,
    ROS_INSTALL_STATE_PATH,
    TMUX_SESSION_NAME,
    VIEWER_BUILD_LOG_PATH,
    WORKSPACE_ROOT,
    WORLD_SERVER_LOG_PATH,
    WORLD_SERVER_PID_PATH,
    WORLD_SERVER_PORT,
    StackError,
    compute_ros_install_validation_hash,
    ensure_state_dir,
    log,
    require_path,
    resolve_local_os_image,
    warn,
)
from dashboard import BOLD, GREEN, NC, RED, USE_COLOR

DOCKER_INSTALL_URL = "https://docs.docker.com/get-started/get-docker/"
COMPOSE_INSTALL_URL = "https://docs.docker.com/compose/install/linux/"

# A healthy daemon answers these in milliseconds; only a wedged one blocks.
# Every probe-style docker/git call carries a timeout so a hung daemon turns
# into a named error (or a degraded status) instead of silent forever-hangs.
DOCKER_PROBE_TIMEOUT_S = 15.0
PROBE_TIMEOUT_S = 30.0  # compose-exec probes: zsh -ic + ros2 daemon can take ~15s
COMPOSE_DOWN_TIMEOUT_S = 180.0


def run_logged(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    env: dict[str, str] | None = None,
    failure_message: str,
) -> None:
    ensure_state_dir()
    with log_path.open("a", encoding="utf-8") as log_file:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise StackError(f"{failure_message}\nRecent log output:\n{tail_file(log_path, limit=60)}")


def latest_log_line(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in reversed(path.read_text(errors="replace").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def run_logged_with_heartbeat(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    env: dict[str, str] | None = None,
    failure_message: str,
    progress_message: str,
    heartbeat_seconds: float = 10.0,
    include_recent_log_on_failure: bool = True,
) -> None:
    ensure_state_dir()
    with log_path.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        next_heartbeat = time.monotonic() + heartbeat_seconds
        while True:
            return_code = proc.poll()
            if return_code is not None:
                break
            now = time.monotonic()
            if now >= next_heartbeat:
                latest = latest_log_line(log_path)
                if latest:
                    log(f"{progress_message} Latest Docker activity: {latest}")
                else:
                    log(progress_message)
                next_heartbeat = now + heartbeat_seconds
            time.sleep(0.5)

    if return_code != 0:
        if not include_recent_log_on_failure:
            raise StackError(f"{failure_message}\nFull log: {log_path}")
        raise StackError(f"{failure_message}\nRecent log output:\n{tail_file(log_path, limit=60)}")


def ensure_docker_available(*, command_hint: str = CLI_SIM, require_compose: bool = True) -> None:
    """Check Docker (and, unless opted out, the Compose v2 plugin).

    `require_compose=False` is for commands that only touch an already-running
    container via plain `docker` (e.g. `sh` -> `docker exec`).
    """
    if shutil.which("docker") is None:
        raise StackError(
            "Docker is not installed or is not available on PATH.\n"
            f"Install Docker Desktop or Docker Engine: {DOCKER_INSTALL_URL}\n"
            f"Start Docker, then rerun `{command_hint}`."
        )

    try:
        result = subprocess.run(  # noqa: UP022
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise StackError(
            f"Docker did not answer `docker info` within {DOCKER_PROBE_TIMEOUT_S:.0f}s.\n"
            "The daemon looks wedged -- a stuck Docker Desktop shows exactly this (also check for a\n"
            "second Docker engine, e.g. Docker Desktop AND docker-engine inside WSL).\n"
            f"Wait for Docker to finish starting or restart it, then rerun `{command_hint}`."
        ) from None
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())
        detail_lower = detail.lower()
        daemon_unreachable = (
            "daemon" in detail_lower or "docker desktop" in detail_lower or "failed to connect" in detail_lower
        )
        message = (
            "Docker is installed, but the Docker daemon is not running or not reachable."
            if daemon_unreachable
            else "Docker is installed, but the Docker daemon check failed."
        )
        raise StackError(
            f"{message}\n"
            f"Start Docker Desktop or your Docker daemon, wait until it finishes starting, then rerun `{command_hint}`.\n"
            f"Install/start guide: {DOCKER_INSTALL_URL}"
        )

    if not require_compose:
        return

    # Compose v2 is a separate CLI plugin. On native-Linux/WSL engine installs
    # `docker` can work while `docker compose` is missing -- the whole startup
    # runs through `docker compose`, so without this it dies deep in with a
    # cryptic bare-`docker` usage error instead of a clear diagnosis.
    try:
        compose = subprocess.run(
            ["docker", "compose", "version"],
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise StackError(
            f"Docker did not answer `docker compose version` within {DOCKER_PROBE_TIMEOUT_S:.0f}s.\n"
            f"The daemon looks wedged. Restart Docker, then rerun `{command_hint}`."
        ) from None
    if compose.returncode != 0:
        # The package name follows the Docker install, not the distro version.
        raise StackError(
            "Docker is running, but Docker Compose v2 is not available (`docker compose` failed).\n"
            "Install the Compose plugin matching your Docker:\n"
            "  distro engine (docker.io): `sudo apt install docker-compose-v2` (Debian: `docker-compose`)\n"
            "  Docker's own repo (docker-ce): `sudo apt install docker-compose-plugin` (dnf/yum: same name)\n"
            "  Docker Desktop already bundles it.\n"
            f"Then rerun `{command_hint}`. Guide: {COMPOSE_INSTALL_URL}"
        )


def docker_compose_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if base_env:
        env.update(base_env)
    return env


def os_compose_env(
    base_env: dict[str, str] | None = None,
    *,
    env_file: Path = GENERATED_OS_ENV_PATH,
) -> dict[str, str]:
    values = {"INNATE_OS_ENV_FILE": str(env_file)}
    if base_env:
        values.update(base_env)
    return docker_compose_env(values)


def shorten_docker_image_ref(image: str) -> str:
    if ":" not in image:
        return image
    repo, tag = image.rsplit(":", 1)
    if tag.startswith("inputs-") and len(tag) > 22:
        tag = f"{tag[:19]}..."
    return f"{repo}:{tag}"


def ensure_os_image_available(
    image: str,
    *,
    cwd: Path,
    env: dict[str, str],
    pull_if_missing: bool,
    include_pull_log_on_failure: bool = True,
) -> None:
    if command_succeeds(["docker", "image", "inspect", image], cwd=cwd, env=env):
        return
    if not pull_if_missing:
        raise StackError(
            f"Innate OS image is not available locally: {image}\n"
            "Pull or build it, or unset sim/config.toml os.image to use the local Docker build."
        )
    log(f"Pulling Innate OS image {shorten_docker_image_ref(image)}...")
    run_logged_with_heartbeat(
        ["docker", "pull", image],
        cwd=cwd,
        env=env,
        log_path=COMPOSE_LOG_PATH,
        failure_message=(f"Could not pull the prebuilt Innate OS image: {shorten_docker_image_ref(image)}"),
        progress_message="Docker is still pulling the Innate OS image.",
        include_recent_log_on_failure=include_pull_log_on_failure,
    )


def prune_stale_local_os_images(current_image: str, *, cwd: Path, env: dict[str, str]) -> None:
    """Untag superseded local fallback images (LOCAL_OS_IMAGE_REPO:inputs-*).

    Each image-inputs change mints a new tag; without cleanup the old ones
    accumulate in `docker images` forever. Best-effort: an image still used by
    a container simply fails to untag and is left alone.
    """
    try:
        listing = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", LOCAL_OS_IMAGE_REPO],
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return
    if listing.returncode != 0:
        return
    stale = [
        ref
        for ref in listing.stdout.split()
        if ref != current_image and ref.startswith(f"{LOCAL_OS_IMAGE_REPO}:inputs-")
    ]
    pruned = 0
    for ref in stale:
        if command_succeeds(["docker", "rmi", ref], cwd=cwd, env=env):
            pruned += 1
    if pruned:
        log(f"Pruned {pruned} superseded local Innate OS image tag(s).")


# Directories the container's ROS nodes create lazily on the workspace
# bind-mount. The container runs as root: on native Linux a root mkdir lands
# on the host as root:root and locks the user out of their own skills dir
# (macOS is immune -- Docker Desktop's file sharing rewrites ownership).
WORKSPACE_USER_DIRS = ("custom_agents", "custom_skills")


def ensure_workspace_dirs(config: dict[str, object]) -> None:
    """Pre-create container-written workspace dirs as the invoking user."""
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    for name in WORKSPACE_USER_DIRS:
        path = os_repo / "workspace" / name
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # fall through to the writability warning
        if not os.access(path, os.W_OK):
            warn(
                f"{path} is not writable -- likely created as root by an earlier run. "
                f"Fix with: sudo chown -R $(id -un):$(id -gn) {path}"
            )


def ensure_os_container(config: dict[str, object], os_env_file: Path, *, offline: bool = False) -> None:
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    os_image = str(config["os_image"]).strip()
    os_image_auto = bool(config["os_image_auto"])
    container_was_running = container_running("innate-dev")

    if container_was_running:
        log("Innate OS dev container already running.")
    else:
        up_cmd = ["docker", "compose", "-f", "sim/docker-compose.dev.yml", "up", "-d"]
        if offline:
            # Reuse an image that is already local; never pull or build, which
            # would reach for the prebuilt tag and base images over the network.
            # Local builds are content-tagged (inputs-<hash>), so the compose
            # default (:latest) never exists -- resolve the real tag here.
            probe_env = os_compose_env(env_file=os_env_file)
            local_image = resolve_local_os_image(os_repo)
            if command_succeeds(["docker", "image", "inspect", local_image], cwd=os_repo, env=probe_env):
                os_image = local_image
            elif not (
                os_image and command_succeeds(["docker", "image", "inspect", os_image], cwd=os_repo, env=probe_env)
            ):
                raise StackError(
                    "Offline, but no Innate OS image is available locally (neither the local build "
                    f"{shorten_docker_image_ref(local_image)} nor a pinned prebuilt).\n"
                    f"Run `{CLI_SIM} up` online once first."
                )
            up_cmd.append("--no-build")
        elif os_image:
            compose_probe_env = os_compose_env(env_file=os_env_file)
            local_image = resolve_local_os_image(os_repo)
            if (
                os_image_auto
                and not command_succeeds(["docker", "image", "inspect", os_image], cwd=os_repo, env=compose_probe_env)
                and command_succeeds(["docker", "image", "inspect", local_image], cwd=os_repo, env=compose_probe_env)
            ):
                # No prebuilt for this exact source tree, but the deps-only local
                # image is content-current (source is bind-mounted, not baked) --
                # reuse it instead of a doomed pull + no-op rebuild.
                log(f"Reusing local Innate OS image {shorten_docker_image_ref(local_image)} (image inputs unchanged).")
                os_image = local_image
                up_cmd.append("--no-build")
            else:
                try:
                    ensure_os_image_available(
                        os_image,
                        cwd=os_repo,
                        env=compose_probe_env,
                        pull_if_missing=bool(config["os_pull_image"]),
                        include_pull_log_on_failure=not os_image_auto,
                    )
                except StackError:
                    if not os_image_auto:
                        raise
                    warn(
                        "No matching prebuilt Innate OS image is available for this checkout "
                        f"({shorten_docker_image_ref(os_image)}). Building it locally instead. "
                        f"Pull details are in {COMPOSE_LOG_PATH}."
                    )
                    os_image = local_image
                    up_cmd.append("--build")
                else:
                    up_cmd.append("--no-build")

        compose_values = {"INNATE_OS_ENV_FILE": str(os_env_file)}
        if os_image:
            compose_values["INNATE_OS_IMAGE"] = os_image
        compose_values["VIRTUAL_MARS_REMOTE"] = str(config.get("world_endpoint", "") or "")
        # Bake the brain wiring into the CONTAINER env, not just the tmux
        # launch args: an in-container `innate restart` relaunches the session
        # argless and must inherit the same backend (compose's own default
        # only fires when COMPOSE_PROFILES is set, which it isn't here).
        compose_values["BRAIN_WEBSOCKET_URI"] = str(config.get("brain_websocket_uri", "") or "")
        compose_env = os_compose_env(compose_values, env_file=os_env_file)
        log("Starting Innate OS dev container...")
        run_logged_with_heartbeat(
            up_cmd,
            cwd=os_repo,
            env=compose_env,
            log_path=COMPOSE_LOG_PATH,
            failure_message="Innate OS Docker startup failed.",
            progress_message=(
                "Docker is still preparing the Innate OS container. First boot or an image rebuild can take a minute."
            ),
        )
        prune_stale_local_os_images(resolve_local_os_image(os_repo), cwd=os_repo, env=compose_env)
    compose_values = {"INNATE_OS_ENV_FILE": str(os_env_file)}
    if os_image:
        compose_values["INNATE_OS_IMAGE"] = os_image
    compose_values["VIRTUAL_MARS_REMOTE"] = str(config.get("world_endpoint", "") or "")
    compose_env = os_compose_env(compose_values, env_file=os_env_file)
    host_repo_id = hashlib.sha256(str(os_repo.resolve()).encode("utf-8")).hexdigest()[:16]

    build_cmd = (
        f"INNATE_OS_ALWAYS_BUILD={1 if config['os_always_build'] else 0} "
        f"INNATE_OS_HOST_REPO_ID={shlex.quote(host_repo_id)} "
        "~/innate-os/scripts/validate_sim_ros_install.zsh"
    )

    ros_inputs_hash = compute_ros_install_validation_hash(os_repo)
    ros_install_marker_matches = (
        ROS_INSTALL_STATE_PATH.exists()
        and ROS_INSTALL_STATE_PATH.read_text(encoding="utf-8").strip() == ros_inputs_hash
    )
    ros_install_already_validated = False
    if ros_install_marker_matches and not bool(config["os_always_build"]):
        ros_install_already_validated = container_was_running or command_succeeds(
            os_compose_zsh_cmd("test -f ~/innate-os/ros2_ws/install/setup.zsh"),
            cwd=os_repo,
            env=compose_env,
        )
    if ros_install_already_validated:
        log("ROS workspace install already validated for this checkout.")
    else:
        log("Building / validating the ROS workspace inside the container...")
        run_logged_with_heartbeat(
            os_compose_zsh_cmd(build_cmd),
            cwd=os_repo,
            env=compose_env,
            log_path=OS_BUILD_LOG_PATH,
            failure_message="Innate OS ROS workspace build failed.",
            progress_message="Still building the ROS workspace (the first build takes a few minutes).",
        )
        ensure_state_dir()
        ROS_INSTALL_STATE_PATH.write_text(f"{ros_inputs_hash}\n", encoding="utf-8")

    log("Launching ROS simulation nodes inside the OS container...")
    brain_websocket_uri = str(config.get("brain_websocket_uri", "")).strip()
    brain_websocket_arg = ""
    if brain_websocket_uri:
        brain_websocket_arg = f" --brain-websocket-uri {shlex.quote(brain_websocket_uri)}"
    brain_client_version = str(config.get("brain_client_version", "")).strip()
    brain_client_version_arg = ""
    if brain_client_version:
        brain_client_version_arg = f" --brain-client-version {shlex.quote(brain_client_version)}"
    launch_script = (
        "INNATE_SIM_TMUX_SETTLE_SECONDS=${INNATE_SIM_TMUX_SETTLE_SECONDS:-0} "
        "INNATE_SIM_TMUX_CLEANUP_SETTLE_SECONDS=${INNATE_SIM_TMUX_CLEANUP_SETTLE_SECONDS:-0} "
        f"{OS_CONTAINER_TMUX_CMD}{brain_websocket_arg}{brain_client_version_arg}"
    )
    launch_wrapper = (
        "rm -f /tmp/innate-os-session.log; "
        f"nohup zsh -lc {shlex.quote(launch_script)} "
        ">/tmp/innate-os-session.log 2>&1 </dev/null & "
        f"for _ in {{1..60}}; do "
        f"tmux has-session -t {shlex.quote(TMUX_SESSION_NAME)} >/dev/null 2>&1 && "
        "echo 'ROS tmux session launch started.' && exit 0; "
        "sleep 0.05; "
        "done; "
        "cat /tmp/innate-os-session.log 2>/dev/null || true; "
        "exit 1"
    )
    launch_cmd = os_compose_zsh_cmd(launch_wrapper)
    run_logged(
        launch_cmd,
        cwd=os_repo,
        env=compose_env,
        log_path=OS_SESSION_LOG_PATH,
        failure_message="Innate OS tmux session launch failed.",
    )


def down_os(config: dict[str, object], *, remove_volumes: bool = False) -> None:
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = os_compose_env()
    ensure_state_dir()
    down_args = ["docker", "compose", "-f", "sim/docker-compose.dev.yml", "down"]
    if remove_volumes:
        down_args += ["-v", "--remove-orphans"]
    with DOWN_LOG_PATH.open("a", encoding="utf-8") as log_file:
        try:
            subprocess.run(
                down_args,
                cwd=os_repo,
                env=compose_env,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=COMPOSE_DOWN_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            # Bounded so a wedged daemon can't swallow the shutdown (or the
            # startup error that triggered a cleanup) in a silent hang.
            warn(
                f"`docker compose down` did not finish within {COMPOSE_DOWN_TIMEOUT_S:.0f}s -- "
                "containers may still be stopping; check `docker ps`."
            )


def clean_runtime(config: dict[str, object]) -> None:
    """Stop the runtime and delete all related Docker resources."""
    down_cloud_agent()
    log("Removing Innate OS containers, networks, and named volumes...")
    down_os(config, remove_volumes=True)
    # Belt-and-suspenders: force-remove named containers that may linger outside
    # the compose project (e.g. after a partial or failed startup).
    for container in ("innate-dev", "innate-cloud-agent"):
        subprocess.run(
            ["docker", "rm", "-f", container],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def cloud_agent_lock(config: dict[str, object]) -> dict | None:
    """The pinned cloud-agent revision this innate-os is tested against
    (sim/cloud-agent.lock), or None when unpinned/unreadable."""
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    try:
        lock = json.loads((os_repo / "sim" / "cloud-agent.lock").read_text())
        return lock if isinstance(lock.get("commit"), str) else None
    except (OSError, ValueError):
        return None


def cloud_agent_git_status(repo: Path) -> dict | None:
    """Local-only git state of a cloud-agent checkout (no network): full/short
    HEAD sha, dirty, and whether origin is the official innate repo."""

    def git(*args: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=10.0,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    try:
        sha = git("rev-parse", "HEAD")
        if sha is None:
            return None
        origin = git("remote", "get-url", "origin") or ""
        return {
            "sha": sha,
            "short": sha[:9],
            "dirty": bool(git("status", "--porcelain")),
            "official": origin.rstrip("/").removesuffix(".git").endswith("innate-inc/innate-cloud-agent"),
        }
    except (OSError, subprocess.TimeoutExpired):
        return None


def cloud_agent_checkout_pinned(repo: Path, commit: str) -> bool:
    """Fetch + detach onto the pinned revision. Callers must ensure the
    worktree is clean first."""
    for args in (["fetch", "--quiet", "origin"], ["checkout", "--quiet", "--detach", commit]):
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=120.0,
            )
        except subprocess.TimeoutExpired:
            return False
        if result.returncode != 0:
            return False
    return True


def start_cloud_agent(config: dict[str, object], cloud_env_file: Path) -> None:
    """Bring up the local brain as the `cloud-agent` service in the OS compose
    project (gated by the local-brain profile). Hosted/none modes run no local
    agent."""
    mode = config["mode"]
    if mode == HOSTED_MODE:
        log("Brain backend is hosted. Skipping local cloud-agent startup.")
        return
    if mode == NONE_MODE:
        log("No brain backend configured (no GEMINI_API_KEY or INNATE_SERVICE_KEY). Skipping cloud-agent.")
        return

    ensure_docker_available(command_hint=f"{CLI_SIM} up")
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]

    # The cloud-agent reads GEMINI_API_KEY from the same env file as the OS
    # container, and the local-brain profile must be active to start the service.
    base_env = {
        "COMPOSE_PROFILES": LOCAL_BRAIN_COMPOSE_PROFILE,
        "INNATE_OS_ENV_FILE": str(cloud_env_file),
    }
    up_cmd = ["docker", "compose", "-f", "sim/docker-compose.dev.yml", "up", "-d"]

    if mode == LOCAL_IMAGE_MODE:
        image = str(config["cloud_image"]).strip()
        if not image:
            raise StackError("sim/config.toml must set cloud_agent.image when cloud_agent.mode = 'local-image'.")
        log(f"Starting local cloud-agent image {image}...")
        compose_env = docker_compose_env({**base_env, "INNATE_CLOUD_AGENT_IMAGE": image})
        run_logged(
            [*up_cmd, "--no-build", "cloud-agent"],
            cwd=os_repo,
            env=compose_env,
            log_path=CLOUD_AGENT_LOG_PATH,
            failure_message="Local cloud-agent image startup failed.",
        )
        return

    cloud_repo: Path | None = config["cloud_repo"]  # type: ignore[assignment]
    if cloud_repo is None:
        raise StackError(
            "The innate-cloud-agent repository was not found next to innate-os "
            f"(expected at {WORKSPACE_ROOT / CLOUD_AGENT_DIR_NAME}).\n"
            f"Run `{CLI_SIM} setup` to clone it, or set sim/config.toml cloud_agent.source_dir."
        )
    require_path(cloud_repo, "innate-cloud-agent repository")
    # Surface what will actually run: a checkout off the pinned (tested)
    # revision is the #1 source of "agent crashes for me" reports. Local-only
    # check; aligning is setup's interactive job.
    status = cloud_agent_git_status(cloud_repo)
    lock = cloud_agent_lock(config)
    if status is not None and lock is not None and status["official"] and status["sha"] != lock["commit"]:
        warn(
            f"cloud-agent source is at {status['short']}, but this innate-os is tested against "
            f"{lock['commit'][:9]} -- run `{CLI_SIM} setup` to align (or ignore if intentional)."
        )
    elif status is not None and not status["official"]:
        log(
            f"Using custom cloud-agent source at {cloud_repo} ({status['short']}{', dirty' if status['dirty'] else ''})."
        )
    log(f"Starting local cloud-agent from source at {cloud_repo}...")
    compose_env = docker_compose_env({**base_env, "INNATE_CLOUD_AGENT_DIR": str(cloud_repo)})
    run_logged(
        [*up_cmd, "--build", "cloud-agent"],
        cwd=os_repo,
        env=compose_env,
        log_path=CLOUD_AGENT_LOG_PATH,
        failure_message="Local cloud-agent source startup failed.",
    )


def down_cloud_agent() -> None:
    # The cloud-agent now lives in the OS compose project, so `down_os` removes it.
    # This is a defensive cleanup in case it was started on its own.
    subprocess.run(
        ["docker", "rm", "-f", "innate-cloud-agent"],
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def tail_file(path: Path, limit: int = 40) -> str:
    if not path.exists():
        return "<no log output yet>"
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def docker_compose_cmd(*parts: str) -> list[str]:
    return ["docker", "compose", "-f", "sim/docker-compose.dev.yml", *parts]


def os_compose_exec_cmd(*parts: str) -> list[str]:
    return docker_compose_cmd("exec", "-T", OS_CONTAINER_SERVICE, *parts)


def os_compose_zsh_cmd(command: str) -> list[str]:
    return os_compose_exec_cmd("zsh", "-lc", command)


def os_compose_zsh_interactive_cmd(command: str) -> list[str]:
    """Interactive zsh so ~/.zshrc runs: that's where ROS + the zenoh RMW env
    come from -- `ros2` in a plain -lc shell probes an empty default-DDS graph."""
    return os_compose_exec_cmd("zsh", "-ic", command)


def open_os_container_shell() -> int:
    """Drop the user into an interactive zsh inside the running ROS container.

    Uses an interactive (TTY) shell so ~/.zshrc runs and sources ROS — a
    non-interactive `zsh -lc` would leave `ros2` and the workspace unsourced.
    """
    if not container_running("innate-dev"):
        raise StackError(f"Innate OS dev container is not running.\nStart it with `{CLI_SIM} up` first.")
    return subprocess.run(["docker", "exec", "-it", "innate-dev", "zsh"]).returncode


def capture_command_output(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = PROBE_TIMEOUT_S,
) -> str:
    """Probe helper: a command that outlives `timeout` (wedged Docker daemon)
    reads as no output, so status views degrade instead of freezing."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ""
    return (result.stdout or result.stderr or "").strip()


def command_succeeds(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = PROBE_TIMEOUT_S,
) -> bool:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def websocket_port_open(port: int) -> bool:
    request = (
        f"GET / HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0) as sock:
            sock.settimeout(1.0)
            sock.sendall(request)
            response = sock.recv(128)
            upgraded = response.startswith(b"HTTP/1.1 101") or response.startswith(b"HTTP/1.0 101")
            if upgraded:
                # Close the upgraded WebSocket politely (masked, empty close
                # frame) so rws logs a clean close instead of an "End of File"
                # read error from us abandoning the socket.
                try:
                    sock.sendall(b"\x88\x80\x00\x00\x00\x00")
                except OSError:
                    pass
            return upgraded
    except OSError:
        return False


def collect_os_process_status(config: dict[str, object]) -> dict[str, bool]:
    os_running = container_running("innate-dev")
    status = {
        "os_running": os_running,
        "os_session_running": False,
        "rosbridge_process_live": False,
        "brain_process_live": False,
        "sim_driver_process_live": False,
    }
    if not os_running:
        return status

    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = os_compose_env()
    output = capture_command_output(
        os_compose_zsh_cmd(
            f"tmux has-session -t {shlex.quote(TMUX_SESSION_NAME)} >/dev/null 2>&1; "
            "echo tmux=$?; "
            "pgrep -f '[r]ws_server|[r]osbridge_websocket' >/dev/null; echo rosbridge=$?; "
            "pgrep -f '[b]rain_client_node.py' >/dev/null; echo brain=$?; "
            "pgrep -f 'mars_sim_driver/[s]im_driver' >/dev/null; echo simdriver=$?"
        ),
        cwd=os_repo,
        env=compose_env,
    )
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    status["os_session_running"] = values.get("tmux") == "0"
    status["rosbridge_process_live"] = values.get("rosbridge") == "0"
    status["brain_process_live"] = values.get("brain") == "0"
    status["sim_driver_process_live"] = values.get("simdriver") == "0"
    return status


# Latch so the 0.5s dashboard refresh doesn't WS-probe rosbridge forever.
_rosbridge_ws_confirmed = False


def _rosbridge_live(os_status: dict[str, bool]) -> bool:
    """rosbridge liveness for the steady dashboard refresh.

    WS-probe the endpoint only until it first accepts a connection; afterwards
    the rws process-liveness check (pgrep) is enough. This stops opening — and
    immediately dropping — a WebSocket on every refresh, which rws logs as
    connect/EOF/disconnect noise. Reset when the process dies so a restart is
    re-probed.
    """
    global _rosbridge_ws_confirmed
    process_live = bool(os_status["os_session_running"] and os_status["rosbridge_process_live"])
    if not process_live:
        _rosbridge_ws_confirmed = False
    elif not _rosbridge_ws_confirmed:
        _rosbridge_ws_confirmed = websocket_port_open(9090)
    return process_live and _rosbridge_ws_confirmed


# On failure, kick the ros2 CLI daemon: in a fresh container it can start
# before the zenoh router and wedge with "!rclpy.ok()", failing every probe
# while the driver is actually fine. The stop makes the NEXT poll succeed.
VIRTUAL_MARS_PROBE_CMD = (
    "timeout 8 ros2 topic echo /odom --once >/dev/null 2>&1 && echo VIRTUAL_MARS_OK || ros2 daemon stop >/dev/null 2>&1"
)

# Latch so the 0.5s dashboard refresh doesn't ros2-probe /odom forever.
_virtual_mars_confirmed = False


def virtual_mars_ready(config: dict[str, object]) -> bool:
    """True if the virtual MARS driver is publishing /odom in the container."""
    if not container_running("innate-dev"):
        return False
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    output = capture_command_output(
        os_compose_zsh_interactive_cmd(VIRTUAL_MARS_PROBE_CMD),
        cwd=os_repo,
        env=os_compose_env(),
    )
    return "VIRTUAL_MARS_OK" in output


def _sim_driver_live(config: dict[str, object], os_status: dict[str, bool]) -> bool:
    """Sim driver liveness for the steady dashboard refresh.

    Probe /odom via ros2 only until it is first confirmed; afterwards the
    driver process-liveness check (pgrep) is enough. Reset when the process
    dies so a restart is re-probed.
    """
    global _virtual_mars_confirmed
    process_live = bool(os_status["os_session_running"] and os_status["sim_driver_process_live"])
    if not process_live:
        _virtual_mars_confirmed = False
    elif not _virtual_mars_confirmed:
        _virtual_mars_confirmed = virtual_mars_ready(config)
    return process_live and _virtual_mars_confirmed


def collect_runtime_probe(
    config: dict[str, object],
    *,
    sim_driver_ready: bool | None = None,
) -> dict[str, object]:
    os_status = collect_os_process_status(config)
    sim_running = bool(sim_driver_ready) if sim_driver_ready is not None else _sim_driver_live(config, os_status)
    rosbridge_live = _rosbridge_live(os_status)
    agent_running = container_running("innate-cloud-agent") if config["mode"] in LOCAL_MODES else True
    return {
        "os_status": os_status,
        "sim_running": sim_running,
        "rosbridge_live": rosbridge_live,
        "agent_running": agent_running,
    }


def os_runtime_ready(config: dict[str, object]) -> bool:
    os_status = collect_os_process_status(config)
    return (
        os_status["os_session_running"]
        and os_status["rosbridge_process_live"]
        and os_status["brain_process_live"]
        and websocket_port_open(9090)
    )


def wait_for_os_runtime_ready(config: dict[str, object], *, timeout_seconds: float = 8.0) -> bool:
    deadline = time.time() + timeout_seconds
    next_report = time.time() + 15.0
    while time.time() < deadline:
        if os_runtime_ready(config):
            return True
        if time.time() >= next_report:  # long waits must not read as a hang
            log(f"Still waiting for the ROS bridge and brain client... ({int(deadline - time.time())}s remaining)")
            next_report = time.time() + 15.0
        time.sleep(OS_SESSION_READY_POLL_SECONDS)
    return False


def wait_for_virtual_mars(config: dict[str, object], *, timeout_seconds: float = 180.0) -> bool:
    """True once the virtual MARS driver is publishing /odom in the container."""
    deadline = time.time() + timeout_seconds
    while True:
        if virtual_mars_ready(config):
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        log(f"Waiting for the sim driver (/odom)... ({int(remaining)}s remaining)")
        time.sleep(min(3.0, remaining))


def runtime_already_running(config: dict[str, object]) -> bool:
    if not os_runtime_ready(config):
        return False
    if config["mode"] in LOCAL_MODES and not container_running("innate-cloud-agent"):
        return False
    return True


def format_startup_check(ok: bool, label: str, detail: str) -> str:
    icon = "✓" if ok else "✗"
    color = GREEN if ok else RED
    return f"  {color}{icon}{NC} {BOLD}{label}:{NC} {detail}"


def print_startup_checks(
    config: dict[str, object],
    *,
    sim_driver_ready: bool,
) -> None:
    probe = collect_runtime_probe(config, sim_driver_ready=sim_driver_ready)
    os_status: dict[str, bool] = probe["os_status"]  # type: ignore[assignment]
    checks = [
        (
            bool(probe["agent_running"]),
            "Cloud agent",
            "hosted mode" if config["mode"] == HOSTED_MODE else "local container",
        ),
        (
            os_status["os_running"],
            "OS container",
            "running" if os_status["os_running"] else "down",
        ),
        (
            os_status["os_session_running"],
            "ROS session",
            "tmux session running" if os_status["os_session_running"] else "missing",
        ),
        (
            bool(probe["rosbridge_live"]),
            "ROSBridge",
            "ws://localhost:9090 live" if probe["rosbridge_live"] else "not accepting connections",
        ),
        (
            os_status["brain_process_live"],
            "Brain process",
            "brain_client_node.py running" if os_status["brain_process_live"] else "brain_client_node.py missing",
        ),
        (
            sim_driver_ready,
            "Sim driver (MuJoCo)",
            "/odom publishing" if sim_driver_ready else "/odom not publishing",
        ),
    ]

    log("Startup checks:")
    for ok, label, detail in checks:
        print(format_startup_check(ok, label, detail))


def capture_os_brain_logs(config: dict[str, object], lines: int = 18) -> list[str]:
    if not container_running("innate-dev"):
        return ["OS container offline."]
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = os_compose_env()
    capture_flags = "-e -J -p" if USE_COLOR else "-J -p"
    output = capture_command_output(
        os_compose_zsh_cmd(
            f"if ! tmux has-session -t {shlex.quote(TMUX_SESSION_NAME)} >/dev/null 2>&1; then "
            "echo __INNATE_NO_TMUX_SESSION__; "
            "exit 0; "
            "fi; "
            f"tmux capture-pane {capture_flags} -t {shlex.quote(TMUX_SESSION_NAME)}:nav-brain.1 -S -{lines} 2>/dev/null || true"
        ),
        cwd=os_repo,
        env=compose_env,
    )
    if "__INNATE_NO_TMUX_SESSION__" in output:
        recent_launch_output = tail_file(OS_SESSION_LOG_PATH, limit=max(lines - 3, 4))
        return [
            "OS tmux session is not running.",
            "The ROS stack did not finish launching inside the container.",
            f"Check: {CLI_SIM} logs os-session",
            *recent_launch_output.splitlines(),
        ][:lines]
    if not output:
        return ["No OS brain output yet."]
    return output.splitlines()[-lines:]


def capture_agent_logs(config: dict[str, object], lines: int = 18) -> list[str]:
    if config["mode"] == HOSTED_MODE:
        return ["Hosted mode enabled.", "No local agent container is running."]
    if config["mode"] == NONE_MODE:
        return ["No brain backend configured.", "The sim is running without an agent."]
    if not container_running("innate-cloud-agent"):
        return ["Local cloud-agent is not running."]
    output = capture_command_output(["docker", "logs", "--tail", str(lines), "innate-cloud-agent"])
    if not output:
        return ["No local cloud-agent output yet."]
    return output.splitlines()[-lines:]


def health_score(level: str) -> float:
    if level == "healthy":
        return 100.0
    if level == "warn":
        return 60.0
    return 20.0


# Directories wholly owned by the published asset bundle: each is replaced
# atomically on refresh. work/* land under sim/assets/, viewer/* under
# sim/viewer/. Must stay in sync with sim/tools/publish_assets.py's layout.
SIM_ASSET_UNITS = [
    "work/apartment_split",
    "work/apartment_split_v2",
    "work/apartment_visual",
    "work/sdf_shells",
    "work/map",
    # (The bundle's viewer/public/physics/apartment_sdf is demo-only and not
    # extracted; the hulls feed the webapp's "collisions" overlay.)
    "viewer/public/physics/apartment_collisions_v2",
    "viewer/public/models",
    "viewer/public/robot",
    "viewer/assets/apartment_obj",
    # Built SimSession bundle: ships prebuilt so `up` needs no Node.js
    # (absent from pre-dist-lib bundles; the extractor skips missing units).
    "viewer/dist-lib",
]


def ensure_sim_assets(config: dict[str, object]) -> None:
    """Download + extract the sim asset bundle pinned by sim/sim-assets.lock.

    The generated geometry (collision hulls, room meshes, nav map, GLB/STLs)
    lives in a GitHub release, not in git. Extracting in place -- work/ into
    sim/assets/, viewer/ into sim/viewer/ -- keeps every existing consumer
    (compose mounts, webapp routes, tools) pointed at unchanged paths.
    Idempotent via the .assets-tag marker; tools/publish_assets.py bumps the
    lock and the next `up` refreshes.
    """
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    lock = json.loads((sim_repo / "sim-assets.lock").read_text())
    marker = sim_repo / "assets" / ".assets-tag"
    if marker.exists() and marker.read_text().strip() == lock["tag"]:
        return

    log(f"Downloading sim assets {lock['tag']} (~100 MB, one-time)...")
    tarball = sim_repo / ".sim-assets.tmp.tar.gz"
    try:
        with urlopen(Request(lock["url"]), timeout=600) as resp, open(tarball, "wb") as out:
            total_mb = int(resp.headers.get("Content-Length") or 0) >> 20
            done = 0
            next_report = time.monotonic() + 5.0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if time.monotonic() >= next_report:  # slow networks: progress, not silence
                    log(f"Downloading sim assets... {done >> 20} MB" + (f" of {total_mb} MB" if total_mb else ""))
                    next_report = time.monotonic() + 5.0
    except (URLError, OSError) as exc:
        tarball.unlink(missing_ok=True)
        raise StackError(f"Failed to download sim assets from {lock['url']}: {exc}") from exc

    sha = hashlib.sha256()
    with open(tarball, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    if digest != lock["sha256"]:
        tarball.unlink()
        raise StackError(f"sim asset bundle checksum mismatch (got {digest}, lock says {lock['sha256']})")

    staging = sim_repo / ".sim-assets.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with tarfile.open(tarball) as tar:
            for member in tar.getmembers():
                if member.name.startswith(("/", "..")) or ".." in Path(member.name).parts:
                    raise StackError(f"unsafe path in asset bundle: {member.name}")
                # The publisher only emits regular files; links could escape
                # the staging dir during extractall, so reject them outright.
                if not (member.isfile() or member.isdir()):
                    raise StackError(f"unsupported member type in asset bundle: {member.name}")
            tar.extractall(staging)
        for unit in SIM_ASSET_UNITS:
            src = staging / unit
            if not src.is_dir():
                continue
            top, rel = unit.split("/", 1)
            dest = (sim_repo / "assets" / rel) if top == "work" else (sim_repo / "viewer" / rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(src), str(dest))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(lock["tag"] + "\n")
        log(f"Sim assets {lock['tag']} installed.")
    finally:
        tarball.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def ensure_sim_viewer_bundle(config: dict[str, object], *, offline: bool = False) -> None:
    """Make sure the webapp's 3D-view bundle (viewer/dist-lib) is usable.

    The webapp loads /sim-viewer/sim-session.js for the 3D sim view. The
    asset bundle ships it prebuilt (publish_assets.py), so running the sim
    never needs Node.js. A missing/empty bundle means a broken asset state
    (e.g. an interrupted first run), and the fix is re-fetching the pinned
    assets -- never a local npm build. Viewer developers opt in to
    build-from-source with INNATE_SIM_VIEWER_DEV=1. Never runs on robots.
    """
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    viewer = sim_repo / "viewer"
    bundle = viewer / "dist-lib" / "sim-session.js"

    if os.environ.get("INNATE_SIM_VIEWER_DEV", "").strip() == "1":
        _build_viewer_bundle_from_source(viewer, bundle, offline=offline)
        return

    if bundle.is_file() and bundle.stat().st_size > 0:
        return

    if offline:
        warn("The prebuilt sim viewer bundle is missing and this run is offline -- no 3D webapp view.")
        return
    warn("The prebuilt sim viewer bundle is missing or empty -- re-fetching the sim assets...")
    (sim_repo / "assets" / ".assets-tag").unlink(missing_ok=True)
    ensure_sim_assets(config)
    if not (bundle.is_file() and bundle.stat().st_size > 0):
        # Only reachable with a pre-dist-lib asset bundle pinned.
        warn(
            "The pinned asset bundle does not include a prebuilt viewer (no 3D webapp view). "
            "Viewer developers can build one from source with INNATE_SIM_VIEWER_DEV=1 (needs Node.js)."
        )


def _build_viewer_bundle_from_source(viewer: Path, bundle: Path, *, offline: bool) -> None:
    """INNATE_SIM_VIEWER_DEV=1: rebuild dist-lib when viewer sources are
    newer than it (the edit-run loop for sim/viewer developers)."""
    npm = shutil.which("npm")
    if npm is None:
        raise StackError(
            "INNATE_SIM_VIEWER_DEV=1 needs npm on PATH (install Node.js), "
            "or unset it to use the prebuilt viewer bundle."
        )
    sources = [viewer / "package.json", viewer / "vite.lib.config.ts", viewer / "tsconfig.json"]
    sources += sorted((viewer / "src").rglob("*.ts"))
    if bundle.exists():
        newest_src = max((p.stat().st_mtime for p in sources if p.exists()), default=0.0)
        if bundle.stat().st_mtime >= newest_src:
            return
    if not (viewer / "node_modules").is_dir():
        if offline:
            warn("Offline and sim/viewer/node_modules is missing -- skipping the viewer bundle build.")
            return
        log("Installing sim viewer npm dependencies (one-time)...")
        run_logged(
            [npm, "ci"],
            cwd=viewer,
            log_path=VIEWER_BUILD_LOG_PATH,
            failure_message="npm ci failed for sim/viewer.",
        )
    log("Building the sim viewer bundle (dist-lib)...")
    run_logged_with_heartbeat(
        [npm, "run", "build:lib"],
        cwd=viewer,
        log_path=VIEWER_BUILD_LOG_PATH,
        failure_message="Sim viewer bundle build failed (npm run build:lib).",
        progress_message="Still building the sim viewer bundle.",
    )


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _world_server_ping_reply(port: int, timeout: float = 2.0) -> dict | None:
    """The server's ping reply (advertises state_port), or None."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
            payload = json.dumps({"op": "ping"}).encode()
            conn.sendall(len(payload).to_bytes(4, "big") + payload)
            header = _recv_exact(conn, 4)
            if header is None:
                return None
            body = _recv_exact(conn, int.from_bytes(header, "big"))
            if body is None:
                return None
            reply = json.loads(body)
            return reply if reply.get("ok") is True else None
    except (OSError, ValueError):
        return None


def _world_server_ping(port: int, timeout: float = 2.0) -> bool:
    return _world_server_ping_reply(port, timeout) is not None


def _stop_stale_world_server() -> None:
    """SIGTERM whatever owns the RPC port (the PID file only covers servers
    this checkout started)."""
    stop_world_server()
    out = subprocess.run(
        ["lsof", "-ti", f"tcp:{WORLD_SERVER_PORT}", "-sTCP:LISTEN"], capture_output=True, text=True, check=False
    )
    for pid in out.stdout.split():
        with contextlib.suppress(ProcessLookupError, OSError, ValueError):
            os.kill(int(pid), signal.SIGTERM)
    for _ in range(20):
        if not _world_server_ping(WORLD_SERVER_PORT, timeout=0.5):
            return
        time.sleep(0.25)
    warn("A previous world server is still holding the port; the new one may fail to bind.")


def ensure_world_server(config: dict[str, object]) -> str:
    """Start the host world server (physics + native-GL rendering outside
    Docker -- see mars_sim_driver/world_server.py) and return the endpoint
    the container should use, or "" for in-container rendering.

    Docker has no GPU on macOS/Windows, so in-container renders run on
    software GL at ~105ms/frame; the host renders the same frame in ~15ms.
    Default: on for macOS, off elsewhere; INNATE_SIM_HOST_WORLD=1/0 overrides.
    Requires uv on the host (for the mujoco environment); falls back to
    in-container rendering with a warning when unavailable.
    """
    override = os.environ.get("INNATE_SIM_HOST_WORLD", "").strip()
    enabled = override == "1" if override else sys.platform == "darwin"
    if not enabled:
        return ""
    if shutil.which("uv") is None:
        warn("uv not found -- running the sim world in-container (software GL). Install uv for native-speed rendering.")
        return ""

    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    endpoint = f"host.docker.internal:{WORLD_SERVER_PORT}"
    reply = _world_server_ping_reply(WORLD_SERVER_PORT)
    if reply is not None:
        if reply.get("state_port"):
            log("Host world server already running.")
            return endpoint
        # Pre-stream server: reusing it would starve the webapp's 3D view.
        log("Host world server is outdated (no observer state stream) -- restarting it...")
        _stop_stale_world_server()

    log("Starting host world server (native GL rendering)...")
    ensure_state_dir()
    bootstrap = (
        "import sys; sys.path.insert(0, 'ros2_ws/src/mars_bot/mars_sim_driver'); "
        "from mars_sim_driver.world_server import main; main()"
    )
    env = os.environ.copy()
    env["VIRTUAL_MARS_ASSETS"] = str(sim_repo / "assets")
    with WORLD_SERVER_LOG_PATH.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            ["uv", "run", "--project", str(sim_repo), "python", "-c", bootstrap],
            cwd=sim_repo.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    WORLD_SERVER_PID_PATH.write_text(f"{proc.pid}\n", encoding="utf-8")
    for _ in range(60):  # model build takes a few seconds
        if _world_server_ping(WORLD_SERVER_PORT):
            log("Host world server ready.")
            return endpoint
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    warn(
        "Host world server failed to start -- running the sim world in-container instead. "
        f"Details: {WORLD_SERVER_LOG_PATH}"
    )
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    WORLD_SERVER_PID_PATH.unlink(missing_ok=True)
    return ""


def stop_world_server() -> None:
    if not WORLD_SERVER_PID_PATH.exists():
        return
    try:
        pid = int(WORLD_SERVER_PID_PATH.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        log("Stopped host world server.")
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    WORLD_SERVER_PID_PATH.unlink(missing_ok=True)


def ensure_skill_assets(config: dict[str, object]) -> None:
    """Download skill assets declared in each skill's metadata.json.

    The sim shares the repo workspace/ via bind mount but never runs the
    hardware post_update.sh, which is where these assets are normally fetched.
    Mirrors that step here so sim skills have their downloads present.
    Idempotent: assets already on disk are skipped.
    """
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    workspace = sim_repo.parent / "workspace"
    meta_files = sorted(workspace.glob("innate_skills/*/metadata.json")) + sorted(
        workspace.glob("custom_skills/*/metadata.json")
    )
    for meta_file in meta_files:
        try:
            downloads = json.loads(meta_file.read_text()).get("downloads") or {}
        except (json.JSONDecodeError, OSError):
            continue
        for fname, url in downloads.items():
            dest = meta_file.parent / fname
            if dest.exists():
                continue
            log(f"Downloading skill asset {dest.relative_to(workspace)}...")
            tmp = meta_file.parent / f"{fname}.tmp"
            try:
                with urlopen(Request(url), timeout=300) as resp, open(tmp, "wb") as out:
                    shutil.copyfileobj(resp, out)
                tmp.replace(dest)
            except (URLError, OSError) as exc:
                tmp.unlink(missing_ok=True)
                raise StackError(f"Failed to download skill asset {dest}: {exc}") from exc


def container_running(container_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False  # wedged daemon: report down rather than hang the caller
    return result.returncode == 0 and result.stdout.strip() == "true"


def collect_status_snapshot(config: dict[str, object]) -> dict[str, object]:
    probe = collect_runtime_probe(config)
    os_status: dict[str, bool] = probe["os_status"]  # type: ignore[assignment]
    os_running = os_status["os_running"]
    os_session_running = os_status["os_session_running"]
    rosbridge_process_live = os_status["rosbridge_process_live"]
    brain_process_live = os_status["brain_process_live"]
    agent_running = bool(probe["agent_running"])
    sim_running = bool(probe["sim_running"])
    rosbridge_live = bool(probe["rosbridge_live"])

    sim_level, sim_label = ("healthy", "ok") if sim_running else ("error", "down")
    transport_level, transport_label = ("healthy", "live") if rosbridge_live else ("error", "down")
    if not os_running:
        brain_level = "error"
        brain_label = "offline"
    elif not os_session_running:
        brain_level = "error"
        brain_label = "session missing"
    elif not rosbridge_process_live:
        brain_level = "warn"
        brain_label = "rosbridge down"
    elif not brain_process_live:
        brain_level = "warn"
        brain_label = "brain booting"
    elif rosbridge_live:
        brain_level = "healthy"
        brain_label = "ros ready"
    else:
        brain_level = "warn"
        brain_label = "booting"
    if config["mode"] == NONE_MODE:
        agent_level, agent_label = "warn", "none"
    elif config["mode"] == HOSTED_MODE:
        agent_level, agent_label = "healthy", "hosted"
    else:
        agent_level = "healthy" if agent_running else "warn"
        agent_label = "online" if agent_running else "offline"

    if all(level == "healthy" for level in (sim_level, transport_level, brain_level, agent_level)):
        stack_mood = ("healthy", "LIVE")
    elif any(level == "error" for level in (sim_level, transport_level, brain_level)):
        stack_mood = ("error", "DEGRADED")
    else:
        stack_mood = ("warn", "WARMING")

    system_summary = (
        f"os {'up' if os_running else 'down'} | "
        f"sim {'up' if sim_running else 'down'} | "
        f"brain {'ok' if brain_level == 'healthy' else brain_label}"
    )

    return {
        "os_running": os_running,
        "os_session_running": os_session_running,
        "agent_running": agent_running,
        "sim_running": sim_running,
        "rosbridge_live": rosbridge_live,
        "rosbridge_process_live": rosbridge_process_live,
        "brain_process_live": brain_process_live,
        "sim_level": sim_level,
        "sim_label": sim_label,
        "transport_level": transport_level,
        "transport_label": transport_label,
        "brain_level": brain_level,
        "brain_label": brain_label,
        "agent_level": agent_level,
        "agent_label": agent_label,
        "stack_level": stack_mood[0],
        "stack_label": stack_mood[1],
        "health_score": health_score(stack_mood[0]),
        "system_summary": system_summary,
    }
