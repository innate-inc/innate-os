# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import contextlib
import errno
import functools
import grp
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import oci
from config import (
    ASSETS_IMAGE_LAYERS,
    BOOTSTRAP_LOG_PATH,
    CLI_SIM,
    COMPOSE_LOG_PATH,
    COMPOSE_PROJECT_NAME,
    DEPS_IMAGE_TAG_PREFIX,
    DOWN_LOG_PATH,
    GENERATED_OS_ENV_PATH,
    GEOMETRY_INPUT_PATHSPECS,
    HOST_REPO_ID,
    INNATE_BACKEND,
    LEGACY_CLOUD_AGENT_CONTAINER,
    LEGACY_SHARED_CONTAINER,
    LEGACY_SHARED_PROJECT,
    NO_BACKEND,
    OS_BUILD_LOG_PATH,
    OS_CONTAINER_NAME,
    OS_CONTAINER_SERVICE,
    OS_CONTAINER_TMUX_CMD,
    OS_SESSION_LOG_PATH,
    OS_SESSION_READY_POLL_SECONDS,
    PORT_BASE_ENV,
    PUBLISHED_PORT_ENV,
    REPO_ROOT,
    ROS_INSTALL_STATE_PATH,
    SIM_ASSET_UNITS,
    SIM_ASSET_UNITS_AUTHORED,
    SIM_ASSET_UNITS_DERIVED,
    SIM_FOXGLOVE_PORT,
    SIM_HTTP_PORT,
    SIM_HTTPS_PORT,
    SIM_ROSBRIDGE_PORT,
    SIM_UDP_PORT,
    TMUX_SESSION_NAME,
    VIEWER_BUILD_LOG_PATH,
    VIEWER_TREE_PATH,
    WORLD_SERVER_LOG_PATH,
    WORLD_SERVER_MODEL_DIGEST_PATH,
    WORLD_SERVER_PID_PATH,
    WORLD_SERVER_PORT,
    WORLD_SERVER_PORTS_PATH,
    WORLD_STATE_PORT,
    DockerUnresponsiveError,
    StackError,
    compute_geometry_inputs_hash,
    compute_ros_install_validation_hash,
    ensure_state_dir,
    log,
    resolve_assets_image,
    resolve_auto_os_image,
    resolve_deps_image,
    resolve_local_os_image,
    resolve_local_viewer_image,
    resolve_viewer_image,
    viewer_tree_dirty,
    warn,
)
from dashboard import (
    CYAN,
    DIM,
    GREEN,
    NC,
    RED,
    STEP_LABEL_WIDTH,
    STEP_TAIL_ROWS,
    USE_COLOR,
    active_step,
    clean_log_line,
    format_bytes,
    live_step,
    render_progress_bar,
)

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


_DOCKER_LAYER_RE = re.compile(r"^([0-9a-f]{12}): (.+)$")
_DOCKER_SIZE_RE = re.compile(r"([\d.]+)([kMG]?B)/([\d.]+)([kMG]?B)")
_BYTE_UNITS = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3}


_DOWNLOADED = ("Download complete", "Verifying Checksum", "Extracting", "Pull complete", "Already exists")
_PULLED = ("Pull complete", "Already exists")


def _parse_size(status: str) -> tuple[float, float]:
    size = _DOCKER_SIZE_RE.search(status)
    if not size:
        return 0.0, 0.0
    return (
        float(size.group(1)) * _BYTE_UNITS[size.group(2)],
        float(size.group(3)) * _BYTE_UNITS[size.group(4)],
    )


def _layer_share(status: str, ratio: float) -> float:
    """How much of one layer's work is done: half for the download, half for
    the extraction. Progress per layer only ever grows, unlike a byte
    percentage, whose denominator jumps every time a queued layer starts."""
    if status.startswith(_PULLED):
        return 1.0
    if status.startswith("Extracting"):
        return 0.5 + 0.5 * ratio
    if status.startswith(("Download complete", "Verifying Checksum")):
        return 0.5
    if status.startswith("Downloading"):
        return 0.5 * ratio
    return 0.0


def docker_pull_progress(output: str) -> str | None:
    """One aggregate progress line for a `docker pull`, or None when the output
    is not one (a compose or build log, or nothing yet).

    Docker's own per-layer chatter is what users read as confusing: a dozen
    interleaved ids, each announcing "Download complete" while the pull plainly
    continues.
    """
    layers: dict[str, dict[str, float | str]] = {}
    for line in output.splitlines():
        match = _DOCKER_LAYER_RE.match(line.strip())
        if not match:
            continue
        status = match.group(2)
        layer = layers.setdefault(match.group(1), {"status": "", "ratio": 0.0, "done": 0.0, "total": 0.0})
        layer["status"] = status
        done, total = _parse_size(status)
        layer["ratio"] = done / total if total else 0.0
        # Only download lines carry download bytes -- an Extracting line
        # reports the layer's UNCOMPRESSED size, which would inflate the total.
        if status.startswith("Downloading"):
            layer["done"], layer["total"] = done, total
        elif status.startswith(_DOWNLOADED):
            layer["done"] = layer["total"]
    if not layers:
        return None

    progress = sum(_layer_share(str(layer["status"]), float(layer["ratio"])) for layer in layers.values())
    downloaded = sum(float(layer["done"]) for layer in layers.values())
    complete = sum(1 for layer in layers.values() if str(layer["status"]).startswith(_PULLED))
    fraction = progress / len(layers)
    # Only when docker actually reported sizes: its non-TTY output often has
    # none, and "0 MB" beside a moving bar reads as a stalled download.
    size = f"{format_bytes(downloaded)}  " if downloaded else ""
    return f"{render_progress_bar(fraction)} {fraction * 100:3.0f}%  {size}{DIM}{complete}/{len(layers)} layers{NC}"


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
    progress_formatter: Callable[[str], str | None] | None = None,
) -> None:
    ensure_state_dir()
    # Heartbeats must describe THIS command: reading the whole shared log
    # would parrot stale lines from the previous command (a failed pull's
    # 'unauthorized' haunted the subsequent build's heartbeats).
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    started = time.monotonic()
    # A bar redrawn in place needs to be refreshed often to look like one; a
    # log file gets the slow cadence, so a CI transcript is not a flipbook.
    live = progress_formatter is not None and sys.stdout.isatty()
    if live or (sys.stdout.isatty() and active_step() is not None):
        heartbeat_seconds = 0.5
    drew_progress = False
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
                appended = latest = ""
                with contextlib.suppress(OSError):
                    appended = log_path.read_text(errors="replace")[log_offset:]
                    latest = next((line.strip() for line in reversed(appended.splitlines()) if line.strip()), "")
                elapsed = int(now - started)
                stamp = f"{elapsed // 60}m{elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
                progress = progress_formatter(appended) if progress_formatter and appended else None
                step = active_step()
                if step is not None:
                    # The step line already says what is happening; the detail
                    # says how far along. Before docker's first layer line is
                    # parseable that is just the clock.
                    step.detail = f"{progress}  {stamp}" if progress else stamp
                    # A formatter means there is something better to show: for
                    # the pulls, the bar that replaced this very chatter.
                    if progress_formatter is None:
                        lines = [clean_log_line(line) for line in appended.splitlines()]
                        step.tail = [line for line in lines if line.strip()][-STEP_TAIL_ROWS:]
                elif progress and live:
                    print(f"\r\033[K  {progress}  {DIM}{stamp}{NC}", end="", flush=True)
                    drew_progress = True
                elif progress:
                    log(f"{progress_message} ({stamp}) {progress}")
                elif latest:
                    log(f"{progress_message} ({stamp}) Latest: {latest}")
                else:
                    log(f"{progress_message} ({stamp}, no output yet)")
                next_heartbeat = now + heartbeat_seconds
            time.sleep(0.25 if live else 0.5)

    if drew_progress:
        print()  # close the line the bar was redrawing

    if return_code != 0:
        if not include_recent_log_on_failure:
            raise StackError(f"{failure_message}\nFull log: {log_path}")
        raise StackError(f"{failure_message}\nRecent log output:\n{tail_file(log_path, limit=60)}")


