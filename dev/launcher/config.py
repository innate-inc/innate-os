from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from dashboard import CYAN, GREEN, NC, YELLOW

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
DEFAULT_HOSTED_BRAIN_WEBSOCKET_URI = "wss://agent-v1.innate.bot"
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
SECRET_ENV_KEYS = (
    "INNATE_SERVICE_KEY",
)
SECRET_ENV_PLACEHOLDERS = {
    "INNATE_SERVICE_KEY": {
        "",
        "your_service_key_here",
        "your-innate-service-key",
        "your-generated-token-here",
    },
}
SECRET_ENV_MIN_LENGTHS = {
    "INNATE_SERVICE_KEY": 16,
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


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    shutil.copyfile(ENV_TEMPLATE_PATH, ENV_PATH)
    warn(f"Created {ENV_PATH} from template.")


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


def is_configured_secret_value(key: str, value: str | None) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    if stripped in SECRET_ENV_PLACEHOLDERS.get(key, {""}):
        return False
    minimum_length = SECRET_ENV_MIN_LENGTHS.get(key, 1)
    return len(stripped) >= minimum_length


def parse_toml_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    return data if isinstance(data, dict) else {}


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
    if telemetry_url := get_nested_str(os_config, "telemetry", "url"):
        env["TELEMETRY_URL"] = telemetry_url
    if cartesia_voice_id := get_nested_str(os_config, "voice", "cartesia_voice_id"):
        env["CARTESIA_VOICE_ID"] = cartesia_voice_id
    return env


def resolve_brain_websocket_uri(
    mode: str,
    cloud_port: str,
    os_config: dict[str, object],
) -> str:
    if mode in LOCAL_MODES:
        return f"ws://host.docker.internal:{cloud_port}"
    return (
        get_nested_str(os_config, "brain", "websocket_uri")
        or DEFAULT_HOSTED_BRAIN_WEBSOCKET_URI
    )


def resolve_brain_client_version(repo_root: Path) -> str:
    def git_output(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            text=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    exact_tag = git_output("describe", "--exact-match", "--tags", "HEAD")
    if exact_tag:
        return exact_tag.splitlines()[0].strip()

    tags = git_output("tag", "--list", "--sort=-version:refname")
    if tags:
        return f"{tags.splitlines()[0].strip()}-dev"

    return "0.3.0-dev"


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
            raise StackError(f"ROS install validation script is missing: {relative_path}")
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
    for key in SECRET_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if is_configured_secret_value(key, value):
            raw_env[key] = value
    if ignored_os_env_keys:
        warn(
            f"Ignoring deprecated OS config keys in {ENV_PATH.name}: "
            f"{', '.join(ignored_os_env_keys)}. Move them to config/os.toml if still needed."
        )
    os_config = parse_toml_file(OS_CONFIG_PATH)
    sim_config = parse_toml_file(SIM_CONFIG_PATH)
    cloud_port = "8765"
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
        "cloud_port": cloud_port,
        "brain_websocket_uri": resolve_brain_websocket_uri(
            mode,
            cloud_port,
            os_config,
        ),
        "brain_client_version": resolve_brain_client_version(os_repo),
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
    os_env: dict[str, str] = dict(raw_env)

    ensure_state_dir()
    write_env_file(GENERATED_OS_ENV_PATH, os_env)
    return GENERATED_OS_ENV_PATH


def build_cloud_env(config: dict[str, object]) -> Path:
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    cloud_env: dict[str, str] = dict(raw_env)
    cloud_env.setdefault("SKIP_AUTH", "true")
    cloud_env.setdefault("ROBOT_TYPE", "sim")
    cloud_env.setdefault("PORT", str(config["cloud_port"]))
    cloud_env.setdefault("DEFAULT_ROBOT_TOKEN", "local-dev-robot-token")
    cloud_env.setdefault("DEFAULT_USER_ID", "local-dev-user")
    cloud_env.setdefault("DEFAULT_SERVICE_KEY", "local-dev-service-key")

    ensure_state_dir()
    write_env_file(GENERATED_CLOUD_ENV_PATH, cloud_env)
    return GENERATED_CLOUD_ENV_PATH
