#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from dashboard import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    NC,
    RED,
    USE_COLOR,
    YELLOW,
    DashboardCallbacks,
    DashboardOptions,
    print_banner,
    print_status,
    watch_dashboard,
)

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

SCRIPT_PATH = Path(__file__).resolve()
LAUNCHER_DIR = SCRIPT_PATH.parent
DEV_DIR = LAUNCHER_DIR.parent
REPO_ROOT = DEV_DIR.parent
WORKSPACE_ROOT = REPO_ROOT.parent
ENV_PATH = REPO_ROOT / ".env"
ENV_TEMPLATE_PATH = REPO_ROOT / ".env.template"
OS_CONFIG_PATH = REPO_ROOT / "config" / "os.toml"
OS_CONFIG_TEMPLATE_PATH = REPO_ROOT / "config" / "os.toml.template"
SIM_CONFIG_PATH = REPO_ROOT / "sim" / "config.toml"
SIM_CONFIG_TEMPLATE_PATH = REPO_ROOT / "sim" / "config.toml.template"
STATE_DIR = LAUNCHER_DIR / ".state"
LOG_DIR = STATE_DIR / "logs"
SIM_LOG_PATH = LOG_DIR / "simulator.log"
BOOTSTRAP_LOG_PATH = LOG_DIR / "bootstrap.log"
FRONTEND_LOG_PATH = LOG_DIR / "frontend-build.log"
CLOUD_AGENT_LOG_PATH = LOG_DIR / "cloud-agent.log"
COMPOSE_LOG_PATH = LOG_DIR / "compose.log"
OS_BUILD_LOG_PATH = LOG_DIR / "os-build.log"
OS_SESSION_LOG_PATH = LOG_DIR / "os-session.log"
DOWN_LOG_PATH = LOG_DIR / "down.log"
SIM_PID_PATH = STATE_DIR / "simulator.pid"
ROS_INSTALL_STATE_PATH = STATE_DIR / "ros-install.inputs.sha256"
SIM_STARTUP_CHECK_DELAY_SECONDS = 0.25
SIM_HTTP_POLL_SECONDS = 0.25
SIM_HTTP_REQUEST_TIMEOUT_SECONDS = 0.5
OS_SESSION_READY_POLL_SECONDS = 0.25
GENERATED_OS_ENV_PATH = STATE_DIR / "innate-os.env"
GENERATED_CLOUD_ENV_PATH = STATE_DIR / "cloud-agent.env"
HOSTED_MODE = "hosted"
LOCAL_IMAGE_MODE = "local-image"
LOCAL_SOURCE_MODE = "local-source"
LOCAL_MODES = {LOCAL_IMAGE_MODE, LOCAL_SOURCE_MODE}
AUTO_OS_IMAGE = "auto"
LOCAL_OS_IMAGE = "local"
DEFAULT_SIM_OS_IMAGE = "ghcr.io/innate-inc/innate-os-sim-ros"
SIM_IMAGE_INPUT_FILES = (
    ".dockerignore",
    "Dockerfile",
    "Dockerfile.ros-prebuilt",
    "Dockerfile.ros-prebuilt.dockerignore",
    "ros2_ws/apt-dependencies.common.txt",
    "ros2_ws/apt-dependencies.hardware.txt",
    "ros2_ws/apt-dependencies.simulation.txt",
)
ROS_INSTALL_VALIDATION_INPUT_FILES = (
    "scripts/validate_sim_ros_install.zsh",
)
OS_CONTAINER_SERVICE = "innate"
OS_CONTAINER_TMUX_CMD = "./scripts/launch_sim_in_tmux.zsh --detach"
ENV_KEYS_MOVED_TO_OS_CONFIG = {
    "BRAIN_WEBSOCKET_URI",
    "TELEMETRY_URL",
    "CARTESIA_VOICE_ID",
}
LOG_TARGETS = {
    "bootstrap": BOOTSTRAP_LOG_PATH,
    "frontend": FRONTEND_LOG_PATH,
    "cloud-agent": CLOUD_AGENT_LOG_PATH,
    "compose": COMPOSE_LOG_PATH,
    "os-build": OS_BUILD_LOG_PATH,
    "os-session": OS_SESSION_LOG_PATH,
    "simulator": SIM_LOG_PATH,
    "down": DOWN_LOG_PATH,
}
SIM_DATASET_REPOS = {
    "ReplicaCAD_baked_lighting": "https://huggingface.co/datasets/ai-habitat/ReplicaCAD_baked_lighting",
    "ReplicaCAD_dataset": "https://huggingface.co/datasets/ai-habitat/ReplicaCAD_dataset",
}
SIM_REQUIRED_DATA_PATHS = {
    "ReplicaCAD baked lighting stage": Path(
        "data/ReplicaCAD_baked_lighting/stages_uncompressed/Baked_sc0_staging_00.glb"
    ),
    "ReplicaCAD stage config": Path(
        "data/ReplicaCAD_baked_lighting/configs/stages/Baked_sc0_staging_00.stage_config.json"
    ),
    "ReplicaCAD object dataset": Path("data/ReplicaCAD_dataset/objects"),
}