DOCKER_GROUP = "docker"
DOCKER_GROUP_REEXEC_ENV = "INNATE_SIM_DOCKER_GROUP_REEXEC"


def _docker_group_is_stale() -> bool:
    """Is this user a member of the docker group everywhere except in this
    process? That is precisely what `usermod -aG docker` leaves behind: the
    grant is in /etc/group, but a process only reads its groups at creation,
    so every shell opened before it stays locked out until the next login.
    """
    if sys.platform != "linux":
        return False
    try:
        entry = grp.getgrnam(DOCKER_GROUP)
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return False
    return entry.gr_gid not in os.getgroups() and user in entry.gr_mem


def reexec_under_docker_group() -> None:
    """Re-run this command with the docker group applied, or return.

    `sg` runs one command under a group the caller is already a member of --
    the non-interactive half of `newgrp`, and the only way to fix this without
    a new login session, since no process can add a group to a running one.
    Returns (rather than raising) whenever it cannot help, leaving the caller's
    own diagnosis to stand.
    """
    if os.environ.get(DOCKER_GROUP_REEXEC_ENV) or not _docker_group_is_stale():
        return
    command = shlex.join([sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])
    argv = _docker_group_argv(command)
    if argv is None:
        return
    log(f"Your user is in the {DOCKER_GROUP} group, but this shell predates it -- rerunning with it applied.")
    log("A new login session will not need this.")
    # The guard rides along in the environment: a re-exec that somehow does not
    # confer the group must fail once, not fork forever.
    os.environ[DOCKER_GROUP_REEXEC_ENV] = "1"
    try:
        os.execvp(argv[0], argv)
    except OSError:
        os.environ.pop(DOCKER_GROUP_REEXEC_ENV, None)


def _docker_group_argv(command: str) -> list[str] | None:
    """How to re-run `command` with the docker group applied, or None.

    `sg` is the direct route. Minimized WSL images ship neither it nor newgrp
    (both live in the `passwd` package), so fall back to sudo, which re-reads
    the target user's groups from the database. Only passwordless sudo: a
    prompt nobody asked for, in the middle of `up`, is worse than the message
    this would have replaced.
    """
    if shutil.which("sg") is not None:
        return ["sg", DOCKER_GROUP, "-c", command]
    if shutil.which("sudo") is None or not command_succeeds(
        ["sudo", "-n", "true"], cwd=Path.cwd(), env=os.environ.copy()
    ):
        return None
    # `env VAR=1` in the argv, because sudo's env_reset drops the guard this
    # function's caller just set -- and a guard that silently vanishes is not
    # one. See DOCKER_GROUP_REEXEC_ENV.
    return [
        "sudo",
        "-n",
        "-u",
        pwd.getpwuid(os.getuid()).pw_name,
        "--",
        "env",
        f"{DOCKER_GROUP_REEXEC_ENV}=1",
        "sh",
        "-c",
        command,
    ]


# Docker Desktop 4.x can install its CLI into ~/.docker/bin rather than
# /usr/local/bin, and teaches the shell about it by editing ~/.zprofile --
# which bash never reads, and no non-login shell reads either. The binary is
# there; only the search is wrong.
DOCKER_CLI_DIRS = (
    Path.home() / ".docker" / "bin",
    Path("/usr/local/bin"),
    Path("/Applications/Docker.app/Contents/Resources/bin"),
)


def _find_docker_cli() -> None:
    """Put Docker's own CLI on PATH for this process, if PATH lacks it."""
    if shutil.which("docker"):
        return
    for directory in DOCKER_CLI_DIRS:
        candidate = directory / "docker"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
            log(f"Using the docker command from {directory} (your shell's PATH does not have it).")
            return


def ensure_docker_available(*, command_hint: str = CLI_SIM, require_compose: bool = True) -> None:
    _find_docker_cli()
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
                "  sudo usermod -aG docker $USER\n"
                "then open a new login session -- or, if `newgrp` is installed (it ships in the\n"
                "`passwd` package, which minimized WSL images leave out), `newgrp docker` applies\n"
                "it to this shell.\n"
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
            reexec_under_docker_group()  # returns only if it cannot be fixed here
            raise StackError(
                "Docker is running, but your user is not allowed to talk to it (permission denied "
                "on the Docker socket).\n"
                "  sudo usermod -aG docker $USER\n"
                "then open a new login session -- or, if `newgrp` is installed (it ships in the\n"
                "`passwd` package, which minimized WSL images leave out), `newgrp docker` applies\n"
                "it to this shell.\n"
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

    # Right after the engine probe, before any Compose check can raise over
    # it: a broken-mount daemon plus an old Compose plugin are two problems,
    # and fixing the second must not hide the first.

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
            f"Docker Compose did not answer `version` within {DOCKER_PROBE_TIMEOUT_S:.0f}s.\n"
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
        # Conservative floors, not feature-driven: the compose file asks for
        # nothing exotic now that the image mounts are gone.
        (20, 10),
        "Update Docker (Desktop: update the app; Linux: reinstall docker-ce from Docker's\nrepo)",
        command_hint,
        DOCKER_INSTALL_URL,
    )
    # Older Compose rejects the whole file with a schema error naming a field,
    # not a version.
    _require_min_version(
        "Docker Compose",
        compose.stdout,
        (2, 0),
        "Update the Compose plugin (Docker Desktop: update the app; Linux: reinstall\n"
        "docker-compose-v2 / docker-compose-plugin from Docker's repo)",
        command_hint,
        COMPOSE_INSTALL_URL,
    )


