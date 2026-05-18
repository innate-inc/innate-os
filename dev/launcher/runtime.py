from __future__ import annotations

import json
import os
import socket
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from dashboard import BOLD, GREEN, NC, RED, USE_COLOR
from config import (
    BOOTSTRAP_LOG_PATH,
    CLI_SIM,
    CLOUD_AGENT_LOG_PATH,
    COMPOSE_LOG_PATH,
    DOWN_LOG_PATH,
    FRONTEND_LOG_PATH,
    GENERATED_OS_ENV_PATH,
    HOSTED_MODE,
    LAUNCHER_DIR,
    LOCAL_IMAGE_MODE,
    LOCAL_MODES,
    OS_BUILD_LOG_PATH,
    OS_CONTAINER_SERVICE,
    OS_CONTAINER_TMUX_CMD,
    OS_SESSION_LOG_PATH,
    ROS_INSTALL_STATE_PATH,
    SIM_DATASET_REPOS,
    SIM_HTTP_POLL_SECONDS,
    SIM_HTTP_REQUEST_TIMEOUT_SECONDS,
    SIM_LOG_PATH,
    SIM_PID_PATH,
    SIM_REQUIRED_DATA_PATHS,
    SIM_STARTUP_CHECK_DELAY_SECONDS,
    STATE_DIR,
    TMUX_SESSION_NAME,
    StackError,
    compute_ros_install_validation_hash,
    ensure_state_dir,
    log,
    require_path,
    warn,
)


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
        raise StackError(
            f"{failure_message}\nRecent log output:\n{tail_file(log_path, limit=60)}"
        )


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
        raise StackError(
            f"{failure_message}\nRecent log output:\n{tail_file(log_path, limit=60)}"
        )


def ensure_dependency(command: str, label: str | None = None) -> None:
    if shutil.which(command) is None:
        raise StackError(f"Missing dependency: {label or command}")


def python_import_succeeds(python_path: Path, module: str) -> bool:
    result = subprocess.run(
        [str(python_path), "-c", f"import {module}"],
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def ensure_sim_setup(config: dict[str, object], *, allow_setup: bool) -> Path:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    frontend_dir = sim_repo / "frontend"
    dist_index = frontend_dir / "dist" / "index.html"
    sim_python = sim_repo / ".venv" / "bin" / "python"

    ensure_dependency("python3")

    needs_setup = not sim_python.exists()
    if sim_python.exists() and not python_import_succeeds(sim_python, "dotenv"):
        if not allow_setup:
            raise StackError(
                "Simulator virtualenv is incomplete.\n"
                f"Run `{CLI_SIM} setup` to repair {sim_repo / '.venv'}."
            )
        warn("Simulator virtualenv is incomplete. Re-running setup to repair it...")
        needs_setup = True

    if needs_setup:
        if not allow_setup:
            raise StackError(
                "Simulator Python environment is not ready.\n"
                f"Run `{CLI_SIM} setup` before `{CLI_SIM} up`."
            )
        log("Setting up sim Python environment...")
        run_logged(
            ["bash", "./setup.sh"],
            cwd=sim_repo,
            log_path=BOOTSTRAP_LOG_PATH,
            failure_message="Simulator environment setup failed.",
        )

    if not sim_python.exists():
        raise StackError(f"Simulator Python environment was not created at {sim_python}")
    if not python_import_succeeds(sim_python, "dotenv"):
        raise StackError(
            "Simulator Python environment is missing required packages after setup.\n"
            f"Check: {BOOTSTRAP_LOG_PATH}"
        )

    if not dist_index.exists():
        if not allow_setup:
            raise StackError(
                "Simulator frontend build is missing.\n"
                f"Run `{CLI_SIM} setup` before `{CLI_SIM} up`."
            )
        ensure_dependency("yarn")
        if not (frontend_dir / "node_modules").exists():
            log("Installing simulator frontend dependencies...")
            run_logged(
                ["yarn", "install"],
                cwd=frontend_dir,
                log_path=FRONTEND_LOG_PATH,
                failure_message="Simulator frontend dependency install failed.",
            )
        log("Building simulator frontend...")
        run_logged(
            ["yarn", "build"],
            cwd=frontend_dir,
            log_path=FRONTEND_LOG_PATH,
            failure_message="Simulator frontend build failed.",
        )

    return sim_python


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
        failure_message=(
            "Could not pull the prebuilt Innate OS image: "
            f"{shorten_docker_image_ref(image)}"
        ),
        progress_message="Docker is still pulling the Innate OS image.",
        include_recent_log_on_failure=include_pull_log_on_failure,
    )