SHOW_LIVE_DASHBOARD_DEFAULT = sys.stdout.isatty()
TMUX_SESSION_NAME = "innate"
CLI_ROOT = "./innate"
CLI_SIM = "./innate sim"
DASHBOARD_OPTIONS = DashboardOptions(
    hosted_mode=HOSTED_MODE,
    local_modes=LOCAL_MODES,
    cli_sim=CLI_SIM,
    state_dir=STATE_DIR,
)

class StackError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"{CYAN}[innate]{NC} {message}")


def success(message: str) -> None:
    print(f"{GREEN}[ok]{NC} {message}")


def warn(message: str) -> None:
    print(f"{YELLOW}[warn]{NC} {message}")


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


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


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    shutil.copyfile(ENV_TEMPLATE_PATH, ENV_PATH)
    warn(f"Created {ENV_PATH} from template. Add your Innate service key there if needed.")


def ensure_config_file(path: Path, template_path: Path) -> None:
    if path.exists():
        return
    shutil.copyfile(template_path, path)
    warn(f"Created {path} from template. Edit it only if you need non-default behavior.")


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        env[key.strip()] = value
    return env


def _strip_toml_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    chars: list[str] = []

    for char in value:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\" and (in_single or in_double):
            chars.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            chars.append(char)
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            chars.append(char)
            continue
        if char == "#" and not in_single and not in_double:
            break
        chars.append(char)

    return "".join(chars).strip()


def _parse_toml_scalar(raw_value: str) -> object:
    value = _strip_toml_comment(raw_value).strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def parse_toml_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if tomllib is not None:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return data if isinstance(data, dict) else {}

    data: dict[str, object] = {}
    current_section: dict[str, object] = data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            if not section_name:
                continue
            current_section = data
            for key in section_name.split("."):
                current_section = current_section.setdefault(key, {})  # type: ignore[assignment]
            continue
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        current_section[key] = _parse_toml_scalar(raw_value)
    return data


def get_nested_value(data: dict[str, object], *keys: str) -> object | None:
    current: object = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def get_nested_str(data: dict[str, object], *keys: str) -> str | None:
    value = get_nested_value(data, *keys)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def get_nested_bool(data: dict[str, object], *keys: str) -> bool | None:
    value = get_nested_value(data, *keys)
    if isinstance(value, bool):
        return value
    return None


def build_os_config_env(os_config: dict[str, object]) -> dict[str, str]:
    env: dict[str, str] = {}
    if websocket_uri := get_nested_str(os_config, "brain", "websocket_uri"):
        env["BRAIN_WEBSOCKET_URI"] = websocket_uri
    if telemetry_url := get_nested_str(os_config, "telemetry", "url"):
        env["TELEMETRY_URL"] = telemetry_url
    if cartesia_voice_id := get_nested_str(os_config, "voice", "cartesia_voice_id"):
        env["CARTESIA_VOICE_ID"] = cartesia_voice_id
    return env


def resolve_repo_path(value: str | None, default_name: str) -> Path:
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
        return path
    return (REPO_ROOT / default_name).resolve()


def require_path(path: Path, label: str) -> Path:
    if not path.exists():
        raise StackError(f"{label} not found at {path}")
    return path