def _parse_version(version_output: str | None) -> tuple[int, ...] | None:
    """First `[v]major.minor[.patch]` in a version-command's output, or None."""
    found = re.search(r"v?(\d+)\.(\d+)(?:\.(\d+))?", version_output or "")
    if found is None:
        return None
    return tuple(int(part) for part in found.groups() if part is not None)


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
    version = _parse_version(version_output)
    if version is None or version[:2] >= minimum:
        return
    raise StackError(
        f"{label} {'.'.join(map(str, version))} is older than the simulator supports; "
        f"it needs {label} {minimum[0]}.{minimum[1]} or newer.\n"
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
    values = {
        "INNATE_OS_ENV_FILE": str(env_file),
        "INNATE_OS_CONTAINER_NAME": OS_CONTAINER_NAME,
        "SIM_WORLD_STATE_PORT": str(WORLD_STATE_PORT),
        **PUBLISHED_PORT_ENV,
    }
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
        progress_formatter=docker_pull_progress,
    )


def adopt_published_deps_image(local_image: str, *, cwd: Path, env: dict[str, str]) -> bool:
    """Pull the published deps image and give it the local build's name.

    Retagged rather than used under its own name: same bytes at the same content
    address, so every path that already looks for the local build keeps working.
    """
    deps_image = resolve_deps_image(REPO_ROOT)
    log(f"Fetching prebuilt dependencies {shorten_docker_image_ref(deps_image)}...")
    plat = _docker_platform()
    try:
        run_logged_with_heartbeat(
            ["docker", "pull", *(["--platform", plat] if plat else []), deps_image],
            cwd=cwd,
            env=env,
            log_path=COMPOSE_LOG_PATH,
            failure_message=f"Could not pull the prebuilt dependency image: {shorten_docker_image_ref(deps_image)}",
            progress_message="Docker is still pulling the dependency image.",
            include_recent_log_on_failure=False,
            progress_formatter=docker_pull_progress,
        )
    except StackError:
        return False
    if not command_succeeds(["docker", "tag", deps_image, local_image], cwd=cwd, env=env):
        return False
    log("Using the published dependency image; skipping the local Docker build.")
    return True