def ensure_os_container(config: dict[str, object], os_env_file: Path) -> None:
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    os_image = str(config["os_image"]).strip()
    os_image_auto = bool(config["os_image_auto"])
    container_was_running = container_running("innate-dev")

    if container_was_running:
        log("Innate OS dev container already running.")
    else:
        up_cmd = ["docker", "compose", "-f", "docker-compose.dev.yml", "up", "-d"]
        if os_image:
            try:
                ensure_os_image_available(
                    os_image,
                    cwd=os_repo,
                    env=os_compose_env(env_file=os_env_file),
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
                os_image = ""
            else:
                up_cmd.append("--no-build")

        compose_values = {"INNATE_OS_ENV_FILE": str(os_env_file)}
        if os_image:
            compose_values["INNATE_OS_IMAGE"] = os_image
        compose_env = os_compose_env(compose_values, env_file=os_env_file)
        log("Starting Innate OS dev container...")
        run_logged_with_heartbeat(
            up_cmd,
            cwd=os_repo,
            env=compose_env,
            log_path=COMPOSE_LOG_PATH,
            failure_message="Innate OS Docker startup failed.",
            progress_message=(
                "Docker is still preparing the Innate OS container. "
                "First boot or an image rebuild can take a minute."
            ),
        )
    compose_values = {"INNATE_OS_ENV_FILE": str(os_env_file)}
    if os_image:
        compose_values["INNATE_OS_IMAGE"] = os_image
    compose_env = os_compose_env(compose_values, env_file=os_env_file)

    build_cmd = (
        f"INNATE_OS_ALWAYS_BUILD={1 if config['os_always_build'] else 0} "
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
        run_logged(
            os_compose_zsh_cmd(build_cmd),
            cwd=os_repo,
            env=compose_env,
            log_path=OS_BUILD_LOG_PATH,
            failure_message="Innate OS ROS workspace build failed.",
        )
        ensure_state_dir()
        ROS_INSTALL_STATE_PATH.write_text(f"{ros_inputs_hash}\n", encoding="utf-8")

    log("Launching ROS simulation nodes inside the OS container...")
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


def down_os(config: dict[str, object]) -> None:
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = os_compose_env()
    ensure_state_dir()
    with DOWN_LOG_PATH.open("a", encoding="utf-8") as log_file:
        subprocess.run(
            ["docker", "compose", "-f", "docker-compose.dev.yml", "down"],
            cwd=os_repo,
            env=compose_env,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            check=False,
        )


def start_cloud_agent(config: dict[str, object], cloud_env_file: Path) -> None:
    mode = config["mode"]
    if mode == HOSTED_MODE:
        log("Cloud agent mode is hosted. Skipping local cloud-agent startup.")
        return

    ensure_dependency("docker")

    base_env = {
        "STACK_CLOUD_AGENT_PORT": str(config["cloud_port"]),
        "STACK_CLOUD_AGENT_ENV_FILE": str(cloud_env_file),
    }

    if mode == LOCAL_IMAGE_MODE:
        image = str(config["cloud_image"]).strip()
        if not image:
            raise StackError(
                "sim/config.toml must set cloud_agent.image when cloud_agent.mode = 'local-image'."
            )
        log(f"Starting local cloud-agent image {image}...")
        compose_env = docker_compose_env({**base_env, "STACK_CLOUD_AGENT_IMAGE": image})
        run_logged(
            [
                "docker",
                "compose",
                "-p",
                "stack-cloud",
                "-f",
                "compose.cloud-agent.image.yml",
                "up",
                "-d",
            ],
            cwd=LAUNCHER_DIR,
            env=compose_env,
            log_path=CLOUD_AGENT_LOG_PATH,
            failure_message="Local cloud-agent image startup failed.",
        )
        return

    cloud_repo: Path | None = config["cloud_repo"]  # type: ignore[assignment]
    if cloud_repo is None:
        raise StackError(
            "sim/config.toml must set cloud_agent.source_dir when cloud_agent.mode = 'local-source'."
        )
    require_path(cloud_repo, "innate-cloud-agent repository")
    log(f"Starting local cloud-agent from source at {cloud_repo}...")
    compose_env = docker_compose_env(
        {**base_env, "STACK_CLOUD_AGENT_SOURCE_DIR": str(cloud_repo)}
    )
    run_logged(
        [
            "docker",
            "compose",
            "-p",
            "stack-cloud",
            "-f",
            "compose.cloud-agent.source.yml",
            "up",
            "-d",
            "--build",
        ],
            cwd=LAUNCHER_DIR,
        env=compose_env,
        log_path=CLOUD_AGENT_LOG_PATH,
        failure_message="Local cloud-agent source startup failed.",
    )


def down_cloud_agent() -> None:
    subprocess.run(
        ["docker", "rm", "-f", "stack-cloud-agent"],
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["docker", "network", "rm", "stack-cloud_default"],
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        text=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    status = result.stdout.strip()
    if not status:
        return False
    return not status.startswith("Z")


def read_sim_pid() -> int | None:
    if not SIM_PID_PATH.exists():
        return None
    try:
        pid = int(SIM_PID_PATH.read_text().strip())
    except ValueError:
        return None
    if pid_is_running(pid):
        return pid
    SIM_PID_PATH.unlink(missing_ok=True)
    return None


def stop_simulator() -> None:
    pid = read_sim_pid()
    if pid is None:
        return
    log("Stopping simulator backend...")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not pid_is_running(pid):
            break
        time.sleep(0.25)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            os.kill(pid, signal.SIGKILL)
    SIM_PID_PATH.unlink(missing_ok=True)


def start_simulator(config: dict[str, object], sim_python: Path) -> None:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    existing_pid = read_sim_pid()
    if existing_pid is not None:
        log(f"Simulator backend already running (pid {existing_pid}).")
        return

    ensure_state_dir()
    env = os.environ.copy()
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    env["ROSBRIDGE_URI"] = raw_env.get("ROSBRIDGE_URI", "ws://localhost:9090")
    env["SIMULATOR_PORT"] = raw_env.get("SIMULATOR_PORT", "8000")
    env["SIM_LOG_MODE"] = str(config.get("sim_log_mode", "quiet"))

    sim_args = shlex.split(str(config["sim_args"]))
    if config.get("sim_visualization") and "--vis" not in sim_args and "-v" not in sim_args:
        sim_args.append("--vis")

    cmd = [str(sim_python), "main.py", *sim_args]
    log("Starting simulator backend...")
    with SIM_LOG_PATH.open("ab") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=sim_repo,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    SIM_PID_PATH.write_text(f"{proc.pid}\n")
    time.sleep(SIM_STARTUP_CHECK_DELAY_SECONDS)
    if proc.poll() is not None:
        SIM_PID_PATH.unlink(missing_ok=True)
        tail = tail_file(SIM_LOG_PATH)
        raise StackError(
            "Simulator backend exited immediately.\n"
            f"Recent log output:\n{tail}"
        )


def tail_file(path: Path, limit: int = 40) -> str:
    if not path.exists():
        return "<no log output yet>"
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def docker_compose_cmd(*parts: str) -> list[str]:
    return ["docker", "compose", "-f", "docker-compose.dev.yml", *parts]


def os_compose_exec_cmd(*parts: str) -> list[str]:
    return docker_compose_cmd("exec", "-T", OS_CONTAINER_SERVICE, *parts)


def os_compose_zsh_cmd(command: str) -> list[str]:
    return os_compose_exec_cmd("zsh", "-lc", command)


def capture_command_output(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    return (result.stdout or result.stderr or "").strip()


def command_succeeds(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> bool:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def tcp_port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def collect_os_process_status(config: dict[str, object]) -> dict[str, bool]:
    os_running = container_running("innate-dev")
    status = {
        "os_running": os_running,
        "os_session_running": False,
        "rosbridge_process_live": False,
        "brain_process_live": False,
    }
    if not os_running:
        return status

    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = os_compose_env()
    output = capture_command_output(
        os_compose_zsh_cmd(
            f"tmux has-session -t {shlex.quote(TMUX_SESSION_NAME)} >/dev/null 2>&1; "
            "echo tmux=$?; "
            "pgrep -f rws_server >/dev/null; echo rosbridge=$?; "
            "pgrep -f brain_client_node.py >/dev/null; echo brain=$?"
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
    return status


def config_simulator_port(config: dict[str, object]) -> str:
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    return str(raw_env.get("SIMULATOR_PORT", "8000"))


def collect_runtime_probe(
    config: dict[str, object],
    *,
    simulator_http_ready: bool | None = None,
) -> dict[str, object]:
    simulator_port = config_simulator_port(config)
    os_status = collect_os_process_status(config)
    sim_running = (
        bool(simulator_http_ready)
        if simulator_http_ready is not None
        else simulator_ready(simulator_port)
    )
    rosbridge_live = (
        os_status["os_session_running"]
        and os_status["rosbridge_process_live"]
        and tcp_port_open(9090)
    )
    agent_running = (
        True if config["mode"] == HOSTED_MODE else container_running("stack-cloud-agent")
    )
    metrics = fetch_simulator_metrics(simulator_port) if sim_running else {}
    backend_status = fetch_brain_backend_status(simulator_port) if sim_running else {}
    backend_level, backend_label = health_from_brain_backend(
        backend_status, str(config["mode"])
    )
    return {
        "simulator_port": simulator_port,
        "os_status": os_status,
        "sim_running": sim_running,
        "rosbridge_live": rosbridge_live,
        "agent_running": agent_running,
        "metrics": metrics,
        "backend_status": backend_status,
        "backend_level": backend_level,
        "backend_label": backend_label,
    }


def os_runtime_ready(config: dict[str, object]) -> bool:
    probe = collect_runtime_probe(config, simulator_http_ready=False)
    os_status: dict[str, bool] = probe["os_status"]  # type: ignore[assignment]
    return (
        os_status["os_session_running"]
        and os_status["rosbridge_process_live"]
        and os_status["brain_process_live"]
        and bool(probe["rosbridge_live"])
    )


def wait_for_os_runtime_ready(
    config: dict[str, object], *, timeout_seconds: float = 8.0
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if os_runtime_ready(config):
            return True
        time.sleep(OS_SESSION_READY_POLL_SECONDS)
    return False


def format_startup_check(ok: bool, label: str, detail: str) -> str:
    icon = "✓" if ok else "✗"
    color = GREEN if ok else RED
    return f"  {color}{icon}{NC} {BOLD}{label}:{NC} {detail}"


def print_startup_checks(
    config: dict[str, object],
    *,
    simulator_http_ready: bool,
    brain_directive_count: int,
) -> None:
    probe = collect_runtime_probe(config, simulator_http_ready=simulator_http_ready)
    simulator_port = str(probe["simulator_port"])
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
            "ws://localhost:9090 live"
            if probe["rosbridge_live"]
            else "not accepting connections",
        ),
        (
            os_status["brain_process_live"],
            "Brain process",
            "brain_client_node.py running"
            if os_status["brain_process_live"]
            else "brain_client_node.py missing",
        ),
        (
            brain_directive_count > 0,
            "Brain directives",
            f"{brain_directive_count} loaded"
            if brain_directive_count > 0
            else "not available yet",
        ),
        (
            simulator_http_ready,
            "Simulator HTTP",
            f"http://localhost:{simulator_port} ready"
            if simulator_http_ready
            else f"http://localhost:{simulator_port} not ready",
        ),
        (
            probe["backend_level"] == "healthy",
            "Hosted backend",
            str(probe["backend_label"]),
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
    if not container_running("stack-cloud-agent"):
        return ["Local cloud-agent is not running."]
    output = capture_command_output(
        ["docker", "logs", "--tail", str(lines), "stack-cloud-agent"]
    )
    if not output:
        return ["No local cloud-agent output yet."]
    return output.splitlines()[-lines:]


def capture_simulator_logs(
    simulator_live: bool,
    *,
    lines: int = 18,
) -> list[str]:
    if not SIM_LOG_PATH.exists():
        if simulator_live:
            return [
                "Simulator is healthy.",
                "No launcher-owned simulator log exists for this run yet.",
            ]
        return ["Simulator log not created yet."]
    raw_lines = SIM_LOG_PATH.read_text(errors="replace").splitlines()
    selected = raw_lines[-lines:]
    return selected or ["Simulator log is empty."]


def pick_primary_fps(metrics: dict[str, object]) -> float:
    fps_by_camera = metrics.get("fps_by_camera", {}) if isinstance(metrics, dict) else {}
    if not isinstance(fps_by_camera, dict):
        return 0.0
    for camera_name in ("first_person", "chase"):
        value = fps_by_camera.get(camera_name)
        if isinstance(value, (float, int)):
            return float(value)
    return 0.0


def pick_latest_frame_age(metrics: dict[str, object]) -> float | None:
    latest_frame_age = (
        metrics.get("latest_frame_age_by_camera", {}) if isinstance(metrics, dict) else {}
    )
    if not isinstance(latest_frame_age, dict):
        return None
    ages = [
        float(value)
        for value in latest_frame_age.values()
        if isinstance(value, (float, int))
    ]
    if not ages:
        return None
    return min(ages)


def summarize_queue_pressure(queue_sizes: object) -> tuple[str, str, int]:
    if not isinstance(queue_sizes, dict) or not queue_sizes:
        return ("unknown", "n/a", 0)

    tracked_keys = (
        "sensor_to_agent",
        "sim_to_agent",
        "agent_to_sim",
        "sim_to_web",
        "chat_to_bridge",
        "chat_from_bridge",
    )
    numeric_sizes = [
        int(queue_sizes.get(key, 0))
        for key in tracked_keys
        if isinstance(queue_sizes.get(key, 0), int)
    ]
    max_depth = max(numeric_sizes, default=0)
    total_depth = sum(numeric_sizes)

    if max_depth >= 25 or total_depth >= 40:
        return ("error", "high", max_depth)
    if max_depth >= 8 or total_depth >= 15:
        return ("warn", "elevated", max_depth)
    return ("healthy", "light", max_depth)


def queue_load_score(level: str, max_depth: int) -> float:
    if level == "error":
        return min(100.0, 65.0 + max_depth * 1.5)
    if level == "warn":
        return min(100.0, 35.0 + max_depth * 2.0)
    return min(100.0, max_depth * 4.0)


def health_score(level: str) -> float:
    if level == "healthy":
        return 100.0
    if level == "warn":
        return 60.0
    return 20.0


def health_from_video(sim_running: bool, fps: float, frame_age: float | None) -> tuple[str, str]:
    if not sim_running:
        return ("error", "offline")
    if fps >= 8.0 and (frame_age is None or frame_age <= 0.25):
        return ("healthy", "smooth")
    if fps >= 2.0:
        return ("warn", "starting")
    return ("error", "stalled")


def health_from_transport(
    rosbridge_live: bool, queue_sizes: object
) -> tuple[str, str, str, int]:
    if not rosbridge_live:
        return ("error", "down", "n/a", 0)
    level, label, max_depth = summarize_queue_pressure(queue_sizes)
    return (level, "live", label, max_depth)


def missing_sim_data_paths(sim_repo: Path) -> list[tuple[str, Path]]:
    missing: list[tuple[str, Path]] = []
    for label, relative_path in SIM_REQUIRED_DATA_PATHS.items():
        candidate = sim_repo / relative_path
        if not candidate.exists():
            missing.append((label, candidate))
    return missing


def ensure_sim_data(config: dict[str, object], *, allow_fetch: bool) -> None:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    missing_before = missing_sim_data_paths(sim_repo)
    if not missing_before:
        return

    if not allow_fetch:
        details = "\n".join(f"- {label}: {path}" for label, path in missing_before)
        raise StackError(
            "Required simulator scene data is missing.\n"
            f"{details}\nRun `{CLI_SIM} setup` to download it.\n"
            f"See: {sim_repo / 'data' / 'README.md'}"
        )

    ensure_dependency("git")
    if not command_succeeds(["git", "lfs", "version"], cwd=sim_repo):
        raise StackError(
            "Git LFS is required to download simulator scene data automatically.\n"
            "Install it first, for example: `brew install git-lfs && git lfs install`.\n"
            f"Then rerun `{CLI_SIM} setup`."
        )

    data_dir = sim_repo / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    log("Ensuring required simulator scene data is present...")
    run_logged(
        ["git", "lfs", "install"],
        cwd=sim_repo,
        log_path=BOOTSTRAP_LOG_PATH,
        failure_message="Git LFS setup failed for simulator data bootstrap.",
    )

    for dataset_name, dataset_url in SIM_DATASET_REPOS.items():
        dataset_dir = data_dir / dataset_name
        if not dataset_dir.exists():
            log(f"Downloading simulator dataset {dataset_name}...")
            run_logged(
                ["git", "clone", dataset_url],
                cwd=data_dir,
                log_path=BOOTSTRAP_LOG_PATH,
                failure_message=f"Downloading {dataset_name} failed.",
            )
            continue

        if command_succeeds(["git", "rev-parse", "--is-inside-work-tree"], cwd=dataset_dir):
            log(f"Refreshing simulator dataset {dataset_name} via Git LFS...")
            run_logged(
                ["git", "lfs", "pull"],
                cwd=dataset_dir,
                log_path=BOOTSTRAP_LOG_PATH,
                failure_message=f"Fetching large files for {dataset_name} failed.",
            )

    missing_after = missing_sim_data_paths(sim_repo)
    if missing_after:
        details = "\n".join(f"- {label}: {path}" for label, path in missing_after)
        raise StackError(
            "Required simulator scene data is still missing after automatic download.\n"
            f"{details}\nSee: {sim_repo / 'data' / 'README.md'}"
        )


def sim_endpoint(port: str, path: str) -> str:
    return f"http://localhost:{port}/{path.lstrip('/')}"


def request_json(
    url: str,
    *,
    timeout: float = 2.0,
    payload: dict[str, object] | None = None,
    method: str = "GET",
) -> dict[str, object]:
    data = None
    headers = {}
    request: str | Request = url
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if method != "GET" or payload is not None:
        request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return {}
            body = json.loads(response.read().decode("utf-8"))
            return body if isinstance(body, dict) else {}
    except (URLError, TimeoutError, json.JSONDecodeError):
        return {}


def sim_get_json(port: str, path: str, *, timeout: float = 2.0) -> dict[str, object]:
    return request_json(sim_endpoint(port, path), timeout=timeout)


def sim_post_json(
    port: str,
    path: str,
    payload: dict[str, object],
    *,
    timeout: float = 2.0,
) -> dict[str, object]:
    return request_json(
        sim_endpoint(port, path),
        timeout=timeout,
        payload=payload,
        method="POST",
    )


def wait_for_simulator_http(port: str, timeout_seconds: float = 90.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if read_sim_pid() is None:
            tail = tail_file(SIM_LOG_PATH, limit=80)
            hint = ""
            if "ReplicaCAD_baked_lighting" in tail or "ReplicaCAD_dataset" in tail:
                hint = "\nHint: required simulator scene data is missing. See sim/data/README.md."
            raise StackError(
                "Simulator backend exited while waiting for the HTTP endpoint.\n"
                f"Recent log output:\n{tail}{hint}"
            )
        payload = sim_get_json(
            port,
            "video_feeds_ready",
            timeout=SIM_HTTP_REQUEST_TIMEOUT_SECONDS,
        )
        if payload.get("ready"):
            return
        time.sleep(SIM_HTTP_POLL_SECONDS)
    tail = tail_file(SIM_LOG_PATH, limit=80)
    raise StackError(
        "Timed out waiting for the simulator HTTP endpoint.\n"
        f"Recent log output:\n{tail}"
    )


def simulator_ready(port: str) -> bool:
    return bool(sim_get_json(port, "video_feeds_ready").get("ready"))


def fetch_simulator_metrics(port: str) -> dict[str, object]:
    return sim_get_json(port, "stack_metrics")


def fetch_brain_backend_status(port: str) -> dict[str, object]:
    payload = fetch_available_agents_payload(port)
    status = payload.get("brain_backend_status")
    return status if isinstance(status, dict) else {}


def fetch_available_agents_payload(port: str) -> dict[str, object]:
    return sim_get_json(port, "get_available_agents")


def available_agent_count(port: str) -> int:
    payload = fetch_available_agents_payload(port)
    agents = payload.get("agents")
    return len(agents) if isinstance(agents, list) else 0


def wait_for_brain_directives(port: str, *, timeout_seconds: float = 30.0) -> int:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        count = available_agent_count(port)
        if count > 0:
            return count
        time.sleep(OS_SESSION_READY_POLL_SECONDS)
    return 0


def health_from_brain_backend(
    status: dict[str, object], mode: str
) -> tuple[str, str]:
    if not status:
        return ("warn", "unknown") if mode == HOSTED_MODE else ("warn", "not reported")

    connected = bool(status.get("connected", False))
    state = str(status.get("state", "unknown") or "unknown")
    message = str(status.get("message", "") or "")
    label = state.replace("_", " ")

    if connected:
        return "healthy", "connected"
    if state == "invalid_config" and "KEY" in message.upper():
        return "error", "missing key"
    if state == "invalid_config":
        return "error", "invalid config"
    if state in {"connection_error", "backend_error", "disconnected", "stopped"}:
        return "error", label
    if state in {"unknown", "starting", "configured", "connecting", "authenticating"}:
        return "warn", label
    return "warn", label


def set_simulator_log_mode(port: str, mode: str) -> bool:
    return sim_post_json(port, "sim_log_config", {"mode": mode}).get("mode") == mode


def container_running(container_name: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
        text=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def collect_status_snapshot(config: dict[str, object]) -> dict[str, object]:
    probe = collect_runtime_probe(config)
    simulator_port = str(probe["simulator_port"])
    os_status: dict[str, bool] = probe["os_status"]  # type: ignore[assignment]
    os_running = os_status["os_running"]
    os_session_running = os_status["os_session_running"]
    rosbridge_process_live = os_status["rosbridge_process_live"]
    brain_process_live = os_status["brain_process_live"]
    agent_running = bool(probe["agent_running"])
    sim_running = bool(probe["sim_running"])
    rosbridge_live = bool(probe["rosbridge_live"])
    metrics: dict[str, object] = probe["metrics"]  # type: ignore[assignment]
    backend_status: dict[str, object] = probe["backend_status"]  # type: ignore[assignment]
    backend_level = str(probe["backend_level"])
    backend_label = str(probe["backend_label"])
    queue_sizes = metrics.get("queue_sizes", {}) if isinstance(metrics, dict) else {}
    sim_log_mode = (
        str(metrics.get("sim_log_mode", config.get("sim_log_mode", "quiet")))
        if isinstance(metrics, dict)
        else str(config.get("sim_log_mode", "quiet"))
    )
    primary_fps = pick_primary_fps(metrics)
    frame_age = pick_latest_frame_age(metrics)
    video_level, video_label = health_from_video(sim_running, primary_fps, frame_age)
    transport_level, transport_label, queue_pressure, queue_peak = health_from_transport(
        rosbridge_live, queue_sizes
    )
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
    agent_level = (
        "healthy"
        if config["mode"] == HOSTED_MODE or agent_running
        else "warn"
    )
    agent_label = "hosted" if config["mode"] == HOSTED_MODE else ("online" if agent_running else "offline")

    if all(level == "healthy" for level in (video_level, transport_level, brain_level, backend_level, agent_level)):
        stack_mood = ("healthy", "LIVE")
    elif any(level == "error" for level in (video_level, transport_level, brain_level, backend_level)):
        stack_mood = ("error", "DEGRADED")
    else:
        stack_mood = ("warn", "WARMING")

    chat_load = "n/a"
    if isinstance(queue_sizes, dict) and queue_sizes:
        chat_load = (
            f"to {queue_sizes.get('chat_to_bridge', 0)} / "
            f"from {queue_sizes.get('chat_from_bridge', 0)}"
        )
    frame_age_ms = (frame_age or 0.0) * 1000.0
    queue_summary = "n/a"
    if isinstance(queue_sizes, dict) and queue_sizes:
        queue_summary = (
            f"s->agent {queue_sizes.get('sensor_to_agent', 0)} | "
            f"sim->agent {queue_sizes.get('sim_to_agent', 0)} | "
            f"agent->sim {queue_sizes.get('agent_to_sim', 0)} | "
            f"web {queue_sizes.get('sim_to_web', 0)}"
        )

    system_summary = (
        f"os {'up' if os_running else 'down'} | "
        f"sim {'up' if sim_running else 'down'} | "
        f"brain {'ok' if brain_level == 'healthy' else brain_label} | "
        f"backend {'ok' if backend_level == 'healthy' else backend_label}"
    )

    return {
        "simulator_port": simulator_port,
        "os_running": os_running,
        "os_session_running": os_session_running,
        "agent_running": agent_running,
        "sim_running": sim_running,
        "rosbridge_live": rosbridge_live,
        "rosbridge_process_live": rosbridge_process_live,
        "brain_process_live": brain_process_live,
        "primary_fps": primary_fps,
        "frame_age": frame_age,
        "frame_age_ms": frame_age_ms,
        "video_level": video_level,
        "video_label": video_label,
        "transport_level": transport_level,
        "transport_label": transport_label,
        "queue_pressure": queue_pressure,
        "queue_peak": queue_peak,
        "queue_load_score": queue_load_score(transport_level, queue_peak),
        "brain_level": brain_level,
        "brain_label": brain_label,
        "backend_status": backend_status,
        "backend_level": backend_level,
        "backend_label": backend_label,
        "agent_level": agent_level,
        "agent_label": agent_label,
        "stack_level": stack_mood[0],
        "stack_label": stack_mood[1],
        "health_score": health_score(stack_mood[0]),
        "queue_sizes": queue_sizes,
        "queue_summary": queue_summary,
        "chat_load": chat_load,
        "system_summary": system_summary,
        "sim_log_mode": sim_log_mode,
    }