def iter_sim_image_input_files(repo_root: Path) -> list[Path]:
    relative_paths: list[Path] = []
    for raw_path in SIM_IMAGE_INPUT_FILES:
        relative_path = Path(raw_path)
        if (repo_root / relative_path).is_file():
            relative_paths.append(relative_path)

    src_root = repo_root / "ros2_ws" / "src"
    if src_root.exists():
        for path in sorted(src_root.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(repo_root)
            if "__pycache__" in relative_path.parts or relative_path.suffix == ".pyc":
                continue
            relative_paths.append(relative_path)

    return sorted(relative_paths, key=lambda path: path.as_posix())


def compute_sim_image_inputs_hash(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for relative_path in iter_sim_image_input_files(repo_root):
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((repo_root / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def compute_ros_install_validation_hash(repo_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(compute_sim_image_inputs_hash(repo_root).encode("utf-8"))
    for raw_path in ROS_INSTALL_VALIDATION_INPUT_FILES:
        relative_path = Path(raw_path)
        path = repo_root / relative_path
        if not path.is_file():
            continue
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_auto_os_image(repo_root: Path) -> str:
    return f"{DEFAULT_SIM_OS_IMAGE}:inputs-{compute_sim_image_inputs_hash(repo_root)}"


def resolve_os_image_setting(value: str | None, repo_root: Path) -> tuple[str, bool]:
    if value is None or value == AUTO_OS_IMAGE:
        return resolve_auto_os_image(repo_root), True
    if value == LOCAL_OS_IMAGE:
        return "", False
    return value, False


def get_config() -> dict[str, object]:
    ensure_env_file()
    ensure_config_file(OS_CONFIG_PATH, OS_CONFIG_TEMPLATE_PATH)
    ensure_config_file(SIM_CONFIG_PATH, SIM_CONFIG_TEMPLATE_PATH)

    user_env = parse_env_file(ENV_PATH)
    ignored_os_env_keys = sorted(
        key for key in user_env if key in ENV_KEYS_MOVED_TO_OS_CONFIG
    )
    raw_env = {
        key: value
        for key, value in user_env.items()
        if key not in ENV_KEYS_MOVED_TO_OS_CONFIG
    }
    if ignored_os_env_keys:
        warn(
            f"Ignoring deprecated OS config keys in {ENV_PATH.name}: "
            f"{', '.join(ignored_os_env_keys)}. Move them to config/os.toml if still needed."
        )
    os_config = parse_toml_file(OS_CONFIG_PATH)
    sim_config = parse_toml_file(SIM_CONFIG_PATH)
    os_config_env = build_os_config_env(os_config)

    merged_env = dict(raw_env)
    merged_env.update(os_config_env)
    merged_env.setdefault("ROSBRIDGE_URI", "ws://localhost:9090")
    merged_env.setdefault("SIMULATOR_PORT", "8000")

    mode = get_nested_str(sim_config, "cloud_agent", "mode") or HOSTED_MODE
    if mode not in {HOSTED_MODE, LOCAL_IMAGE_MODE, LOCAL_SOURCE_MODE}:
        raise StackError(
            "sim/config.toml cloud_agent.mode must be one of: hosted, local-image, local-source"
        )

    os_repo = require_path(REPO_ROOT, "innate-os repository")
    sim_repo = require_path(REPO_ROOT / "sim", "sim repository")

    cloud_dir_value = get_nested_str(sim_config, "cloud_agent", "source_dir")
    cloud_repo = None
    if cloud_dir_value:
        cloud_repo = resolve_repo_path(cloud_dir_value, "innate-cloud-agent")
    elif (WORKSPACE_ROOT / "innate-cloud-agent").exists():
        cloud_repo = (WORKSPACE_ROOT / "innate-cloud-agent").resolve()

    os_always_build = get_nested_bool(sim_config, "os", "always_build")
    os_pull_image = get_nested_bool(sim_config, "os", "pull_image")
    configured_os_image = get_nested_str(sim_config, "os", "image")
    env_os_image = os.environ.get("INNATE_OS_IMAGE", "").strip() or None
    os_image, os_image_auto = resolve_os_image_setting(
        configured_os_image or env_os_image,
        os_repo,
    )

    return {
        "raw_env": merged_env,
        "user_env": user_env,
        "os_config_env": os_config_env,
        "mode": mode,
        "os_repo": os_repo,
        "sim_repo": sim_repo,
        "cloud_repo": cloud_repo,
        "cloud_port": "8765",
        "cloud_image": get_nested_str(sim_config, "cloud_agent", "image") or "",
        "sim_visualization": get_nested_bool(sim_config, "display", "visualization")
        if get_nested_bool(sim_config, "display", "visualization") is not None
        else False,
        "sim_log_mode": "quiet",
        "sim_args": "--log-everything",
        "os_image": os_image,
        "os_image_auto": os_image_auto,
        "os_pull_image": os_pull_image if os_pull_image is not None else True,
        "os_always_build": os_always_build if os_always_build is not None else False,
    }


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in sorted(values.items()) if value != ""]
    path.write_text("\n".join(lines) + "\n")


def build_os_env(config: dict[str, object]) -> Path:
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    mode = config["mode"]
    cloud_port = config["cloud_port"]

    os_env: dict[str, str] = dict(raw_env)

    if mode in LOCAL_MODES:
        os_env["BRAIN_WEBSOCKET_URI"] = f"ws://host.docker.internal:{cloud_port}"
    elif raw_env.get("BRAIN_WEBSOCKET_URI"):
        os_env["BRAIN_WEBSOCKET_URI"] = raw_env["BRAIN_WEBSOCKET_URI"]
    else:
        os_env.pop("BRAIN_WEBSOCKET_URI", None)

    ensure_state_dir()
    write_env_file(GENERATED_OS_ENV_PATH, os_env)
    return GENERATED_OS_ENV_PATH


def build_cloud_env(config: dict[str, object]) -> Path:
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    cloud_env: dict[str, str] = dict(raw_env)
    cloud_env.setdefault("SKIP_AUTH", "true")
    cloud_env.setdefault("ROBOT_TYPE", "sim")

    ensure_state_dir()
    write_env_file(GENERATED_CLOUD_ENV_PATH, cloud_env)
    return GENERATED_CLOUD_ENV_PATH


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


def dashboard_callbacks() -> DashboardCallbacks:
    return DashboardCallbacks(
        collect_status_snapshot=collect_status_snapshot,
        capture_simulator_logs=capture_simulator_logs,
        capture_os_brain_logs=capture_os_brain_logs,
        capture_agent_logs=capture_agent_logs,
        set_simulator_log_mode=set_simulator_log_mode,
        success=success,
    )


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


def cmd_up(
    config: dict[str, object],
    *,
    watch: bool = SHOW_LIVE_DASHBOARD_DEFAULT,
    sim_visualization_override: bool | None = None,
) -> None:
    started = False
    try:
        if sim_visualization_override is not None:
            config = {**config, "sim_visualization": sim_visualization_override}
        print_banner()
        ensure_dependency("docker")
        os_env_file = build_os_env(config)
        cloud_env_file = build_cloud_env(config)
        sim_python = ensure_sim_setup(config, allow_setup=False)
        ensure_sim_data(config, allow_fetch=False)

        started = True
        start_cloud_agent(config, cloud_env_file)
        ensure_os_container(config, os_env_file)
        start_simulator(config, sim_python)

        simulator_port = config_simulator_port(config)
        log("Waiting for the simulator HTTP endpoint...")
        wait_for_simulator_http(simulator_port)
        log("Waiting for ROS bridge and brain client...")
        if not wait_for_os_runtime_ready(config):
            print_startup_checks(
                config,
                simulator_http_ready=True,
                brain_directive_count=available_agent_count(simulator_port),
            )
            raise StackError(
                "Simulator backend is up, but the OS ROS bridge/brain client did not become ready.\n"
                f"Recent OS log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
            )
        log("Waiting for brain directives...")
        brain_directive_count = wait_for_brain_directives(simulator_port)
        print_startup_checks(
            config,
            simulator_http_ready=True,
            brain_directive_count=brain_directive_count,
        )
        if brain_directive_count <= 0:
            raise StackError(
                "Simulator backend is up, but brain directives never became available.\n"
                f"Recent brain log output:\n{os.linesep.join(capture_os_brain_logs(config, lines=40))}"
            )
        success("Innate sim runtime is up.")
        if watch and sys.stdout.isatty():
            dashboard_result = watch_dashboard(
                config, dashboard_callbacks(), DASHBOARD_OPTIONS
            )
            if dashboard_result == "shutdown":
                print()
                log("Ctrl+C received. Stopping the Innate runtime...")
                cmd_down(config)
        else:
            print_status(config, dashboard_callbacks(), DASHBOARD_OPTIONS)
    except KeyboardInterrupt:
        print()
        if started:
            warn("Interrupted. Stopping the Innate runtime...")
            cmd_down(config)
        else:
            warn("Interrupted before the Innate runtime finished starting.")


def cmd_down(config: dict[str, object]) -> None:
    stop_simulator()
    down_cloud_agent()
    down_os(config)
    log("Innate sim runtime is down.")


def cmd_logs(target: str) -> None:
    if target == "startup":
        found_logs = False
        for name in ("bootstrap", "frontend", "compose", "cloud-agent", "os-build", "os-session", "simulator"):
            path = LOG_TARGETS[name]
            if path.exists():
                found_logs = True
                print(f"{BOLD}{path}{NC}")
                print(tail_file(path, limit=80))
                print()
        if not found_logs:
            warn("No startup logs have been written yet.")
        return

    if target == "brain":
        config = get_config()
        print("\n".join(capture_os_brain_logs(config, lines=60)))
        return

    path = LOG_TARGETS[target]
    print(tail_file(path, limit=120))


def cmd_setup(config: dict[str, object]) -> None:
    print_banner()
    sim_python = ensure_sim_setup(config, allow_setup=True)
    ensure_sim_data(config, allow_fetch=True)
    success("Simulator setup is ready.")
    print(f"OS secrets: {ENV_PATH}")
    print(f"OS config: {OS_CONFIG_PATH}")
    print(f"Sim config: {SIM_CONFIG_PATH}")
    print(f"Simulator Python: {sim_python}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="innate",
        description="Innate local development CLI."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sim_parser = subparsers.add_parser(
        "sim",
        prog=f"{CLI_ROOT} sim",
        help="Set up and run the local simulator-backed runtime",
    )
    sim_subparsers = sim_parser.add_subparsers(dest="sim_command", required=True)
    sim_subparsers.add_parser(
        "setup",
        prog=f"{CLI_SIM} setup",
        help="Prepare the simulator environment, frontend build, and required scene data",
    )
    up_parser = sim_subparsers.add_parser(
        "up",
        prog=f"{CLI_SIM} up",
        help="Start the local simulator-backed runtime",
    )
    up_parser.add_argument(
        "--once",
        action="store_true",
        help="Start the runtime and print a single status snapshot instead of the live dashboard",
    )
    up_parser.add_argument(
        "--vis",
        action="store_true",
        help="Start the simulator with the native visualization window enabled for this run",
    )
    sim_subparsers.add_parser(
        "down",
        prog=f"{CLI_SIM} down",
        help="Stop the local simulator-backed runtime",
    )
    status_parser = sim_subparsers.add_parser(
        "status",
        prog=f"{CLI_SIM} status",
        help="Show current runtime status",
    )
    status_parser.add_argument(
        "mode",
        nargs="?",
        default="panel",
        choices=["panel", "verbose"],
        help="Show the default panel or include extra repo/runtime details",
    )
    logs_parser = sim_subparsers.add_parser(
        "logs",
        prog=f"{CLI_SIM} logs",
        help="Show recent logs",
    )
    logs_parser.add_argument(
        "target",
        nargs="?",
        default="simulator",
        choices=["startup", "bootstrap", "frontend", "compose", "cloud-agent", "os-build", "os-session", "simulator", "brain", "down"],
        help="Which log stream to show",
    )
    return parser


def normalize_argv(argv: list[str]) -> list[str]:
    if not argv:
        return argv

    legacy_commands = {
        "init": "setup",
        "setup": "setup",
        "up": "up",
        "down": "down",
        "status": "status",
        "logs": "logs",
    }
    first = argv[0]
    if first in legacy_commands:
        mapped = legacy_commands[first]
        print(
            f"[deprecated] Use `{CLI_SIM} {mapped}` instead of `{first}`.",
            file=sys.stderr,
        )
        return ["sim", mapped, *argv[1:]]
    return argv


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_argv(sys.argv[1:]))

    try:
        config = get_config()

        if args.command != "sim":
            parser.error(f"Unknown command group: {args.command}")

        if args.sim_command == "setup":
            cmd_setup(config)
        elif args.sim_command == "up":
            cmd_up(
                config,
                watch=not args.once,
                sim_visualization_override=True if args.vis else None,
            )
        elif args.sim_command == "down":
            cmd_down(config)
        elif args.sim_command == "status":
            print_status(
                config,
                dashboard_callbacks(),
                DASHBOARD_OPTIONS,
                verbose=args.mode == "verbose",
            )
        elif args.sim_command == "logs":
            cmd_logs(args.target)
        else:
            parser.error(f"Unknown sim command: {args.sim_command}")
    except StackError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Command failed: {' '.join(exc.cmd)}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return exc.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