def prune_stale_local_images(
    current_image: str,
    *,
    cwd: Path,
    env: dict[str, str],
    label: str,
    tag_prefix: str = "inputs-",
    keep_recent: int = 0,
) -> None:
    """Untag superseded content-addressed tags sharing `current_image`'s repo.

    `keep_recent` holds back that many newest, so a branch bounce does not
    re-download. Affordable only because the bases are digest-pinned: a second
    copy costs its own layers, not a duplicate of everything beneath them.

    The repo comes off `current_image` rather than a second argument: sweeping
    a different repo than the one being kept is the only way this can be wrong.
    rpartition takes the LAST colon, so a registry:port survives.

    Best-effort: an image still used by a container fails to untag and is left.
    """
    repo = current_image.rpartition(":")[0]
    try:
        listing = subprocess.run(
            # Timestamp first: keep_recent must not rest on the undocumented
            # ordering of a formatted listing.
            ["docker", "images", "--format", "{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}", repo],
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
    rows = [line.split("\t", 1) for line in listing.stdout.splitlines() if "\t" in line]
    stale = [
        ref for _, ref in sorted(rows, reverse=True) if ref != current_image and ref.startswith(f"{repo}:{tag_prefix}")
    ]
    pruned = sum(command_succeeds(["docker", "rmi", ref], cwd=cwd, env=env) for ref in stale[keep_recent:])
    if pruned:
        log(f"Pruned {pruned} superseded {label} image tag(s).")


def prune_superseded_pulled_images(config: dict[str, object], *, cwd: Path, env: dict[str, str]) -> None:
    """Never under --offline: deleting the only copy of something that cannot be
    re-fetched is a different mistake from using disk."""
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    families = (
        (resolve_auto_os_image(os_repo), "inputs-", "Innate OS prebuilt", 1),
        (resolve_deps_image(os_repo), DEPS_IMAGE_TAG_PREFIX, "dependency", 1),
        (resolve_viewer_image(os_repo), "inputs-", "sim viewer bundle", 1),
        (resolve_assets_image(os_repo), "inputs-", "sim asset", 0),
    )
    for current, prefix, label, keep in families:
        prune_stale_local_images(current, cwd=cwd, env=env, label=label, tag_prefix=prefix, keep_recent=keep)


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


def world_endpoint() -> str:
    """The address the container reaches the host world server on."""
    return f"host.docker.internal:{WORLD_SERVER_PORT}"


# Label, host port, and the container port published there -- None for the world
# server, which runs outside Docker. Container ports never move; the host side does.
_STACK_PORTS = (
    ("webapp (https)", SIM_HTTPS_PORT, "443/tcp"),
    ("webapp (http)", SIM_HTTP_PORT, "80/tcp"),
    ("rosbridge", SIM_ROSBRIDGE_PORT, "9090/tcp"),
    ("leader receiver", SIM_UDP_PORT, "9999/udp"),
    ("foxglove bridge", SIM_FOXGLOVE_PORT, "8765/tcp"),
    ("world server", WORLD_SERVER_PORT, None),
    ("world state stream", WORLD_STATE_PORT, None),
)


# The world ports reach the container as env, not as a publish, so they need
# their own drift check. Third field: what an ABSENT variable means, mirroring
# the consumer's fallback, so a container predating it is not read as drift.
_CONTAINER_ENV_PUBLISH = (
    ("VIRTUAL_MARS_REMOTE", world_endpoint(), ""),
    ("INNATE_WORLD_STATE_PORT", str(WORLD_STATE_PORT), "8800"),
)


def _container_env(name: str) -> dict[str, str] | None:
    """The container's baked environment, or None when the probe cannot answer."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Config.Env}}", name],
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    pairs = (entry.partition("=") for entry in entries)
    return {key: value for key, sep, value in pairs if sep}


def _container_port_map(name: str) -> dict[str, set[int]] | None:
    """`docker port` as {"443/tcp": {443}}. None is "unknown", never "drift"."""
    try:
        result = subprocess.run(
            ["docker", "port", name],
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    mapping: dict[str, set[int]] = {}
    for line in result.stdout.splitlines():
        spec, arrow, published = line.partition(" -> ")
        host_port = published.rpartition(":")[2].strip()
        if arrow and host_port.isdigit():
            mapping.setdefault(spec.strip(), set()).add(int(host_port))
    return mapping


def os_container_current() -> bool:
    """Whether the running container's publishes and world-server env still match
    what the launcher resolves. Neither can be changed in place, so drift costs a
    recreate; an unanswered probe reports current, since a flaky answer must not."""
    published = _container_port_map(OS_CONTAINER_NAME)
    if published is None:
        return True
    if not all(host in published.get(spec, set()) for _, host, spec in _STACK_PORTS if spec):
        return False
    env = _container_env(OS_CONTAINER_NAME)
    if env is None:
        return True
    return all(env.get(key, absent) == expected for key, expected, absent in _CONTAINER_ENV_PUBLISH)


def retitle_step(message: str) -> None:
    step = active_step()
    if step is not None:
        step.retitle(message)


def ensure_os_container(config: dict[str, object], os_env_file: Path, *, offline: bool = False) -> None:
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    os_image = str(config["os_image"]).strip()
    os_image_auto = bool(config["os_image_auto"])
    reuse_running_container = container_running(OS_CONTAINER_NAME)
    if reuse_running_container and not os_container_current():
        log(f"The running OS container is bound to an older port block; recreating it to serve {config['webapp_url']}.")
        reuse_running_container = False

    if reuse_running_container:
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
                elif bool(config["os_pull_image"]) and adopt_published_deps_image(
                    local_image, cwd=os_repo, env=compose_probe_env
                ):
                    os_image = local_image
                    up_cmd.append("--no-build")
                else:
                    warn(
                        "No matching prebuilt Innate OS image is available for this checkout "
                        f"({shorten_docker_image_ref(os_image)}), and no published dependency image "
                        "either. Building it locally instead -- this takes 25-30 minutes once. "
                        f"Details are in {COMPOSE_LOG_PATH}."
                    )
                    os_image = local_image
                    up_cmd.append("--build")
            else:
                up_cmd.append("--no-build")

        compose_values = {"INNATE_OS_ENV_FILE": str(os_env_file)}
        if os_image:
            compose_values["INNATE_OS_IMAGE"] = os_image
        compose_values["VIRTUAL_MARS_REMOTE"] = str(config.get("world_endpoint", "") or "")
        compose_env = os_compose_env(compose_values, env_file=os_env_file)
        log("Starting Innate OS dev container...")
        retitle_step("Starting the Innate OS container")
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
    if not offline:
        retitle_step("Reclaiming superseded images")
        prune_superseded_pulled_images(config, cwd=os_repo, env=compose_env)
    build_cmd = (
        f"INNATE_OS_ALWAYS_BUILD={1 if config['os_always_build'] else 0} "
        f"INNATE_OS_HOST_REPO_ID={shlex.quote(HOST_REPO_ID)} "
        "~/innate-os/scripts/validate_sim_ros_install.zsh"
    )

    ros_inputs_hash = compute_ros_install_validation_hash(os_repo)
    ros_install_marker_matches = (
        ROS_INSTALL_STATE_PATH.exists()
        and ROS_INSTALL_STATE_PATH.read_text(encoding="utf-8").strip() == ros_inputs_hash
    )
    ros_install_already_validated = False
    if ros_install_marker_matches and not bool(config["os_always_build"]):
        ros_install_already_validated = reuse_running_container or command_succeeds(
            os_compose_zsh_cmd("test -f ~/innate-os/ros2_ws/install/setup.zsh"),
            cwd=os_repo,
            env=compose_env,
        )
    if ros_install_already_validated:
        log("ROS workspace install already validated for this checkout.")
    else:
        log("Building / validating the ROS workspace inside the container...")
        retitle_step("Building the ROS workspace (first build takes a few minutes)")
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
    retitle_step("Launching the ROS nodes")
    launch_script = (
        "INNATE_SIM_TMUX_SETTLE_SECONDS=${INNATE_SIM_TMUX_SETTLE_SECONDS:-0} "
        "INNATE_SIM_TMUX_CLEANUP_SETTLE_SECONDS=${INNATE_SIM_TMUX_CLEANUP_SETTLE_SECONDS:-0} "
        f"{OS_CONTAINER_TMUX_CMD}"
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
    remove_superseded_containers()
    log("Removing Innate OS containers, networks, and named volumes...")
    down_os(config, remove_volumes=True)
    # Belt-and-suspenders: force-remove named containers that may linger outside
    # the compose project (e.g. after a partial or failed startup).
    force_remove_container(OS_CONTAINER_NAME)


def force_remove_container(name: str) -> bool:
    """Best-effort `docker rm -f`; True when a container was actually removed.

    Never raises: this runs first in the teardown paths, where a wedged daemon
    must not swallow the shutdown (or the startup error that triggered it).
    """
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", name],
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        warn(f"Docker did not answer `docker rm -f {name}` within {DOCKER_PROBE_TIMEOUT_S:.0f}s; check `docker ps`.")
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def container_in_compose_project(name: str, project: str = COMPOSE_PROJECT_NAME) -> bool:
    """True when `name` is a container `project` created, rather than a
    hand-started one that happens to share the name."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}', name],
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == project


def remove_legacy_cloud_agent() -> None:
    """Drop the cloud-agent container older checkouts ran the brain in.

    It is no longer a service of this compose project, so `down` cannot see it,
    and it holds host port 8765 -- which the Foxglove bridge now publishes on.
    Only a container this launcher started is removed: the same name run by hand
    (an own fork, or one serving a physical MARS) is left alone.
    """
    if not container_in_compose_project(LEGACY_CLOUD_AGENT_CONTAINER):
        return
    if force_remove_container(LEGACY_CLOUD_AGENT_CONTAINER):
        log("Removed the leftover cloud-agent container; the brain now runs inside brain_client.")


def running_stack_from_another_checkout() -> tuple[str, str] | None:
    """(container, that checkout's path) for another clone's running stack.

    Stacks are per-checkout but the ports are not. The path comes off the
    container's own ros2_ws bind mount, so the remedy can name it.
    """
    try:
        listing = subprocess.run(
            ["docker", "ps", "--filter", "label=com.docker.compose.project", "--format", "{{.Names}}"],
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None  # a probe that cannot answer must not block a start
    if listing.returncode != 0:
        return None
    for name in listing.stdout.split():
        if name == OS_CONTAINER_NAME or not name.startswith(f"{LEGACY_SHARED_CONTAINER}-"):
            continue
        try:
            mounts = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    '{{range .Mounts}}{{if eq .Destination "/root/innate-os/ros2_ws"}}{{.Source}}{{end}}{{end}}',
                    name,
                ],
                text=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=DOCKER_PROBE_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return (name, "")
        source = mounts.stdout.strip() if mounts.returncode == 0 else ""
        # Docker Desktop prefixes the host path; strip it to something cd-able.
        checkout = source[len("/host_mnt") :] if source.startswith("/host_mnt/") else source
        return (name, str(Path(checkout).parent) if checkout else "")
    return None


def _bind_refusal(port: int, *, udp: bool, reuse: bool = False) -> int | None:
    """errno from claiming the port the way its consumer will, or None when the
    bind succeeds. Docker publishes without SO_REUSEADDR; the host world server
    sets it (socket.create_server), so its probe must too -- a just-stopped
    server's TIME_WAIT remnants otherwise read as a live collision for ~30s."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as probe:
        if reuse:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError as exc:
            return exc.errno
    return None


def _tcp_listener_answers(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
        return True
    return False


def _host_port_free(port: int, *, udp: bool, reuse: bool = False) -> bool:
    """Whether the port's consumer can still claim it (reuse: see
    _bind_refusal). Only EADDRINUSE proves a collision: Linux refuses ports
    below 1024 to a non-root binder while the daemon that publishes them runs
    as root, so a free 443 refuses the probe."""
    refusal = _bind_refusal(port, udp=udp, reuse=reuse)
    if refusal is None:
        return True
    if refusal == errno.EACCES and not udp:
        return not _tcp_listener_answers(port)
    return refusal != errno.EADDRINUSE


def _container_published_ports(name: str) -> set[int]:
    """Host ports a container publishes -- the OLD ones if it outlived a port
    change, which is the point: those are the ports it is really holding."""
    published = _container_port_map(name)
    return set() if published is None else {port for ports in published.values() for port in ports}


def _own_world_server_ports() -> set[int]:
    """Ports the world server this checkout started is holding, from the recorded
    pair rather than the configuration (see WORLD_SERVER_PORTS_PATH). A live pid
    is enough: a wedged server still holds them, and ensure_world_server stops it."""
    try:
        pid = int(WORLD_SERVER_PID_PATH.read_text(encoding="utf-8").strip())
        ports = {int(port) for port in WORLD_SERVER_PORTS_PATH.read_text(encoding="utf-8").split()}
    except (OSError, ValueError):
        return set()
    try:
        os.kill(pid, 0)
    except OSError:
        return set()
    return ports


def _suggest_port_base() -> int | None:
    """A base whose whole block is free, so the remedy cannot echo back the base
    that just failed."""
    for base in range(8600, 9600, 10):
        if all(
            _host_port_free(base + offset, udp=(spec or "").endswith("/udp"), reuse=spec is None)
            for offset, (_, _, spec) in enumerate(_STACK_PORTS)
        ):
            return base
    return None


def _other_checkout_holding(ports: set[int]) -> tuple[str, str] | None:
    """The other checkout's stack, but only when it actually publishes one of
    these ports -- an unrelated listener must not be reported as a simulator."""
    found = running_stack_from_another_checkout()
    if found is None or not ports & _container_published_ports(found[0]):
        return None
    return found


def refuse_if_ports_taken() -> None:
    """Refuse before anything claims a host port this stack needs. Ahead of the
    world server, not just ahead of compose: a second checkout sharing the block
    would otherwise SIGTERM the first one's physics (_stop_stale_world_server)
    before Docker ever complained about 443."""
    ours = _container_published_ports(OS_CONTAINER_NAME) | _own_world_server_ports()
    taken = [
        (label, port)
        for label, port, spec in _STACK_PORTS
        if port not in ours and not _host_port_free(port, udp=(spec or "").endswith("/udp"), reuse=spec is None)
    ]
    if not taken:
        return
    listing = "\n".join(f"  {port:<5} {label}" for label, port in taken)
    suggestion = _suggest_port_base()
    move = (
        f"move this checkout to a block that is free:\n  {PORT_BASE_ENV}={suggestion} {CLI_SIM} up"
        if suggestion is not None
        else f"set {PORT_BASE_ENV} to the start of seven free ports"
    )
    other = _other_checkout_holding({port for _, port in taken})
    if other is None:
        culprit = f"Stop whatever owns them (lsof -nP -iTCP:{taken[0][1]}), or "
    else:
        container, checkout = other
        stop = f"  cd {checkout} && {CLI_SIM} down" if checkout else f"  docker stop {container}"
        culprit = f"Another checkout's simulator holds them ({container}). Stop it:\n{stop}\n\nOr "
    raise StackError(f"These host ports are already in use:\n{listing}\n\n{culprit}{move}")


def remove_legacy_shared_container() -> None:
    """Drop the single container every checkout used to share.

    Only one the OLD compose project created, so the same name run by hand is
    left alone. Its volumes are named in the log rather than removed.
    """
    if not container_in_compose_project(LEGACY_SHARED_CONTAINER, LEGACY_SHARED_PROJECT):
        return
    if force_remove_container(LEGACY_SHARED_CONTAINER):
        log(
            "Removed the shared Innate OS container; each checkout now gets its own.\n"
            "This checkout rebuilds its ROS workspace once. Reclaim the old shared one with:\n"
            f"  docker volume rm {LEGACY_SHARED_PROJECT}_ros2_ws_build "
            f"{LEGACY_SHARED_PROJECT}_ros2_ws_install {LEGACY_SHARED_PROJECT}_ros2_ws_log "
            f"{LEGACY_SHARED_PROJECT}_ccache"
        )


def remove_superseded_containers() -> None:
    remove_legacy_cloud_agent()
    remove_legacy_shared_container()


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
    #
    # `-p` rather than `name:` or COMPOSE_PROJECT_NAME: it outranks both, so no
    # reader has to recall their precedence.
    return ["docker", "compose", "-p", COMPOSE_PROJECT_NAME, "-f", "sim/docker-compose.dev.yml", *parts]


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
    if not container_running(OS_CONTAINER_NAME):
        raise StackError(f"Innate OS dev container is not running.\nStart it with `{CLI_SIM} up` first.")
    return subprocess.run(["docker", "exec", "-it", OS_CONTAINER_NAME, "zsh"]).returncode


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
    os_running = container_running(OS_CONTAINER_NAME)
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
        _rosbridge_ws_confirmed = websocket_port_open(SIM_ROSBRIDGE_PORT)
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
    if not container_running(OS_CONTAINER_NAME):
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
    return {
        "os_status": os_status,
        "sim_running": sim_running,
        "rosbridge_live": rosbridge_live,
    }


def os_runtime_ready(config: dict[str, object]) -> bool:
    os_status = collect_os_process_status(config)
    return (
        os_status["os_session_running"]
        and os_status["rosbridge_process_live"]
        and os_status["brain_process_live"]
        and websocket_port_open(SIM_ROSBRIDGE_PORT)
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
    """The running stack is both complete and current. A container on a stale port
    block is not reusable however healthy it looks -- only a recreate moves it."""
    return os_runtime_ready(config) and os_container_current()


def format_startup_check(ok: bool, label: str, detail: str) -> str:
    """The row a live step settles into, so the summary at the end of `up` and
    the steps that produced it are visibly one list."""
    mark = f"{GREEN}✔{NC}" if ok else f"{RED}✗{NC}"
    return f"  {CYAN}{label:>{STEP_LABEL_WIDTH}}{NC}  {mark} {detail}"


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
        (world_ok, "world", world_detail),
        (
            os_status["os_running"],
            "os",
            "running" if os_status["os_running"] else "down",
        ),
        (
            os_status["os_session_running"],
            "ros",
            "tmux session running" if os_status["os_session_running"] else "missing",
        ),
        (
            bool(probe["rosbridge_live"]),
            "bridge",
            f"ws://localhost:{SIM_ROSBRIDGE_PORT} live" if probe["rosbridge_live"] else "not accepting connections",
        ),
        (
            os_status["brain_process_live"],
            "brain",
            "brain_client_node.py running" if os_status["brain_process_live"] else "brain_client_node.py missing",
        ),
        (
            sim_driver_ready,
            "sim",
            "/odom publishing" if sim_driver_ready else "/odom not publishing",
        ),
    ]

    print()
    for ok, label, detail in checks:
        print(format_startup_check(ok, label, detail))
    return world_ok


def capture_os_brain_logs(config: dict[str, object], lines: int = 18) -> list[str]:
    if not container_running(OS_CONTAINER_NAME):
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

    # The marker holds "<digest> <image ref> <geometry inputs hash>". Keyed on
    # the geometry layer's
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
    geometry_hash = compute_geometry_inputs_hash(config["os_repo"])  # type: ignore[arg-type]
    if not os.environ.get("INNATE_SIM_ASSETS_IMAGE", "").strip() and parts[1:2] == [image] and installed:
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
        # The name also moves for inputs that cannot change geometry, and
        # "push the branch" is not advice a fork can take.
        if parts[2:3] == [geometry_hash] and installed:
            log("Reusing the installed geometry (geometry inputs unchanged).")
            return
        raise StackError(
            f"No published sim asset image for this checkout ({shorten_docker_image_ref(image)}): {exc}\n"
            f"The geometry inputs themselves changed ({', '.join(GEOMETRY_INPUT_PATHSPECS)}), so what is "
            f"installed no longer describes this checkout. Push the branch so CI publishes it, or set "
            f"INNATE_SIM_ASSETS_IMAGE to one that exists."
        ) from exc
    digest = manifest["layers"][ASSETS_IMAGE_LAYERS.index("work")]["digest"]

    if parts[:1] == [digest] and installed:
        # Same geometry under a new ref (or an old digest-only marker):
        # remember the ref so the next run skips the probe above.
        marker.write_text(f"{digest} {image} {geometry_hash}\n")
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
        marker.write_text(f"{digest} {image} {geometry_hash}\n")
        log(f"Sim assets {digest[7:19]} installed.")
    finally:
        blob.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def ensure_viewer_public_assets(config: dict[str, object]) -> None:
    """Install the models and physics the webapp serves at /models and /physics.

    The same image as the geometry, a different layer -- and on disk rather
    than mounted from the image, for the reasons in install_layer_subtree.
    """
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    install_layer_subtree(
        assets_image_ref(config),
        ASSETS_IMAGE_LAYERS.index("viewer"),
        "viewer",
        sim_repo / "viewer" / "public",
        sim_repo / "viewer" / "public" / ".installed-tag",
        label="viewer assets",
        geometry_hash=compute_geometry_inputs_hash(config["os_repo"]),  # type: ignore[arg-type]
    )


def install_layer_subtree(
    image: str,
    layer_index: int,
    subtree: str,
    destination: Path,
    marker: Path,
    *,
    label: str,
    geometry_hash: str | None = None,
) -> None:
    """Put one subtree of one image layer on disk, idempotently.

    The host reads these directories directly and the container bind-mounts
    them. They used to be `type: image` mounts, which is a tidier idea and a
    worse one in practice: two Docker components broke that feature in a
    single year (moby#51687, and Compose 5 resolving the source to a manifest
    digest), and both failures land on a user who never chose the feature.

    Idempotent through `marker`, which lives INSIDE the directory it describes
    (like sim/assets/.assets-tag): both are gitignored, so neither shows up as
    a working-tree change -- which would make every checkout look dirty and
    send the viewer bundle down the local-build path forever -- and deleting
    the directory forgets the marker with it. It holds
    "<layer digest> <ref> [<geometry inputs hash>]" -- the digest, so a tag
    that moved for an unrelated input does not re-fetch; the ref, so an
    override that renames the image does; and the narrow hash, so an
    unpublished tag can be told from a real change (see ensure_sim_assets).
    """
    parts = marker.read_text().split() if marker.exists() else []
    populated = destination.is_dir() and any(destination.iterdir())
    if parts[1:2] == [image] and populated:
        return

    try:
        manifest = oci.manifest_for_image(image)
    except oci.OciError:
        if geometry_hash is not None and parts[2:3] == [geometry_hash] and populated:
            log(f"Reusing the installed {label} (geometry inputs unchanged).")
            return
        raise
    digest = manifest["layers"][layer_index]["digest"]
    if parts[:1] == [digest] and populated:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{digest} {image} {geometry_hash or ''}\n")
        return

    # Staged beside the destination so the install is a rename, not a copy
    # across filesystems.
    blob = destination.parent / f".{destination.name}.tmp.tar.gz"
    staging = destination.parent / f".{destination.name}.tmp"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        repo, _ = oci.split_ref(image)
        with blob.open("wb") as handle:
            oci.fetch_layer(repo, digest, handle, oci.anon_token(repo), label=label)
        oci.safe_extract(blob, staging)
        source = staging / subtree
        if not source.is_dir():
            raise StackError(f"{shorten_docker_image_ref(image)} layer {digest[7:19]} has no {subtree}/ subtree.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(destination, ignore_errors=True)
        shutil.move(str(source), str(destination))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{digest} {image} {geometry_hash or ''}\n")
        log(f"{label} {digest[7:19]} installed.")
    finally:
        blob.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def viewer_image_ref(config: dict[str, object]) -> str:
    """The image the bundle came from, for reporting and for the local build.

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
    """Install the SimSession bundle the webapp loads, at sim/viewer/dist-lib.

    The published bundle when one describes this checkout -- fetched as a layer
    over plain HTTPS, so this path needs no Docker at all -- otherwise built
    here from sim/viewer/Dockerfile and copied out of the image. Two ways to
    end up building:

      * the tree is dirty, so no published image CAN describe it
      * the tree is clean but CI has not published it yet (or retention expired
        a branch tag) -- worth a warning, not worth blocking on

    Either way the bundle lands on disk and the container bind-mounts it, so
    the host never needs Node.js. Never runs on robots.
    """
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    destination = sim_repo / "viewer" / "dist-lib"
    marker = destination / ".installed-tag"

    override = os.environ.get("INNATE_SIM_VIEWER_BUNDLE_IMAGE", "").strip()
    if not override and not viewer_bundle_built_locally():
        image = resolve_viewer_image(config["os_repo"])  # type: ignore[arg-type]
        try:
            # A marker match returns before any network call, which is what
            # makes this work offline.
            install_layer_subtree(image, 0, "bundle", destination, marker, label="viewer bundle")
            config["viewer_bundle_image"] = image
            return
        except oci.OciError as exc:
            if offline:
                raise StackError(
                    f"Offline, and the sim viewer bundle is not installed:\n  {image}\n"
                    f"Run `{CLI_SIM} up` online once first."
                ) from exc
            # A permanently broken publish pipeline would otherwise be
            # invisible: everyone would quietly build their own.
            warn(
                f"No published sim viewer bundle for this commit ({shorten_docker_image_ref(image)}).\n"
                "  CI may still be publishing it (publish-viewer-bundle.yml, about a minute). "
                "Building it locally with the same Dockerfile in the meantime."
            )

    image = override or _build_viewer_image_locally(config, offline=offline)
    _extract_bundle_from_image(config, image, destination, marker)


