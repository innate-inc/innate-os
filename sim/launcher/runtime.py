# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import oci
from config import (
    ASSETS_IMAGE_LAYERS,
    BENCHMARK_LOG_PATH,
    BENCHMARK_PID_PATH,
    BENCHMARK_PORT,
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
    NONE_MODE,
    OS_BUILD_LOG_PATH,
    OS_CONTAINER_SERVICE,
    OS_CONTAINER_TMUX_CMD,
    OS_SESSION_LOG_PATH,
    OS_SESSION_READY_POLL_SECONDS,
    REPO_ROOT,
    ROS_INSTALL_STATE_PATH,
    SIM_ASSET_UNITS,
    SIM_ASSET_UNITS_AUTHORED,
    SIM_ASSET_UNITS_DERIVED,
    TMUX_SESSION_NAME,
    VIEWER_BUILD_LOG_PATH,
    VIEWER_TREE_PATH,
    WORKSPACE_ROOT,
    WORLD_SERVER_LOG_PATH,
    WORLD_SERVER_MODEL_DIGEST_PATH,
    WORLD_SERVER_PID_PATH,
    WORLD_SERVER_PORT,
    DockerUnresponsiveError,
    StackError,
    compute_ros_install_validation_hash,
    ensure_state_dir,
    log,
    require_path,
    resolve_assets_image,
    resolve_local_os_image,
    resolve_local_viewer_image,
    resolve_viewer_image,
    viewer_tree_dirty,
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
    # Heartbeats must describe THIS command: reading the whole shared log
    # would parrot stale lines from the previous command (a failed pull's
    # 'unauthorized' haunted the subsequent build's heartbeats).
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    started = time.monotonic()
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
                latest = ""
                with contextlib.suppress(OSError):
                    appended = log_path.read_text(errors="replace")[log_offset:]
                    latest = next((line.strip() for line in reversed(appended.splitlines()) if line.strip()), "")
                elapsed = int(now - started)
                stamp = f"{elapsed // 60}m{elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
                if latest:
                    log(f"{progress_message} ({stamp}) Latest: {latest}")
                else:
                    log(f"{progress_message} ({stamp}, no output yet)")
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

    Requiring compose also requires versions new enough for the viewer's
    `type: image` mount: docker-compose.dev.yml carries those mounts
    unconditionally, so an old Compose fails at parse time on every verb, not
    just `up`.
    """
    if shutil.which("docker") is None:
        if sys.platform == "darwin":
            install_help = (
                f"Install Docker Desktop:  brew install --cask docker\n(or download it: {DOCKER_INSTALL_URL})"
            )
        else:
            install_help = (
                "On Ubuntu (including WSL):    sudo apt install docker.io docker-compose-v2\n"
                "On Debian / Raspberry Pi OS:  curl -fsSL https://get.docker.com | sudo sh\n"
                "then let your user talk to Docker:\n"
                "  sudo usermod -aG docker $USER && newgrp docker    # newgrp applies it to this shell\n"
                f"Other platforms: {DOCKER_INSTALL_URL}"
            )
        raise StackError(
            f"Docker is not installed or is not available on PATH.\n{install_help}\nThen rerun `{command_hint}`."
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
            "The docker daemon looks stuck -- a frozen Docker Desktop shows exactly this (also check\n"
            "for a second Docker engine, e.g. Docker Desktop AND docker-engine inside WSL).\n"
            f"Wait for Docker to finish starting or restart it, then rerun `{command_hint}`."
        ) from None
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "").split())
        detail_lower = detail.lower()
        if "permission denied" in detail_lower:
            # The daemon runs fine; this user just isn't in the docker group
            # yet (the usual state right after `apt install docker.io`).
            raise StackError(
                "Docker is running, but your user is not allowed to talk to it (permission denied "
                "on the Docker socket).\n"
                "  sudo usermod -aG docker $USER && newgrp docker    # newgrp applies it to this shell\n"
                f"Then rerun `{command_hint}` in that shell (new logins have it everywhere)."
            )
        daemon_unreachable = (
            "daemon" in detail_lower or "docker desktop" in detail_lower or "failed to connect" in detail_lower
        )
        message = (
            "Docker is installed, but the Docker daemon is not running or not reachable."
            if daemon_unreachable
            else "Docker is installed, but the Docker daemon check failed."
        )
        start_help = (
            "Open Docker Desktop and wait until it finishes starting"
            if sys.platform == "darwin"
            else "Start it with `sudo systemctl start docker` (or open Docker Desktop if you use it)"
        )
        raise StackError(f"{message}\n{start_help}, then rerun `{command_hint}`.")

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
            f"The docker daemon looks stuck. Restart Docker, then rerun `{command_hint}`."
        ) from None
    if compose.returncode != 0:
        # The package name follows the Docker install, not the distro version.
        raise StackError(
            "Docker is running, but Docker Compose v2 is not available (`docker compose` failed).\n"
            "Install the Compose plugin matching your Docker:\n"
            "  Ubuntu's engine (docker.io): `sudo apt install docker-compose-v2`\n"
            "  Docker's own repo (docker-ce): `sudo apt install docker-compose-plugin` (dnf/yum: same name)\n"
            "  Debian / Raspberry Pi OS: switch to Docker's repo: `curl -fsSL https://get.docker.com | sudo sh`\n"
            "    (Debian's `docker-compose` package is the old v1 tool, not the plugin)\n"
            "  Docker Desktop already bundles it.\n"
            f"Then rerun `{command_hint}`. Guide: {COMPOSE_INSTALL_URL}"
        )

    # `type: image` needs BOTH a new Compose and a new daemon -- the engine
    # materialises the mount, so a new plugin in front of an old dockerd fails
    # at `up`. Both versions were probed above for liveness; reuse them.
    _require_min_version(
        "Docker Engine",
        result.stdout,
        (28, 0),
        "Update Docker (Desktop: update the app; Linux: reinstall docker-ce from Docker's\nrepo)",
        command_hint,
        DOCKER_INSTALL_URL,
    )
    # Older Compose rejects the whole file with a schema error naming a field,
    # not a version.
    _require_min_version(
        "Docker Compose",
        compose.stdout,
        (2, 35),
        "Update the Compose plugin (Docker Desktop: update the app; Linux: reinstall\n"
        "docker-compose-v2 / docker-compose-plugin from Docker's repo)",
        command_hint,
        COMPOSE_INSTALL_URL,
    )


def _require_min_version(
    label: str,
    version_output: str | None,
    minimum: tuple[int, int],
    remedy: str,
    command_hint: str,
    guide_url: str,
) -> None:
    """Fail with an actionable message if `version_output` reports older than
    `minimum`. Silent when the version cannot be parsed: an unrecognised build
    string is not evidence of an old one, and a false refusal to start is worse
    than the raw error this pre-empts."""
    found = re.search(r"v?(\d+)\.(\d+)", version_output or "")
    if not found or (int(found.group(1)), int(found.group(2))) >= minimum:
        return
    raise StackError(
        f"{label} {found.group(0)} is too old: the sim mounts its viewer assets straight from "
        f"an image (`type: image`), which needs {label} {minimum[0]}.{minimum[1]} or newer.\n"
        f"{remedy}, then rerun `{command_hint}`.\nGuide: {guide_url}"
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


def _docker_platform() -> str | None:
    """The host's linux/<arch> platform for docker pulls, so a wrong-arch
    prebuilt fails in milliseconds instead of downloading gigabytes it can
    never run (classic-store docker happily pulls cross-arch and only fails
    at container start with 'exec format error' -- seen on a Raspberry Pi
    pulling the amd64-only prebuilt)."""
    arch = oci.host_arch()
    return f"linux/{arch}" if arch else None


IMAGE_PROBE_RETRY_TIMEOUT_S = 60.0


def docker_image_present(image: str, *, cwd: Path, env: dict[str, str]) -> bool:
    """Strict image-presence probe for pull/build decisions.

    Unlike `command_succeeds`, an unanswered probe is an error rather than a
    "no": misreading a merely-slow daemon as a missing image escalates into a
    multi-gigabyte pull or a full local rebuild (seen on a loaded Raspberry Pi
    right after a `docker load`)."""
    timeout_s = DOCKER_PROBE_TIMEOUT_S
    for timeout_s in (DOCKER_PROBE_TIMEOUT_S, IMAGE_PROBE_RETRY_TIMEOUT_S):
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            continue
        return result.returncode == 0
    raise DockerUnresponsiveError(
        f"Docker did not answer `docker image inspect` within {timeout_s:.0f}s.\n"
        "The docker daemon looks stuck or overloaded, so image availability is unknown -- "
        "not pulling or rebuilding on a guess.\n"
        f"Wait for Docker to settle (or restart it), then rerun `{CLI_SIM} up`."
    )


def ensure_os_image_available(
    image: str,
    *,
    cwd: Path,
    env: dict[str, str],
    pull_if_missing: bool,
    include_pull_log_on_failure: bool = True,
) -> None:
    if docker_image_present(image, cwd=cwd, env=env):
        return
    if not pull_if_missing:
        raise StackError(
            f"Innate OS image is not available locally: {image}\n"
            "Pull or build it, or unset sim/config.toml os.image to use the local Docker build."
        )
    log(f"Pulling Innate OS image {shorten_docker_image_ref(image)}...")
    plat = _docker_platform()
    run_logged_with_heartbeat(
        ["docker", "pull", *(["--platform", plat] if plat else []), image],
        cwd=cwd,
        env=env,
        log_path=COMPOSE_LOG_PATH,
        failure_message=(f"Could not pull the prebuilt Innate OS image: {shorten_docker_image_ref(image)}"),
        progress_message="Docker is still pulling the Innate OS image.",
        include_recent_log_on_failure=include_pull_log_on_failure,
    )


def prune_stale_local_images(current_image: str, *, cwd: Path, env: dict[str, str], label: str) -> None:
    """Untag superseded local builds sharing `current_image`'s repo.

    Both local builds -- the OS fallback and the viewer bundle -- are tagged
    `<repo>:inputs-<hash>`, so each input change mints a new tag and strands
    the old one in `docker images` forever.

    The repo comes off `current_image` rather than a second argument: sweeping
    a different repo than the one being kept is the only way this can be wrong.
    rpartition takes the LAST colon, so a registry:port survives.

    Best-effort: an image still used by a container simply fails to untag and
    is left alone.
    """
    repo = current_image.rpartition(":")[0]
    try:
        listing = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", repo],
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
    stale = [ref for ref in listing.stdout.split() if ref != current_image and ref.startswith(f"{repo}:inputs-")]
    pruned = 0
    for ref in stale:
        if command_succeeds(["docker", "rmi", ref], cwd=cwd, env=env):
            pruned += 1
    if pruned:
        log(f"Pruned {pruned} superseded local {label} image tag(s).")


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
    ensure_home_mount_sources()


def ensure_home_mount_sources() -> None:
    """Pre-create the compose file's home bind-mount sources (~/.gitconfig,
    ~/.ssh). Docker creates a missing bind source as a root-owned DIRECTORY,
    which for ~/.gitconfig breaks git on the host itself ("unable to access
    '~/.gitconfig': Is a directory" -- seen on a fresh machine)."""
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.is_dir():
        try:
            gitconfig.rmdir()  # only succeeds when empty, i.e. Docker-made
            warn(f"Removed the empty directory Docker created at {gitconfig} (it broke `git` on this machine).")
        except OSError:
            warn(f"{gitconfig} is a non-empty directory -- git expects a file there; please fix it manually.")
            return
    if not gitconfig.exists():
        with contextlib.suppress(OSError):
            gitconfig.touch()
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        with contextlib.suppress(OSError):
            ssh_dir.mkdir(mode=0o700)


def ensure_os_container(config: dict[str, object], os_env_file: Path, *, offline: bool = False) -> None:
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    os_image = str(config["os_image"]).strip()
    os_image_auto = bool(config["os_image_auto"])
    container_was_running = container_running("innate-dev")

    if container_was_running:
        log("Innate OS dev container already running.")
    else:
        up_cmd = docker_compose_cmd("up", "-d")
        if offline:
            # Reuse an image that is already local; never pull or build, which
            # would reach for the prebuilt tag and base images over the network.
            # Local builds are content-tagged (inputs-<hash>), so the compose
            # default (:latest) never exists -- resolve the real tag here.
            probe_env = os_compose_env(env_file=os_env_file)
            local_image = resolve_local_os_image(os_repo)
            if docker_image_present(local_image, cwd=os_repo, env=probe_env):
                os_image = local_image
            elif not (os_image and docker_image_present(os_image, cwd=os_repo, env=probe_env)):
                raise StackError(
                    "Offline, but no Innate OS image is available locally (neither the local build "
                    f"{shorten_docker_image_ref(local_image)} nor a pinned prebuilt).\n"
                    f"Run `{CLI_SIM} up` online once first."
                )
            # The viewer mount is an IMAGE, not a bind: compose resolves it
            # even with --pull never, so offline needs it in the local store.
            assets_image = assets_image_ref(config)
            if not docker_image_present(assets_image, cwd=os_repo, env=probe_env):
                raise StackError(
                    "Offline, but the sim asset image is not in the local Docker store:\n"
                    f"  {shorten_docker_image_ref(assets_image)}\n"
                    "The webapp's 3D view mounts the viewer straight from it, so compose cannot "
                    f"start without it.\nRun `{CLI_SIM} up` online once first."
                )
            up_cmd.append("--no-build")
        elif os_image:
            compose_probe_env = os_compose_env(env_file=os_env_file)
            local_image = resolve_local_os_image(os_repo)
            try:
                # The prebuilt (local or pulled) always wins: it carries the
                # baked ROS install, so choosing anything else when it exists
                # silently costs a full colcon build -- hours on a weak
                # machine. In particular the local deps-image shortcut below
                # must NOT preempt the pull: CI publishes prebuilts for branch
                # trees (multi-arch), so "no prebuilt locally" no longer
                # implies "no prebuilt anywhere".
                ensure_os_image_available(
                    os_image,
                    cwd=os_repo,
                    env=compose_probe_env,
                    pull_if_missing=bool(config["os_pull_image"]),
                    include_pull_log_on_failure=not os_image_auto,
                )
            except DockerUnresponsiveError:
                raise  # unknown availability must not trigger a local rebuild
            except StackError:
                if not os_image_auto:
                    raise
                if docker_image_present(local_image, cwd=os_repo, env=compose_probe_env):
                    # No prebuilt anywhere for this exact source tree, but the
                    # deps-only local image is content-current (source is
                    # bind-mounted, not baked) -- reuse it instead of building.
                    log(
                        f"No prebuilt image for this checkout; reusing local "
                        f"{shorten_docker_image_ref(local_image)} (image inputs unchanged)."
                    )
                    os_image = local_image
                    up_cmd.append("--no-build")
                else:
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
        # The viewer's public assets (models, physics) mount straight off this
        # image. The bundle has its own, below.
        compose_values["INNATE_SIM_ASSETS_IMAGE"] = assets_image_ref(config)
        # Published or locally built; ensure_sim_viewer_bundle has made sure
        # whichever it is exists.
        compose_values["INNATE_SIM_VIEWER_BUNDLE_IMAGE"] = viewer_image_ref(config)
        compose_values["VIRTUAL_MARS_REMOTE"] = str(config.get("world_endpoint", "") or "")
        # Bake the brain wiring into the CONTAINER env, not just the tmux
        # launch args: an in-container `innate restart` relaunches the session
        # argless and must inherit the same backend (compose's own default
        # only fires when COMPOSE_PROFILES is set, which it isn't here).
        compose_values["BRAIN_WEBSOCKET_URI"] = str(config.get("brain_websocket_uri", "") or "")
        # Foxglove bridge publishes on host 8765 (Foxglove Studio's default) —
        # but a local brain's cloud-agent owns that host port, so shift the
        # bridge to 8766 unless the user pinned a port themselves.
        if not os.environ.get("SIM_FOXGLOVE_PORT", "").strip() and config["mode"] not in (HOSTED_MODE, NONE_MODE):
            compose_values["SIM_FOXGLOVE_PORT"] = "8766"
            log("Local brain owns port 8765; Foxglove bridge published on ws://localhost:8766.")
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
        prune_stale_local_images(resolve_local_os_image(os_repo), cwd=os_repo, env=compose_env, label="Innate OS")
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
    with contextlib.suppress(OSError):  # read-only fs: keep tearing down
        ensure_state_dir()
    down_args = docker_compose_cmd("down")
    if remove_volumes:
        down_args += ["-v", "--remove-orphans"]
    try:
        log_file = DOWN_LOG_PATH.open("a", encoding="utf-8")
    except OSError:
        # Cleanup must survive an unwritable disk (seen live: a full disk
        # flipped the filesystem read-only mid-startup) -- losing the down
        # log beats crashing while tearing down.
        log_file = None
    try:
        subprocess.run(
            down_args,
            cwd=os_repo,
            env=compose_env,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file if log_file is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_file is not None else subprocess.DEVNULL,
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
    finally:
        if log_file is not None:
            log_file.close()


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


def _os_container_holds_host_8765() -> bool:
    """True when a running innate-dev publishes its Foxglove port on host 8765
    (a container created under hosted/no-brain mode, before a local-brain up
    would have shifted the publish to 8766)."""
    try:
        result = subprocess.run(
            ["docker", "port", "innate-dev", "8765"],
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and any(line.strip().endswith(":8765") for line in result.stdout.splitlines())


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
    # An OS container that predates this local-brain up may still publish the
    # Foxglove bridge on host 8765 (this runs before ensure_os_container, and a
    # running container is not recreated anyway). The cloud-agent host publish
    # is debug-only — the brain reaches it over the compose network — so shift
    # it clear rather than failing the bind (8767: 8766 is Foxglove's own
    # local-brain port, live again as soon as the OS container is recreated).
    if not os.environ.get("SIM_CLOUD_AGENT_PORT", "").strip() and _os_container_holds_host_8765():
        base_env["SIM_CLOUD_AGENT_PORT"] = "8767"
        log("Host port 8765 is held by the running OS container (Foxglove); publishing cloud-agent on 8767.")
    up_cmd = docker_compose_cmd("up", "-d")

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


@functools.lru_cache(maxsize=1)
def viewer_bundle_built_locally() -> bool:
    """Build the bundle image here instead of pulling the published one.

    True when the working tree under sim/viewer differs from HEAD: the bundle's
    tag hashes that tree as it is on disk, so an edited tree names an image CI
    cannot have published, and the registry probe can be skipped.

    Cached: asked repeatedly within one command, and cannot change in-process.
    """
    try:
        return viewer_tree_dirty(REPO_ROOT)
    except (OSError, subprocess.CalledProcessError, StackError):
        # No git, or not a checkout (a release tarball). The published bundle
        # is then the only thing that could work anyway.
        return False


def docker_compose_cmd(*parts: str) -> list[str]:
    # One compose file for every invocation: dist-lib is always an image mount,
    # only WHICH image varies (see viewer_image_ref), so there is no overlay to
    # apply on `up` and forget on every later subcommand.
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


def world_server_health(*, timeout: float = 2.0) -> tuple[bool, str]:
    """(ok, detail) for the host world server: is it answering, and what is
    the measured render cost. The server logs one parseable line at boot --
    "GL self-test (<backend>): <N> ms/frame ..." -- which is the ground
    truth for render speed (an EGL backend on a GPU-less machine is just as
    software-slow as OSMesa, so report the measurement, not the backend)."""
    if not _world_server_ping(WORLD_SERVER_PORT, timeout=timeout):
        return False, f"not answering on 127.0.0.1:{WORLD_SERVER_PORT}"
    detail = "ready"
    try:
        matches = re.findall(
            r"GL self-test(?: \((\w+)\))?: (\d+) ms/frame", WORLD_SERVER_LOG_PATH.read_text(errors="replace")
        )
    except OSError:
        matches = []
    if matches:
        backend, ms = matches[-1]
        detail = f"ready -- {backend or 'native'} GL, {ms} ms/frame"
        if int(ms) > 60:
            detail += " (software-speed rendering)"
    return True, detail


def print_startup_checks(
    config: dict[str, object],
    *,
    sim_driver_ready: bool,
) -> bool:
    """Print the checks panel; returns whether the world server answered
    (its death during boot must end `up` loudly, not as a quiet dashboard)."""
    probe = collect_runtime_probe(config, sim_driver_ready=sim_driver_ready)
    os_status: dict[str, bool] = probe["os_status"]  # type: ignore[assignment]
    world_ok, world_detail = world_server_health()
    if not world_ok:
        time.sleep(2.0)  # a saturated box can miss one ping; don't cry wolf
        world_ok, world_detail = world_server_health()
    checks = [
        (
            bool(probe["agent_running"]),
            "Cloud agent",
            "hosted mode" if config["mode"] == HOSTED_MODE else "local container",
        ),
        (world_ok, "World server", world_detail),
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
            "Sim driver",
            "/odom publishing" if sim_driver_ready else "/odom not publishing",
        ),
    ]

    log("Startup checks:")
    for ok, label, detail in checks:
        print(format_startup_check(ok, label, detail))
    return world_ok


def capture_os_brain_logs(config: dict[str, object], lines: int = 18) -> list[str]:
    if not container_running("innate-dev"):
        return ["OS container offline."]
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = os_compose_env()
    capture_flags = "-e -J -p" if USE_COLOR else "-J -p"
    # Plain sh: tmux needs no ROS env, and this runs every dashboard tick --
    # a login zsh would re-source the whole profile each time.
    output = capture_command_output(
        os_compose_exec_cmd(
            "sh",
            "-c",
            f"if ! tmux has-session -t {shlex.quote(TMUX_SESSION_NAME)} >/dev/null 2>&1; then "
            "echo __INNATE_NO_TMUX_SESSION__; "
            "exit 0; "
            "fi; "
            f"tmux capture-pane {capture_flags} -t {shlex.quote(TMUX_SESSION_NAME)}:nav-brain.1 -S -{lines} 2>/dev/null || true",
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


# The subtrees the host installs out of the asset image's `work` layer are
# config.SIM_ASSET_UNITS, shared with ci/seed_asset_context.py. Each is
# replaced atomically on refresh, and all land under sim/assets/ for the world
# server, which runs on the HOST and writes into them (.model_cache, capped
# textures) -- so they cannot be mounted read-only off the image instead.
# The viewer's dirs are absent because compose does exactly that with them
# (sim/docker-compose.dev.yml).


def assets_image_ref(config: dict[str, object]) -> str:
    """The asset image compose mounts the viewer subtree from.

    COMPUTED, not looked up: the tag is content-addressed over the tracked
    inputs, so it names exactly the image this checkout implies -- the same way
    resolve_auto_os_image names the ROS image.

    INNATE_SIM_ASSETS_IMAGE overrides, for testing an image built elsewhere. It
    moves BOTH the compose mount and ensure_sim_assets' fetch -- manifest, token
    and blob all name the same repository, or the digest read out of one image
    is requested from another and 404s. So an override has to name something
    the registry can serve; an image that exists only in the local Docker store
    fails at the manifest probe.
    """
    override = os.environ.get("INNATE_SIM_ASSETS_IMAGE", "").strip()
    return override or resolve_assets_image(config["os_repo"])  # type: ignore[arg-type]


def ensure_sim_assets(config: dict[str, object]) -> None:
    """Install the sim geometry out of the asset image this checkout implies.

    The generated geometry (collision hulls, room meshes, nav map) is not in
    git; it ships as one layer of ghcr.io/innate-inc/innate-os-sim-assets.
    Only that layer is fetched -- ~85 MB compressed rather than the whole
    image -- over plain HTTPS, so this needs no Docker installed.

    WHICH layer is positional: a manifest names none of them, so the geometry
    is layers[ASSETS_IMAGE_LAYERS.index("work")], and ci/verify_assets_image.py
    asserts that order against every pushed image.

    Extracted in place under sim/assets/, idempotent via the .assets-tag marker.
    """
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    marker = sim_repo / "assets" / ".assets-tag"
    image = assets_image_ref(config)

    # The marker holds "<digest> <image ref>". Keyed on the geometry layer's
    # digest, not the tag: the tag moves whenever any tracked input changes,
    # so re-extracting 168 MB for a viewer-source edit would be waste.
    #
    # Checked against what is on disk too, since the digest only records what
    # this host MEANT to install: a hand-deleted subtree reinstalls instead of
    # being asserted complete forever.
    #
    # DERIVED only. The authored units are the ones a pinned layer may
    # legitimately predate (see the warn below), so demanding them here would
    # leave `installed` false forever and re-fetch the layer on every `up`.
    # Recovering a hand-deleted authored unit means deleting .assets-tag.
    parts = marker.read_text().split() if marker.exists() else []
    installed = all((sim_repo / "assets" / unit).is_dir() for unit in SIM_ASSET_UNITS_DERIVED)

    # Ref match => digest match, so the warm path stays off the network: the
    # ref is content-addressed and ci/build_assets_image.sh never rebuilds an
    # existing tag. NOT valid for an INNATE_SIM_ASSETS_IMAGE override, which
    # may name a mutable tag -- those probe the manifest every time.
    if not os.environ.get("INNATE_SIM_ASSETS_IMAGE", "").strip() and parts[1:] == [image] and installed:
        return

    try:
        manifest = oci.manifest_for_image(image)
    except oci.OciError as exc:
        # Two different mistakes: the checkout implies a tag nobody built, or
        # the override names something the registry will not serve. Saying
        # "set INNATE_SIM_ASSETS_IMAGE" to someone who just did is no help.
        if os.environ.get("INNATE_SIM_ASSETS_IMAGE", "").strip():
            raise StackError(
                f"The registry did not serve INNATE_SIM_ASSETS_IMAGE ({shorten_docker_image_ref(image)}): {exc}\n"
                f"The geometry is fetched over the registry API, so an override has to name a pushed "
                f"image -- one that exists only in the local Docker store cannot be read here."
            ) from exc
        raise StackError(
            f"No published sim asset image for this checkout ({shorten_docker_image_ref(image)}): {exc}\n"
            f"Editing anything the image is built from renames it. Push the branch so CI publishes "
            f"it, or set INNATE_SIM_ASSETS_IMAGE to one that exists."
        ) from exc
    digest = manifest["layers"][ASSETS_IMAGE_LAYERS.index("work")]["digest"]

    if parts[:1] == [digest] and installed:
        # Same geometry under a new ref (or an old digest-only marker):
        # remember the ref so the next run skips the probe above.
        marker.write_text(f"{digest} {image}\n")
        return

    log(f"Downloading sim assets {digest[7:19]} (~85 MB, one-time)...")
    blob = sim_repo / ".sim-assets.tmp.tar.gz"
    staging = sim_repo / ".sim-assets.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        # The repository the manifest came from, never a hardcoded one: the
        # digest was read out of THAT image and exists nowhere else.
        repo, _ = oci.split_ref(image)
        with open(blob, "wb") as out:
            oci.fetch_layer(repo, digest, out, oci.anon_token(repo), label="sim assets")
        oci.safe_extract(blob, staging)
        work = staging / "work"
        # Fatal before anything is installed, rather than writing a marker that
        # claims success: a store without apartment_split_v2 has no collision
        # hulls at all, and would silently short-circuit every later `up`.
        missing = [unit for unit in SIM_ASSET_UNITS_DERIVED if not (work / unit).is_dir()]
        if missing:
            raise StackError(
                f"The pinned geometry layer {digest[7:19]} is missing {missing}.\n"
                "Refusing to install a partial store -- the world server cannot run without it."
            )
        # Authored props are additive: a checkout can legitimately expect ones
        # the pinned layer predates, and a world without them still runs.
        absent = [unit for unit in SIM_ASSET_UNITS_AUTHORED if not (work / unit).is_dir()]
        if absent:
            warn(f"The pinned geometry predates {absent}; the world will load without them.")

        # Stamp one install time so every file reads as arriving now (buildx
        # does not normalise layer mtimes without SOURCE_DATE_EPOCH). NOTE this
        # is what invalidates the driver's compiled world below: its cache key
        # is those very mtimes (mars_sim_driver.core._model_cache_path).
        installed_at = time.time()
        try:
            for extracted in staging.rglob("*"):
                os.utime(extracted, (installed_at, installed_at))
        except OSError as exc:
            raise StackError(f"Failed to stamp sim asset mtimes under {staging}: {exc}") from exc

        for unit in SIM_ASSET_UNITS:
            src = work / unit
            if not src.is_dir():
                continue
            dest = sim_repo / "assets" / unit
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(src), str(dest))
        # Every compiled world is now unreachable (see the re-stamp above) and
        # nothing else ever prunes them, at ~168 MB each. .model_cache sits
        # OUTSIDE the units, so the per-unit replacement leaves it behind.
        shutil.rmtree(sim_repo / "assets" / ".model_cache", ignore_errors=True)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{digest} {image}\n")
        log(f"Sim assets {digest[7:19]} installed.")
    finally:
        blob.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def viewer_image_ref(config: dict[str, object]) -> str:
    """The image compose mounts dist-lib from.

    Published `inputs-<content hash of sim/viewer>` normally, or the local build
    when the working tree has diverged from HEAD.
    INNATE_SIM_VIEWER_BUNDLE_IMAGE overrides both, for a hand-built one.

    ensure_sim_viewer_bundle records its choice in the config, because one case
    cannot be decided without the network: a clean tree whose publish has not
    landed falls back to the local build too.
    """
    override = os.environ.get("INNATE_SIM_VIEWER_BUNDLE_IMAGE", "").strip()
    if override:
        return override
    chosen = config.get("viewer_bundle_image")
    if chosen:
        return str(chosen)
    repo_root: Path = config["os_repo"]  # type: ignore[assignment]
    if viewer_bundle_built_locally():
        return resolve_local_viewer_image(repo_root)
    return resolve_viewer_image(repo_root)


def ensure_sim_viewer_bundle(config: dict[str, object], *, offline: bool = False) -> None:
    """Make sure the image holding the webapp's 3D-view bundle exists.

    Published image when one describes this checkout, otherwise built right
    here from sim/viewer/Dockerfile. Two ways to end up building:

      * the tree is dirty, so no published image CAN describe it
      * the tree is clean but CI has not published it yet (or retention expired
        a branch tag) -- worth a warning, not worth blocking on

    Either way the bundle arrives as a mounted image, so the host needs Docker
    and never Node.js. Never runs on robots.
    """
    if os.environ.get("INNATE_SIM_VIEWER_BUNDLE_IMAGE", "").strip():
        # An explicit override names the image; building a different one and
        # mounting neither would be the one useless outcome.
        return
    if not viewer_bundle_built_locally() and _published_bundle_usable(config, offline=offline):
        return
    _build_viewer_image_locally(config, offline=offline)


def _build_viewer_image_locally(config: dict[str, object], *, offline: bool) -> None:
    """Build sim/viewer/Dockerfile into LOCAL_VIEWER_IMAGE_REPO:inputs-<hash>.

    Skipped when that tag is already in the store: the tag hashes the build's
    inputs off the working tree, so its presence means the bundle for exactly
    these bytes exists.

    Offline is fine on a repeat run and fatal on the first: npm ci inside the
    image needs the network, and there is no published bundle to fall back to.

    Records the ref in the config so viewer_image_ref reports what was actually
    built -- recomputing there would be wrong on the clean-tree fallback path,
    where a registry probe chose the local build.
    """
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    image = resolve_local_viewer_image(os_repo)
    config["viewer_bundle_image"] = image
    env = os_compose_env()
    if docker_image_present(image, cwd=os_repo, env=env):
        return
    # The two ways to get here have different remedies: stash your own edit, or
    # wait for CI to finish publishing.
    why = "sim/viewer differs from HEAD" if viewer_bundle_built_locally() else "nothing is published for it"
    if offline:
        raise StackError(
            f"Offline, and the sim viewer bundle has to be built ({why}, so no published image "
            "describes this checkout).\n"
            f"  {image}\n"
            "The build installs npm dependencies, which needs a connection. Re-run online, or "
            "check out a commit whose bundle you have already built."
        )
    log(f"Building the sim viewer bundle image ({why})...")
    run_logged_with_heartbeat(
        [
            "docker",
            "buildx",
            "build",
            "--file",
            f"{VIEWER_TREE_PATH}/Dockerfile",
            # Host arch only, and loaded into the store rather than pushed:
            # this one serves exactly this machine.
            "--provenance=false",
            "--sbom=false",
            "--tag",
            image,
            "--load",
            ".",
        ],
        cwd=os_repo,
        env=env,
        log_path=VIEWER_BUILD_LOG_PATH,
        failure_message=(
            "Building the sim viewer bundle image failed.\n"
            "Stash your sim/viewer changes to fall back to the published bundle."
        ),
        progress_message="Still building the sim viewer bundle image.",
    )
    # Every edit to sim/viewer mints a new tag, so these pile up faster than
    # any other local image in the project.
    prune_stale_local_images(image, cwd=os_repo, env=env, label="sim viewer bundle")


def _published_bundle_usable(config: dict[str, object], *, offline: bool) -> bool:
    """Can compose mount the published bundle for this checkout?

    False means "build it here instead" -- the caller's fallback. The warning
    matters: a permanently broken publish pipeline would otherwise be
    invisible, since every user would just quietly build their own.

    Probed here and NOT in viewer_bundle_built_locally, which is asked on paths
    that must not touch the network. Records the chosen ref in the config, as
    _build_viewer_image_locally does.
    """
    image = resolve_viewer_image(config["os_repo"])  # type: ignore[arg-type]
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    if docker_image_present(image, cwd=os_repo, env=os_compose_env()):
        config["viewer_bundle_image"] = image
        return True
    if offline:
        # Fatal, not a fallback: the local build needs the network too (npm ci).
        raise StackError(
            "Offline, and the sim viewer bundle image is not in the local Docker store:\n"
            f"  {image}\n"
            f"Run `{CLI_SIM} up` online once first."
        )
    if _viewer_image_published(image):
        config["viewer_bundle_image"] = image
        return True
    warn(
        f"No published sim viewer bundle for this commit ({image}).\n"
        "  CI may still be publishing it (publish-viewer-bundle.yml, about a minute). "
        "Building it locally with the same Dockerfile in the meantime."
    )
    return False


def _viewer_image_published(image: str) -> bool:
    """Does the registry serve `image` to an anonymous client?

    The registry API rather than `docker manifest inspect`, because the
    anonymous HTTPS path is the one users actually pull on -- so this also
    catches GHCR making a brand-new package private, where every fetch 401s.
    """
    try:
        oci.manifest_for_image(image)
    except oci.OciError:
        return False
    return True


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


UV_INSTALL_COMMAND = "curl -LsSf https://astral.sh/uv/install.sh | sh"
_UV_MISSING_MESSAGE = (
    "uv is required: it runs the sim world (MuJoCo physics + rendering) on the host, where it is "
    "~7x faster than in Docker.\n"
    f"Install it (user-local, no sudo):  {UV_INSTALL_COMMAND}\n"
    f"or rerun `{CLI_SIM} setup`, which offers to install it for you."
)


def find_uv() -> str | None:
    """Path to uv, checking PATH plus the official installer's default
    locations (a just-installed uv is often not on PATH yet)."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def ensure_uv_available() -> None:
    """Prerequisite gate for commands that need the host world server."""
    if find_uv() is None:
        raise StackError(_UV_MISSING_MESSAGE)


def _world_server_bind_addresses() -> str:
    """Addresses the host world server should listen on (comma-separated),
    or "" when no host-only bind can be determined (callers fail closed with
    a hard error -- the unauthenticated sim ports must never be opened to
    the LAN by default).

    Docker Desktop (macOS) reaches the host loopback via host.docker.internal,
    so loopback alone suffices. A native Linux/WSL engine resolves it to the
    default bridge gateway instead -- bind that too. The gateway IP is owned
    by the host and not routable from the LAN, so nothing is exposed beyond
    the host and its containers.
    """
    if sys.platform == "darwin":
        return "127.0.0.1"
    gateway = capture_command_output(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        timeout=DOCKER_PROBE_TIMEOUT_S,
    )
    parts = gateway.split(".")
    if len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts):
        return f"127.0.0.1,{gateway}"
    return ""


def world_server_running() -> bool:
    """Whether a host world server answers on the driver port."""
    return _world_server_ping_reply(WORLD_SERVER_PORT) is not None


def _world_model_sources_digest(config: dict[str, object]) -> str:
    """Content digest of the sources compiled into the world server's MuJoCo
    model: the robot description, the driver's model-building modules, and
    the installed apartment bundle (its .assets-tag marker is derived from
    the bundle tarball's sha256, so the tag names the content). A running
    server that compiled different content is serving stale physics -- by
    CONTENT, not mtime, so no copy, checkout or asset refresh can fool it."""
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    mars_bot = os_repo / "ros2_ws" / "src" / "mars_bot"
    driver = mars_bot / "mars_sim_driver" / "mars_sim_driver"
    candidates = sorted((mars_bot / "mars_sim" / "urdf").glob("*"))
    candidates += sorted((mars_bot / "mars_sim" / "meshes").glob("*"))
    candidates += [driver / name for name in ("world.py", "core.py", "constants.py")]
    candidates += [sim_repo / "assets" / ".assets-tag"]
    digest = hashlib.sha256()
    for f in candidates:
        with contextlib.suppress(OSError):
            digest.update(f.name.encode())
            digest.update(f.read_bytes())
    return digest.hexdigest()


def ensure_world_server(config: dict[str, object]) -> str:
    """Start the host world server (physics + rendering, outside Docker --
    see mars_sim_driver/world_server.py) and return the endpoint the
    container must use.

    The world ALWAYS runs on the host: in-container software GL measured
    ~105ms/frame with physics starving the ROS stack (multi-second teleop
    stalls on laptop-class machines), so there is no in-container fallback
    -- every failure here is a hard error naming its fix.

    On a native Linux/WSL Docker engine, host.docker.internal resolves to
    the Docker bridge gateway rather than the host loopback, so there the
    server also binds that gateway IP (host-owned, not LAN-routable).
    """
    uv = find_uv()
    if uv is None:
        raise StackError(_UV_MISSING_MESSAGE)

    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    endpoint = f"host.docker.internal:{WORLD_SERVER_PORT}"
    bind = os.environ.get("INNATE_SIM_WORLD_BIND", "").strip() or _world_server_bind_addresses()
    if not bind:
        # Fail closed: no host-only bind must never widen to 0.0.0.0.
        raise StackError(
            "Could not determine the Docker bridge gateway (the address containers use to reach "
            "the host world server). This usually means a nonstandard Docker setup (podman, "
            "`bridge: none`).\nSet INNATE_SIM_WORLD_BIND to the address the server should listen on."
        )

    reply = _world_server_ping_reply(WORLD_SERVER_PORT)
    if reply is not None:
        expected_binds = {b.strip() for b in bind.split(",") if b.strip()}
        actual_binds = reply.get("binds")
        if reply.get("state_port") and actual_binds is not None and set(actual_binds) == expected_binds:
            # The MuJoCo model is compiled at server start; a URDF or
            # world-module edit since then is not in the running physics.
            running_digest = ""
            with contextlib.suppress(OSError):
                running_digest = WORLD_SERVER_MODEL_DIGEST_PATH.read_text(encoding="utf-8").strip()
            if _world_model_sources_digest(config) == running_digest:
                log("Host world server already running.")
                return endpoint
            log("Host world server compiled different robot/world sources -- restarting it...")
        # Reusing a mismatched server would either starve the webapp's 3D
        # view (pre-stream builds) or keep listeners open that the current
        # bind policy would never create (e.g. a leftover
        # INNATE_SIM_WORLD_BIND=0.0.0.0 server) -- restart instead.
        elif not reply.get("state_port"):
            log("Host world server is outdated (no observer state stream) -- restarting it...")
        elif actual_binds is None:
            log("Host world server predates bind reporting -- restarting it...")
        else:
            log(
                f"Host world server listens on {','.join(actual_binds)} but the current policy "
                f"wants {bind} -- restarting it..."
            )
        _stop_stale_world_server()

    ensure_state_dir()
    attempts: list[tuple[str, str, str]] = []  # (backend label, backend, that attempt's log output)
    user_gl = os.environ.get("MUJOCO_GL", "").strip()
    backends = _world_server_gl_backends()
    healed_venv = False
    index = 0
    while index < len(backends):
        backend = backends[index]
        labels = {"egl": "EGL offscreen", "osmesa": "software (OSMesa)"}
        label = labels.get(backend or user_gl, f"MUJOCO_GL={user_gl}" if user_gl else "native GL")
        if backend == "osmesa":
            warn("Falling back to software rendering (OSMesa) -- works on any machine, but renders are slow.")
        log(f"Starting host world server ({label} rendering)...")
        log_offset = WORLD_SERVER_LOG_PATH.stat().st_size if WORLD_SERVER_LOG_PATH.exists() else 0
        if _start_world_server(uv, sim_repo, bind=bind, mujoco_gl=backend):
            # Record what this server compiled, for the reuse check above.
            WORLD_SERVER_MODEL_DIGEST_PATH.write_text(_world_model_sources_digest(config) + "\n", encoding="utf-8")
            log("Host world server ready.")
            return endpoint
        attempt_log = ""
        with contextlib.suppress(OSError):
            attempt_log = WORLD_SERVER_LOG_PATH.read_text(errors="replace")[log_offset:]
        if "modulenotfounderror" in attempt_log.lower() and not healed_venv:
            # Not a GL problem: the sim venv is half-installed (an interrupted
            # first install). The launcher owns that directory -- rebuild it
            # and retry the same backend instead of blaming the renderer.
            healed_venv = True
            warn("The sim Python environment is incomplete (an interrupted install) -- rebuilding it...")
            shutil.rmtree(sim_repo / ".venv", ignore_errors=True)
            continue
        attempts.append((label, backend, attempt_log))
        if index < len(backends) - 1:
            # Say WHY the faster rung failed before falling to the next one:
            # a GPU owner one apt package away from fast rendering must not
            # silently end up on the software floor.
            warn(f"{label} rendering failed: {_gl_failure_hint(attempt_log, backend)}")
        index += 1

    hint_list = [_gl_failure_hint(text, backend) for _, backend, text in attempts]
    if any("sim/.venv" in hint for hint in hint_list):
        # Even a fresh rebuild missed a dependency: an environment problem,
        # not a rendering one -- do not dress it up as a GL error.
        raise StackError(
            "The sim Python environment failed to build cleanly (a dependency did not import even "
            "after a fresh install).\n"
            f"Try `rm -rf sim/.venv` and rerun `{CLI_SIM} up`; if it persists, share the log on Discord.\n"
            f"Full log: {WORLD_SERVER_LOG_PATH}"
        )
    hints = "\n".join(f"  - {label}: {hint}" for (label, _, _), hint in zip(attempts, hint_list, strict=True))
    fix = ""
    if sys.platform != "darwin":
        # One action, no backend to choose: install everything the render
        # rungs need and the ladder picks the best one by itself.
        fix = (
            f"Run the following, then rerun `{CLI_SIM} up`:\n  sudo apt install libegl1 libgl1 libopengl0 libosmesa6\n"
        )
    raise StackError(
        f"No working rendering backend for the sim world (tried {', '.join(label for label, _, _ in attempts)}).\n"
        f"{fix}"
        f"Details:\n{hints}\n"
        f"Full log: {WORLD_SERVER_LOG_PATH}"
    )


def _world_server_gl_backends() -> list[str | None]:
    """MUJOCO_GL values to try, in order (None = leave the environment
    alone: native GL on macOS, GLFW where a display exists).

    A user-set MUJOCO_GL is respected verbatim. On headless Linux GLFW
    cannot work (it needs a display server), so the ladder goes straight to
    EGL (GPU) and then OSMesa (CPU) -- each attempt is logged, so a slow
    software fallback is always a loud, visible choice."""
    if os.environ.get("MUJOCO_GL", "").strip():
        return [None]  # user's explicit choice, already in the environment
    if sys.platform == "darwin":
        return [None]
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return [None, "egl", "osmesa"]  # desktop/WSLg first, headless routes as backup
    return ["egl", "osmesa"]


def _render_scale_args() -> list[str]:
    """INNATE_SIM_RENDER_SCALE=N renders cameras at 1/N resolution -- an N^2
    cheaper software frame on machines where the render cost starves the rest
    of the stack (a Pi at scale 1 spends ~420 ms/frame). The wire format stays
    640x480, so nothing downstream changes."""
    raw = os.environ.get("INNATE_SIM_RENDER_SCALE", "").strip()
    if not raw:
        return []
    try:
        scale = int(raw)
    except ValueError:
        scale = 0
    if scale < 1:
        warn(f"Ignoring INNATE_SIM_RENDER_SCALE={raw!r} (expected an integer >= 1).")
        return []
    return ["--render-scale", str(scale)]


def _start_world_server(uv: str, sim_repo: Path, *, bind: str, mujoco_gl: str | None) -> bool:
    """One world-server start attempt; True once it answers pings."""
    bootstrap = (
        "import sys; sys.path.insert(0, 'ros2_ws/src/mars_bot/mars_sim_driver'); "
        "from mars_sim_driver.world_server import main; main()"
    )
    env = os.environ.copy()
    env["VIRTUAL_MARS_ASSETS"] = str(sim_repo / "assets")
    if mujoco_gl:
        env["MUJOCO_GL"] = mujoco_gl
    with WORLD_SERVER_LOG_PATH.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [uv, "run", "--project", str(sim_repo), "python", "-c", bootstrap, "--bind", bind] + _render_scale_args(),
            cwd=sim_repo.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    WORLD_SERVER_PID_PATH.write_text(f"{proc.pid}\n", encoding="utf-8")
    # Patient while alive: the first run downloads the Python env (uv sync)
    # and builds the MuJoCo model, which takes minutes on slow machines --
    # killing a live process on a stopwatch misdiagnosed a Raspberry Pi's
    # env download as a GL failure. Heartbeats keep the wait visible.
    deadline = time.monotonic() + 900.0
    next_note = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _world_server_ping(WORLD_SERVER_PORT):
            return True
        if proc.poll() is not None:
            return False  # exited on its own: a real failure, log captured
        if time.monotonic() >= next_note:
            latest = latest_log_line(WORLD_SERVER_LOG_PATH)
            log(f"World server still starting... ({latest or 'no output yet'})")
            next_note = time.monotonic() + 15.0
        time.sleep(0.5)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    WORLD_SERVER_PID_PATH.unlink(missing_ok=True)
    WORLD_SERVER_MODEL_DIGEST_PATH.unlink(missing_ok=True)
    return False


def _gl_failure_hint(attempt_log: str, backend: str | None) -> str:
    """One targeted line per failed GL backend -- a fresh user must see the
    fix, not a Python traceback. Signatures are matched case-insensitively:
    the telltale often only appears in lowercase module paths.

    The most common minimal-system failure is PyOpenGL holding no GL
    library at all ("'NoneType' object has no attribute 'glGetError'"):
    under EGL the GL symbols come from libGL/libOpenGL (apt: libgl1,
    libopengl0 -- libegl1 alone is just the dispatch layer), under OSMesa
    they come from libOSMesa itself (apt: libosmesa6)."""
    lowered = attempt_log.lower()
    if "blank image" in lowered:
        return (
            "the GPU created a context but renders nothing usable (out of GPU memory) -- "
            "software rendering is the correct backend on this machine"
        )
    if "modulenotfounderror" in lowered:
        # A dependency missing from the sim venv itself: the signature of an
        # interrupted first install (seen live on a Pi whose early attempts
        # were cut short mid `uv sync`). Not a GL problem.
        return "the sim Python environment is incomplete -- delete sim/.venv and rerun (it rebuilds automatically)"
    no_gl_library = "glgeterror" in lowered and "nonetype" in lowered
    if backend == "osmesa" or (backend is None and "osmesa" in lowered):
        if no_gl_library or "osmesa" in lowered:
            return "the OSMesa system library is missing -- install it with `sudo apt install libosmesa6`"
    if backend == "egl" or "egl" in lowered:
        if no_gl_library:
            return (
                "EGL loads but there is no OpenGL library behind it -- "
                "install it with `sudo apt install libgl1 libopengl0`"
            )
        return "EGL is unavailable -- `sudo apt install libegl1` provides it (a GPU driver makes it fast)"
    if "DISPLAY" in attempt_log:
        return "no display server is running (on WSL, `wsl --update` enables WSLg)"
    for line in reversed(attempt_log.splitlines()):
        if line.strip():
            return line.strip()
    return "no output -- see the full log"


def stop_world_server() -> None:
    if not WORLD_SERVER_PID_PATH.exists():
        return
    try:
        pid = int(WORLD_SERVER_PID_PATH.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        log("Stopped host world server.")
    except (ValueError, OSError):
        pass
    with contextlib.suppress(OSError):  # read-only fs: the kill still counts
        WORLD_SERVER_PID_PATH.unlink(missing_ok=True)
        WORLD_SERVER_MODEL_DIGEST_PATH.unlink(missing_ok=True)


def benchmark_service_running() -> bool:
    try:
        with urlopen(Request(f"http://127.0.0.1:{BENCHMARK_PORT}/health"), timeout=1.0) as resp:
            return resp.status == 200
    except (URLError, OSError):
        return False


def ensure_benchmark_service(config: dict[str, object]) -> None:
    """Host API behind the webapp's sim-only Benchmark panel. Best-effort:
    a failure warns and the sim comes up without the panel's backend."""
    if benchmark_service_running():
        return
    uv = find_uv()
    bind = os.environ.get("INNATE_SIM_WORLD_BIND", "").strip() or _world_server_bind_addresses()
    if uv is None or not bind:
        warn("Benchmark service skipped (needs uv and a host-only bind address).")
        return
    sim_repo = REPO_ROOT / "sim"
    env = os.environ.copy()
    raw_env = config.get("raw_env")
    # .env secrets are launcher-side; a host process only sees them injected.
    key = (raw_env or {}).get("GEMINI_API_KEY", "") if isinstance(raw_env, dict) else ""
    if key:
        env["GEMINI_API_KEY"] = key
    script = sim_repo / "benchmarks" / "spatial_memory" / "service.py"
    with BENCHMARK_LOG_PATH.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [uv, "run", "--project", str(sim_repo), "--group", "benchmark", "python", str(script)]
            + ["--bind", bind, "--port", str(BENCHMARK_PORT)],
            cwd=sim_repo.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    BENCHMARK_PID_PATH.write_text(f"{proc.pid}\n", encoding="utf-8")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if benchmark_service_running():
            log("Benchmark service ready (webapp Benchmark panel).")
            return
        if proc.poll() is not None:
            warn(f"Benchmark service exited at start; see `{CLI_SIM} logs benchmark`.")
            return
        time.sleep(0.5)
    warn(f"Benchmark service not answering yet; see `{CLI_SIM} logs benchmark`.")


def stop_benchmark_service() -> None:
    if not BENCHMARK_PID_PATH.exists():
        return
    try:
        pid = int(BENCHMARK_PID_PATH.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        log("Stopped benchmark service.")
    except (ValueError, OSError):
        pass
    with contextlib.suppress(OSError):  # read-only fs: the kill still counts
        BENCHMARK_PID_PATH.unlink(missing_ok=True)


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

    world_ok, world_detail = world_server_health(timeout=0.5)
    world_level = "healthy" if world_ok else "error"
    if world_ok:
        world_label = (
            world_detail.removeprefix("ready").strip(" -").replace(" (software-speed rendering)", ", software-speed")
            or "ready"
        )
    else:
        world_label = "down"

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

    if all(level == "healthy" for level in (world_level, sim_level, transport_level, brain_level, agent_level)):
        stack_mood = ("healthy", "LIVE")
    elif any(level == "error" for level in (world_level, sim_level, transport_level, brain_level)):
        stack_mood = ("error", "DEGRADED")
    else:
        stack_mood = ("warn", "WARMING")

    system_summary = (
        f"os {'up' if os_running else 'down'} | "
        f"world {'up' if world_ok else 'down'} | "
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
        "world_level": world_level,
        "world_label": world_label,
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