def _extract_bundle_from_image(config: dict[str, object], image: str, destination: Path, marker: Path) -> None:
    """Copy /bundle out of a local image, for the builds no registry has.

    `docker cp` from a created-but-never-started container: the bundle is
    static files, and starting the image to read them would need it to have an
    entrypoint worth running.
    """
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    env = os_compose_env()
    image_id = capture_command_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], cwd=os_repo, env=env
    )
    if marker.exists() and marker.read_text().split()[:1] == [image_id] and destination.is_dir():
        return

    # The command is never run -- but `docker create` refuses without one, and
    # the bundle image is a bare FROM scratch carrying no CMD to inherit.
    created = capture_command_output(["docker", "create", image, "/bundle"], cwd=os_repo, env=env).strip()
    # capture_command_output falls back to stderr, so a failure here arrives
    # looking like a container id and `docker cp` reports the daemon's error
    # as the name of a container it cannot find. Insist it looks like an id.
    container = created if re.fullmatch(r"[0-9a-f]{12,64}", created) else ""
    if not container:
        detail = f"\n  docker said: {created}" if created else ""
        raise StackError(
            f"Could not create a container from {shorten_docker_image_ref(image)} to read the bundle.{detail}"
        )
    staging = destination.parent / f".{destination.name}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        run_logged(
            ["docker", "cp", f"{container}:/bundle/.", str(staging)],
            cwd=os_repo,
            env=env,
            log_path=VIEWER_BUILD_LOG_PATH,
            failure_message=f"Could not copy the viewer bundle out of {shorten_docker_image_ref(image)}.",
        )
        shutil.rmtree(destination, ignore_errors=True)
        shutil.move(str(staging), str(destination))
        marker.write_text(f"{image_id} {image}\n")
        log("Viewer bundle installed.")
    finally:
        subprocess.run(["docker", "rm", "-f", container], cwd=os_repo, env=env, capture_output=True, check=False)
        shutil.rmtree(staging, ignore_errors=True)


def _build_viewer_image_locally(config: dict[str, object], *, offline: bool) -> str:
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
        return image
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
    log(f"Building the sim viewer bundle ({why})...")
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
    return image


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
    """SIGTERM whatever owns the RPC port. `up` is about to bind it, so unlike
    stop_world_server this does not wait for evidence that we started it."""
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


def prefetch_runtime(config: dict[str, object]) -> None:
    """Download everything the first `up` would otherwise fetch, so that `up`
    is a start rather than a multi-gigabyte download.

    Every step is idempotent and individually non-fatal: a prefetch that only
    gets half way leaves `up` with less to do, never with a broken state --
    which is why a failing phase is reported and stepped over rather than
    raised, after the keys the user just entered have been written.
    """
    print()
    phases: list[tuple[str, str, str, Callable[[], None]]] = [
        ("assets", "Downloading the world geometry", "world geometry", lambda: ensure_sim_assets(config)),
        ("skills", "Downloading the skill assets", "skill assets", lambda: ensure_skill_assets(config)),
        ("viewer", "Downloading the 3D view assets", "3D view assets", lambda: ensure_viewer_public_assets(config)),
        ("bundle", "Fetching the 3D viewer bundle", "3D viewer bundle", lambda: ensure_sim_viewer_bundle(config)),
        ("image", "Pulling the Innate OS image", "Innate OS image", lambda: _prefetch_os_image(config)),
        ("world", "Preparing the sim world environment", "sim world environment", lambda: _prefetch_world_env(config)),
    ]
    for label, doing, done, phase in phases:
        with live_step(label, doing, done) as step:
            try:
                phase()
            except StackError as exc:
                # One phase failing must not throw away the rest, nor the keys
                # already written: `up` fetches whatever is missing anyway, so
                # a network blip costs a retry rather than the whole setup.
                step.ok = False
                warn(f"{exc}\n`{CLI_SIM} up` will fetch this when you start the simulator.")
    print()


def _prefetch_os_image(config: dict[str, object]) -> None:
    os_image = str(config["os_image"]).strip()
    if not os_image or not config["os_pull_image"]:
        return  # os.image = "local": there is no prebuilt to pull
    try:
        ensure_os_image_available(
            os_image,
            cwd=config["os_repo"],  # type: ignore[arg-type]
            env=os_compose_env(),
            pull_if_missing=True,
            include_pull_log_on_failure=not config["os_image_auto"],
        )
    except StackError:
        if not config["os_image_auto"]:
            raise
        # Same fallback order as ensure_os_container, so setup downloads what
        # `up` would and the first start is a start.
        os_repo: Path = config["os_repo"]  # type: ignore[assignment]
        local_image = resolve_local_os_image(os_repo)
        env = os_compose_env()
        if docker_image_present(local_image, cwd=os_repo, env=env):
            return
        if adopt_published_deps_image(local_image, cwd=os_repo, env=env):
            return
        warn(
            f"No prebuilt Innate OS image for this checkout ({shorten_docker_image_ref(os_image)}), "
            f"and no published dependency image either.\n"
            f"`{CLI_SIM} up` will build one locally instead, which takes considerably longer."
        )


def _prefetch_world_env(config: dict[str, object]) -> None:
    """Resolve the host venv the world server runs in (MuJoCo, rendering).

    Not covered by any Docker pull -- it is built on the host by uv, and is
    the largest remaining cold-start cost once the images are local.
    """
    uv = find_uv()
    if uv is None:
        warn(f"uv is not installed, so the sim world's Python environment was not prepared for `{CLI_SIM} up`.")
        return
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    log("Preparing the sim world's Python environment...")
    run_logged_with_heartbeat(
        [uv, "sync", "--project", str(sim_repo)],
        cwd=sim_repo,
        env=os.environ.copy(),
        log_path=BOOTSTRAP_LOG_PATH,
        failure_message="Could not prepare the sim world's Python environment.",
        progress_message="uv is still resolving the sim world's Python environment.",
    )


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
    endpoint = world_endpoint()
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
        if (
            reply.get("state_port") == WORLD_STATE_PORT
            and actual_binds is not None
            and set(actual_binds) == expected_binds
            and reply.get("beacon") == _beacon_port_wanted()
        ):
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
        elif reply["state_port"] != WORLD_STATE_PORT:
            log(
                f"Host world server streams observer state on port {reply['state_port']}, but the "
                f"webapp and viewer were told {WORLD_STATE_PORT} -- restarting it..."
            )
        elif actual_binds is None:
            log("Host world server predates bind reporting -- restarting it...")
        elif set(actual_binds) != expected_binds:
            log(
                f"Host world server listens on {','.join(actual_binds)} but the current policy "
                f"wants {bind} -- restarting it..."
            )
        else:
            log("Host world server announces a different simulator (or none) to the app -- restarting it...")
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


def _beacon_port_wanted() -> int | None:
    """The rosbridge port the world server's beacon should announce; None when
    the beacon is switched off (INNATE_SIM_BEACON=0)."""
    if os.environ.get("INNATE_SIM_BEACON", "1").strip() in ("0", "false", "no"):
        return None
    return SIM_ROSBRIDGE_PORT


def _beacon_env(repo_root: Path) -> dict[str, str]:
    """What the world server's LAN discovery beacon announces (mars_sim_driver
    beacon.py): the ports the controller app needs, the robot's name file, and
    the checkout's version."""
    described = subprocess.run(
        ["git", "-C", str(repo_root), "describe", "--tags", "--always", "--dirty"],
        capture_output=True,
        text=True,
    )
    return {
        "INNATE_SIM_BEACON_ROSBRIDGE_PORT": str(SIM_ROSBRIDGE_PORT),
        "INNATE_SIM_BEACON_WEBAPP_PORT": str(SIM_HTTPS_PORT),
        "INNATE_SIM_BEACON_ROBOT_INFO": str(repo_root / "data" / "robot_info.json"),
        "INNATE_SIM_BEACON_VERSION": described.stdout.strip() if described.returncode == 0 else "",
    }


def _start_world_server(uv: str, sim_repo: Path, *, bind: str, mujoco_gl: str | None) -> bool:
    """One world-server start attempt; True once it answers pings."""
    bootstrap = (
        "import sys; sys.path.insert(0, 'ros2_ws/src/mars_bot/mars_sim_driver'); "
        "from mars_sim_driver.world_server import main; main()"
    )
    env = os.environ.copy()
    env["VIRTUAL_MARS_ASSETS"] = str(sim_repo / "assets")
    env.update(_beacon_env(sim_repo.parent))
    if mujoco_gl:
        env["MUJOCO_GL"] = mujoco_gl
    with WORLD_SERVER_LOG_PATH.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [
                uv,
                "run",
                "--project",
                str(sim_repo),
                "python",
                "-c",
                bootstrap,
                "--bind",
                bind,
                "--port",
                str(WORLD_SERVER_PORT),
                "--state-port",
                str(WORLD_STATE_PORT),
            ]
            + _render_scale_args(),
            cwd=sim_repo.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    WORLD_SERVER_PID_PATH.write_text(f"{proc.pid}\n", encoding="utf-8")
    WORLD_SERVER_PORTS_PATH.write_text(f"{WORLD_SERVER_PORT} {WORLD_STATE_PORT}\n", encoding="utf-8")
    # Patient while alive: the first run downloads the Python env (uv sync)
    # and builds the MuJoCo model, which takes minutes on slow machines --
    # killing a live process on a stopwatch misdiagnosed a Raspberry Pi's
    # env download as a GL failure. Heartbeats keep the wait visible.
    deadline = time.monotonic() + 900.0
    next_note = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _world_server_ping(WORLD_SERVER_PORT):
            # Stamped now, not at the spawn: `down` tells our server from a
            # later squatter by age against this record, and uv only starts
            # the python that binds after syncing the env -- minutes on a
            # first run -- so the spawn-time stamp would disown our own server.
            WORLD_SERVER_PORTS_PATH.touch()
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
    WORLD_SERVER_PORTS_PATH.unlink(missing_ok=True)
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


def _world_server_pids(ports: list[int]) -> set[int]:
    """Live world-server processes, found by the ports they are listening on.

    Not by their command line: ps joins argv with spaces and quotes nothing, so
    no amount of care tells an argument boundary from a space inside a checkout
    path. The ports are per-checkout and are the thing `down` has to free, so
    they identify the server exactly -- and unlike the pid file they survive an
    interrupted `up`. The module check keeps an unrelated squatter on the port
    from being signalled.
    """
    pids: set[int] = set()
    for port in ports:
        found = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, check=False
        )
        for pid_str in found.stdout.split():
            with contextlib.suppress(ValueError):
                pids.add(int(pid_str))
    pids.discard(os.getpid())
    return {pid for pid in pids if _is_world_server(pid)}


def _is_world_server(pid: int) -> bool:
    """Whether pid is running the world server's bootstrap -- the module path,
    not "world_server", so anything merely mentioning the function (this
    launcher included) cannot match."""
    out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    return "mars_sim_driver.world_server" in out.stdout


def _started_before(pid: int, when: float) -> bool:
    """Whether pid has been running since before `when`.

    The ports record can outlive the server that wrote it -- a crash leaves it
    behind -- and another checkout may then bind those same ports. It would be
    a stranger that started after the record was written, so its age tells the
    two apart. Unreadable age is treated as ours: failing to stop our own
    server leaves the ports held, which is the louder failure.
    """
    out = subprocess.run(["ps", "-p", str(pid), "-o", "etime="], capture_output=True, text=True, check=False)
    field = out.stdout.strip()
    if not field:
        return True
    # [[DD-]hh:]mm:ss
    days, _, clock = field.rpartition("-")
    parts = [float(part) for part in clock.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    seconds = parts[0] * 3600 + parts[1] * 60 + parts[2] + (float(days) * 86400 if days else 0)
    return time.time() - seconds <= when + WORLD_PORTS_RECORD_GRACE_S


# ps reports whole seconds, so a server of ours can look a moment younger than
# its own record.
WORLD_PORTS_RECORD_GRACE_S = 5.0


def _world_ports_free(ports: list[int], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(_host_port_free(port, udp=False, reuse=True) for port in ports):
            return True
        time.sleep(0.2)
    return False


def stop_world_server() -> None:
    # Only the ports this checkout recorded when it started a server. The
    # configured ports are no evidence of ownership -- a checkout that never
    # started one would take them as licence to kill whoever holds the
    # defaults, which is another checkout's sim.
    try:
        ports = [int(port) for port in WORLD_SERVER_PORTS_PATH.read_text().split()]
    except (OSError, ValueError):
        ports = []
    targets = _world_server_pids(ports) if ports else set()
    # The record is stamped once the server answers, so a holder that started
    # after it took the ports after ours died: not ours to stop.
    with contextlib.suppress(OSError):
        recorded_at = WORLD_SERVER_PORTS_PATH.stat().st_mtime
        targets = {pid for pid in targets if _started_before(pid, recorded_at)}
    if targets:
        for pid in targets:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        # An immediate `up` re-checks the port block: return only after
        # MuJoCo's teardown has actually released the sockets, or the next
        # run refuses on "world state stream already in use".
        if _world_ports_free(ports, 15.0):
            log("Stopped host world server.")
        else:
            # Escalate to the same pids, not to whoever holds the ports now: if
            # ours died during the wait and a stranger took them, that stranger
            # is not ours to kill.
            for pid in targets:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            if _world_ports_free(ports, 5.0):
                log("Stopped host world server (forced).")
            else:
                warn(f"Something else holds the world ports (lsof -nP -iTCP:{ports[-1]}); `{CLI_SIM} up` may refuse.")
    with contextlib.suppress(OSError):  # read-only fs: the kill still counts
        WORLD_SERVER_PID_PATH.unlink(missing_ok=True)
        WORLD_SERVER_PORTS_PATH.unlink(missing_ok=True)
        WORLD_SERVER_MODEL_DIGEST_PATH.unlink(missing_ok=True)


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
    if config["brain_backend"] == NO_BACKEND:
        llm_level, llm_label = "warn", "no key"
    elif config["brain_backend"] == INNATE_BACKEND:
        llm_level, llm_label = "healthy", "innate proxy"
    else:
        llm_level, llm_label = "healthy", "gemini key"

    if all(level == "healthy" for level in (world_level, sim_level, transport_level, brain_level, llm_level)):
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
        "llm_level": llm_level,
        "llm_label": llm_label,
        "stack_level": stack_mood[0],
        "stack_label": stack_mood[1],
        "health_score": health_score(stack_mood[0]),
        "system_summary": system_summary,
    }
