#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import select
import socket
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None


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
GENERATED_OS_ENV_PATH = STATE_DIR / "innate-os.env"
GENERATED_CLOUD_ENV_PATH = STATE_DIR / "cloud-agent.env"
HOSTED_MODE = "hosted"
LOCAL_IMAGE_MODE = "local-image"
LOCAL_SOURCE_MODE = "local-source"
LOCAL_MODES = {LOCAL_IMAGE_MODE, LOCAL_SOURCE_MODE}
OS_CONTAINER_SERVICE = "innate"
OS_CONTAINER_TMUX_CMD = "./scripts/launch_sim_in_tmux.zsh --detach"
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

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
NC = "\033[0m" if USE_COLOR else ""
BOLD = "\033[1m" if USE_COLOR else ""
DIM = "\033[2m" if USE_COLOR else ""
CYAN = "\033[0;36m" if USE_COLOR else ""
GREEN = "\033[0;32m" if USE_COLOR else ""
YELLOW = "\033[1;33m" if USE_COLOR else ""
RED = "\033[0;31m" if USE_COLOR else ""
SHOW_LIVE_DASHBOARD_DEFAULT = sys.stdout.isatty()
TMUX_SESSION_NAME = "innate"
CLI_ROOT = "./innate"
CLI_SIM = "./innate sim"
ASCII_BANNER = [
    r" ____  _____  _    ____ _  __",
    r"/ ___||_   _|/ \  / ___| |/ /",
    r"\___ \  | | / _ \| |   | ' / ",
    r" ___) | | |/ ___ \ |___| . \ ",
    r"|____/  |_/_/   \_\____|_|\_\ ",
]

TRUECOLOR = USE_COLOR and os.environ.get("COLORTERM", "").lower() in {
    "truecolor",
    "24bit",
}
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ASCII_MIRROR_MAP = str.maketrans(
    {
        "/": "\\",
        "\\": "/",
        "(": ")",
        ")": "(",
        "[": "]",
        "]": "[",
        "{": "}",
        "}": "{",
        "<": ">",
        ">": "<",
    }
)

THEME = {
    "title": (238, 238, 238),
    "dim": (128, 132, 142),
    "hi": (181, 64, 64),
    "panel_health": (85, 109, 89),
    "panel_fps": (108, 108, 75),
    "panel_queue": (92, 88, 141),
    "panel_frame": (128, 82, 82),
    "panel_fill": (30, 31, 36),
    "log_sim": (119, 202, 155),
    "log_agent": (120, 198, 255),
    "log_brain": (220, 112, 112),
    "health_start": (119, 202, 155),
    "health_mid": (203, 192, 108),
    "health_end": (220, 76, 76),
    "fps_start": (116, 230, 252),
    "fps_mid": (80, 197, 255),
    "fps_end": (38, 197, 255),
    "queue_start": (79, 67, 163),
    "queue_mid": (125, 65, 128),
    "queue_end": (220, 175, 222),
    "frame_start": (72, 151, 212),
    "frame_mid": (84, 116, 232),
    "frame_end": (255, 64, 182),
    "line_info": (120, 198, 255),
    "line_warn": (255, 208, 90),
    "line_error": (255, 107, 107),
    "line_success": (119, 202, 155),
    "line_cmd": (199, 146, 234),
    "line_net": (116, 230, 252),
}


class StackError(RuntimeError):
    pass


class DashboardHistory:
    def __init__(self, maxlen: int = 32):
        self.fps = deque(maxlen=maxlen)
        self.queue_load = deque(maxlen=maxlen)
        self.frame_age_ms = deque(maxlen=maxlen)
        self.health = deque(maxlen=maxlen)

    def add(self, snapshot: dict[str, object]) -> None:
        self.fps.append(float(snapshot["primary_fps"]))
        self.queue_load.append(float(snapshot["queue_load_score"]))
        self.frame_age_ms.append(float(snapshot["frame_age_ms"]))
        self.health.append(float(snapshot["health_score"]))

    def seed_from_snapshot(self, snapshot: dict[str, object]) -> None:
        self.add(snapshot)


class DashboardRuntime:
    def __init__(self, config: dict[str, object], *, log_cache_lines: int = 160):
        self.config = config
        self.log_cache_lines = log_cache_lines
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.snapshot = collect_status_snapshot(config)
        self.snapshot_rev = 1
        self.logs: dict[str, list[str]] = self._collect_logs(self.snapshot)
        self.log_rev = 1

    def _collect_logs(self, snapshot: dict[str, object]) -> dict[str, list[str]]:
        logs = {
            "simulator": capture_simulator_logs(
                bool(snapshot["sim_running"]), lines=self.log_cache_lines
            ),
            "brain": capture_os_brain_logs(self.config, lines=self.log_cache_lines),
        }
        if self.config["mode"] != HOSTED_MODE:
            logs["agent"] = capture_agent_logs(self.config, lines=self.log_cache_lines)
        return logs

    def read(self) -> tuple[dict[str, object], dict[str, list[str]], int, int]:
        with self.lock:
            snapshot = dict(self.snapshot)
            logs = {name: list(lines) for name, lines in self.logs.items()}
            return snapshot, logs, self.snapshot_rev, self.log_rev

    def set_snapshot(self, snapshot: dict[str, object]) -> None:
        with self.lock:
            self.snapshot = snapshot
            self.snapshot_rev += 1

    def refresh_snapshot(self) -> None:
        self.set_snapshot(collect_status_snapshot(self.config))

    def set_log(self, name: str, lines: list[str]) -> None:
        with self.lock:
            if self.logs.get(name) == lines:
                return
            self.logs[name] = lines
            self.log_rev += 1


def log(message: str) -> None:
    print(f"{CYAN}[innate]{NC} {message}")


def success(message: str) -> None:
    print(f"{GREEN}[ok]{NC} {message}")


def warn(message: str) -> None:
    print(f"{YELLOW}[warn]{NC} {message}")


def divider() -> None:
    print(f"{DIM}{'━' * 72}{NC}")


def divider_line(width: int) -> str:
    return colorize("━" * max(width, 1), fg=THEME["dim"], dim=True)


def clear_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def print_banner() -> None:
    clear_screen()
    divider()
    print_ascii_banner()
    print(f"{DIM}one env // one cli // os + sim + optional cloud agent{NC}")
    divider()


def format_state(ok: bool, label: str) -> str:
    color = GREEN if ok else YELLOW
    return f"{color}{label}{NC}"


def format_level(level: str, label: str) -> str:
    if level == "healthy":
        color = GREEN
    elif level == "warn":
        color = YELLOW
    else:
        color = RED
    return f"{color}{label}{NC}"


def format_sim_log_badge(mode: str) -> str:
    normalized = (mode or "quiet").strip().lower()
    if normalized == "debug":
        return f"{BOLD}{YELLOW}DEBUG ON{NC}"
    if normalized == "quiet":
        return f"{BOLD}{CYAN}QUIET FILTER ON{NC}"
    return f"{BOLD}{normalized.upper()}{NC}"


def describe_sim_log_mode(mode: str) -> str:
    normalized = (mode or "quiet").strip().lower()
    if normalized == "debug":
        return f"{BOLD}Simulator logs:{NC} {format_sim_log_badge(normalized)}  full simulator chatter visible  {DIM}(press d to return to quiet){NC}"
    if normalized == "quiet":
        return f"{BOLD}Simulator logs:{NC} {format_sim_log_badge(normalized)}  repetitive simulator chatter hidden  {DIM}(press d for full debug){NC}"
    return f"{BOLD}Simulator logs:{NC} {format_sim_log_badge(normalized)}"


def print_ascii_banner() -> None:
    for line in ASCII_BANNER:
        print(gradient_text(line, THEME["health_start"], THEME["fps_end"], bold=True))


def rgb_fg(rgb: tuple[int, int, int]) -> str:
    if not USE_COLOR:
        return ""
    if TRUECOLOR:
        return f"\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    avg = sum(rgb) / 3
    if avg >= 220:
        return "\033[97m"
    if avg >= 170:
        return "\033[37m"
    if avg >= 120:
        return "\033[36m"
    if avg >= 80:
        return "\033[34m"
    return "\033[90m"


def rgb_bg(rgb: tuple[int, int, int]) -> str:
    if not USE_COLOR:
        return ""
    if TRUECOLOR:
        return f"\033[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    avg = sum(rgb) / 3
    if avg >= 170:
        return "\033[47m"
    if avg >= 100:
        return "\033[44m"
    return "\033[40m"


def blend_rgb(
    start: tuple[int, int, int], end: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    ratio = max(0.0, min(1.0, ratio))
    return tuple(
        int(round(start[index] + (end[index] - start[index]) * ratio))
        for index in range(3)
    )


def gradient_rgb(
    start: tuple[int, int, int],
    mid: tuple[int, int, int],
    end: tuple[int, int, int],
    ratio: float,
) -> tuple[int, int, int]:
    if ratio <= 0.5:
        return blend_rgb(start, mid, ratio * 2.0)
    return blend_rgb(mid, end, (ratio - 0.5) * 2.0)


def colorize(
    text: str,
    *,
    fg: tuple[int, int, int] | None = None,
    bg: tuple[int, int, int] | None = None,
    bold: bool = False,
    dim: bool = False,
) -> str:
    if not USE_COLOR or not text:
        return text
    parts = []
    if bold:
        parts.append(BOLD)
    if dim:
        parts.append(DIM)
    if fg is not None:
        parts.append(rgb_fg(fg))
    if bg is not None:
        parts.append(rgb_bg(bg))
    return "".join(parts) + text + NC


def gradient_text(
    text: str,
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    *,
    bold: bool = False,
) -> str:
    if not USE_COLOR or not text:
        return text
    visible = len(text)
    if visible == 0:
        return text
    out: list[str] = []
    for index, char in enumerate(text):
        if char == " ":
            out.append(char)
            continue
        ratio = index / max(visible - 1, 1)
        out.append(colorize(char, fg=blend_rgb(start, end, ratio), bold=bold))
    return "".join(out)


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


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


def get_config() -> dict[str, object]:
    ensure_env_file()
    ensure_config_file(OS_CONFIG_PATH, OS_CONFIG_TEMPLATE_PATH)
    ensure_config_file(SIM_CONFIG_PATH, SIM_CONFIG_TEMPLATE_PATH)

    raw_env = parse_env_file(ENV_PATH)
    os_config = parse_toml_file(OS_CONFIG_PATH)
    sim_config = parse_toml_file(SIM_CONFIG_PATH)
    os_config_env = build_os_config_env(os_config)

    merged_env = dict(raw_env)
    for key, value in os_config_env.items():
        merged_env.setdefault(key, value)
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

    return {
        "raw_env": merged_env,
        "user_env": raw_env,
        "os_config_env": os_config_env,
        "mode": mode,
        "os_repo": os_repo,
        "sim_repo": sim_repo,
        "cloud_repo": cloud_repo,
        "cloud_port": "8765",
        "cloud_image": get_nested_str(sim_config, "cloud_agent", "image") or "",
        "sim_auto_setup": True,
        "sim_auto_build_frontend": True,
        "sim_auto_fetch_data": True,
        "sim_visualization": get_nested_bool(sim_config, "display", "visualization")
        if get_nested_bool(sim_config, "display", "visualization") is not None
        else False,
        "sim_log_mode": "quiet",
        "sim_args": "--log-everything",
        "os_always_build": True,
        "skip_local_cloud_auth": True,
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
    if config["skip_local_cloud_auth"]:
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
        if not config["sim_auto_setup"]:
            raise StackError(
                "Simulator virtualenv is missing required packages. "
                f"Re-run {sim_repo / 'setup.sh'}. The launcher expects simulator setup to stay enabled."
            )
        warn("Simulator virtualenv is incomplete. Re-running setup to repair it...")
        needs_setup = True

    if needs_setup:
        if not allow_setup:
            raise StackError(
                "Simulator Python environment is not ready.\n"
                f"Run `{CLI_SIM} setup` before `{CLI_SIM} up`."
            )
        if not config["sim_auto_setup"]:
            raise StackError(
                f"Simulator virtualenv missing at {sim_python}. Run {sim_repo / 'setup.sh'}"
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
        if not config["sim_auto_build_frontend"]:
            raise StackError(
                f"Simulator frontend build missing at {dist_index}. Run `yarn build` in {frontend_dir}."
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


def ensure_os_container(config: dict[str, object], os_env_file: Path) -> None:
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = docker_compose_env({"INNATE_OS_ENV_FILE": str(os_env_file)})

    if container_running("innate-dev"):
        log("Innate OS dev container already running.")
    else:
        log("Starting Innate OS dev container...")
        run_logged_with_heartbeat(
            ["docker", "compose", "-f", "docker-compose.dev.yml", "up", "-d"],
            cwd=os_repo,
            env=compose_env,
            log_path=COMPOSE_LOG_PATH,
            failure_message="Innate OS Docker startup failed.",
            progress_message=(
                "Docker is still preparing the Innate OS container. "
                "First boot or an image rebuild can take a minute."
            ),
        )

    build_cmd = "source /opt/ros/humble/setup.zsh && cd ~/innate-os/ros2_ws && "
    if config["os_always_build"]:
        build_cmd += "colcon build"
    else:
        build_cmd += (
            "if [ ! -f install/setup.zsh ] || "
            "find src -type f -newer install/setup.zsh -print -quit | grep -q .; then "
            "colcon build; "
            "else "
            "echo 'ROS workspace install is current; skipping rebuild.'; "
            "fi"
        )

    log("Building / validating the ROS workspace inside the container...")
    run_logged(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.dev.yml",
            "exec",
            "-T",
            OS_CONTAINER_SERVICE,
            "zsh",
            "-lc",
            build_cmd,
        ],
        cwd=os_repo,
        env=compose_env,
        log_path=OS_BUILD_LOG_PATH,
        failure_message="Innate OS ROS workspace build failed.",
    )

    log("Launching ROS simulation nodes inside the OS container...")
    launch_cmd = [
        "docker",
        "compose",
        "-f",
        "docker-compose.dev.yml",
        "exec",
        "-T",
        OS_CONTAINER_SERVICE,
        "zsh",
        "-lc",
        OS_CONTAINER_TMUX_CMD,
    ]
    for attempt in range(1, 3):
        run_logged(
            launch_cmd,
            cwd=os_repo,
            env=compose_env,
            log_path=OS_SESSION_LOG_PATH,
            failure_message="Innate OS tmux session launch failed.",
        )
        if wait_for_os_session_ready(config):
            return
        warn(f"OS session did not come up cleanly after launch attempt {attempt}/2.")

    raise StackError(
        "Innate OS container is up, but the ROS/tmux session never became ready.\n"
        f"Recent log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
    )


def down_os(config: dict[str, object]) -> None:
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = docker_compose_env({"INNATE_OS_ENV_FILE": str(GENERATED_OS_ENV_PATH)})
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

    time.sleep(2)
    if proc.poll() is not None:
        tail = tail_file(SIM_LOG_PATH)
        raise StackError(
            "Simulator backend exited immediately.\n"
            f"Recent log output:\n{tail}"
        )

    SIM_PID_PATH.write_text(f"{proc.pid}\n")


def tail_file(path: Path, limit: int = 40) -> str:
    if not path.exists():
        return "<no log output yet>"
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def docker_compose_cmd(*parts: str) -> list[str]:
    return ["docker", "compose", "-f", "docker-compose.dev.yml", *parts]


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


def os_tmux_session_running(config: dict[str, object]) -> bool:
    if not container_running("innate-dev"):
        return False
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = docker_compose_env({"INNATE_OS_ENV_FILE": str(GENERATED_OS_ENV_PATH)})
    return command_succeeds(
        docker_compose_cmd(
            "exec",
            "-T",
            OS_CONTAINER_SERVICE,
            "zsh",
            "-lc",
            f"tmux has-session -t {TMUX_SESSION_NAME}",
        ),
        cwd=os_repo,
        env=compose_env,
    )


def os_process_running(config: dict[str, object], pattern: str) -> bool:
    if not container_running("innate-dev"):
        return False
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = docker_compose_env({"INNATE_OS_ENV_FILE": str(GENERATED_OS_ENV_PATH)})
    return command_succeeds(
        docker_compose_cmd(
            "exec",
            "-T",
            OS_CONTAINER_SERVICE,
            "zsh",
            "-lc",
            f"pgrep -f {shlex.quote(pattern)} >/dev/null",
        ),
        cwd=os_repo,
        env=compose_env,
    )


def wait_for_os_session_ready(
    config: dict[str, object], *, timeout_seconds: float = 20.0
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if (
            os_tmux_session_running(config)
            and os_process_running(config, "rws_server")
            and os_process_running(config, "brain_client_node.py")
        ):
            return True
        time.sleep(1.0)
    return False


def capture_os_brain_logs(config: dict[str, object], lines: int = 18) -> list[str]:
    if not container_running("innate-dev"):
        return ["OS container offline."]
    if not os_tmux_session_running(config):
        recent_launch_output = tail_file(OS_SESSION_LOG_PATH, limit=max(lines - 3, 4))
        return [
            "OS tmux session is not running.",
            "The ROS stack did not finish launching inside the container.",
            f"Check: {CLI_SIM} logs os-session",
            *recent_launch_output.splitlines(),
        ][:lines]
    os_repo: Path = config["os_repo"]  # type: ignore[assignment]
    compose_env = docker_compose_env({"INNATE_OS_ENV_FILE": str(GENERATED_OS_ENV_PATH)})
    capture_flags = "-e -J -p" if USE_COLOR else "-J -p"
    output = capture_command_output(
        docker_compose_cmd(
            "exec",
            "-T",
            OS_CONTAINER_SERVICE,
            "zsh",
            "-lc",
            f"tmux capture-pane {capture_flags} -t {TMUX_SESSION_NAME}:nav-brain.1 -S -{lines}",
        ),
        cwd=os_repo,
        env=compose_env,
    )
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


def dashboard_snapshot_worker(runtime: DashboardRuntime, interval_seconds: float = 0.5) -> None:
    while not runtime.stop_event.is_set():
        runtime.refresh_snapshot()
        runtime.stop_event.wait(interval_seconds)


def dashboard_simulator_log_worker(
    runtime: DashboardRuntime, interval_seconds: float = 0.1
) -> None:
    while not runtime.stop_event.is_set():
        snapshot, _, _, _ = runtime.read()
        runtime.set_log(
            "simulator",
            capture_simulator_logs(
                bool(snapshot["sim_running"]), lines=runtime.log_cache_lines
            ),
        )
        runtime.stop_event.wait(interval_seconds)


def dashboard_brain_log_worker(
    runtime: DashboardRuntime, interval_seconds: float = 0.35
) -> None:
    while not runtime.stop_event.is_set():
        runtime.set_log(
            "brain",
            capture_os_brain_logs(runtime.config, lines=runtime.log_cache_lines),
        )
        runtime.stop_event.wait(interval_seconds)


def dashboard_agent_log_worker(
    runtime: DashboardRuntime, interval_seconds: float = 0.5
) -> None:
    while not runtime.stop_event.is_set():
        runtime.set_log(
            "agent",
            capture_agent_logs(runtime.config, lines=runtime.log_cache_lines),
        )
        runtime.stop_event.wait(interval_seconds)


@contextlib.contextmanager
def dashboard_runtime(config: dict[str, object]):
    runtime = DashboardRuntime(config)
    threads = [
        threading.Thread(
            target=dashboard_snapshot_worker, args=(runtime,), daemon=True
        ),
        threading.Thread(
            target=dashboard_simulator_log_worker, args=(runtime,), daemon=True
        ),
        threading.Thread(
            target=dashboard_brain_log_worker, args=(runtime,), daemon=True
        ),
    ]
    if config["mode"] != HOSTED_MODE:
        threads.append(
            threading.Thread(
                target=dashboard_agent_log_worker, args=(runtime,), daemon=True
            )
        )

    for thread in threads:
        thread.start()

    try:
        yield runtime
    finally:
        runtime.stop_event.set()
        for thread in threads:
            thread.join(timeout=1.0)


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


def dashboard_columns(config: dict[str, object], sim_running: bool) -> list[tuple[str, list[str]]]:
    return [
        ("SIMULATOR LOGS", capture_simulator_logs(sim_running)),
        ("AGENT LOGS", capture_agent_logs(config)),
        ("OS BRAIN LOGS", capture_os_brain_logs(config)),
    ]


def sparkline(
    values: list[float],
    *,
    width: int,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> str:
    ticks = "▁▂▃▄▅▆▇█"
    if width <= 0:
        return ""
    if not values:
        return " " * width

    if len(values) >= width:
        sampled = values[-width:]
    else:
        sampled = [values[0]] * (width - len(values)) + values

    hi = maximum if maximum is not None else max(max(sampled), minimum + 1.0)
    lo = minimum
    span = max(hi - lo, 1e-6)

    chars: list[str] = []
    for value in sampled:
        normalized = max(0.0, min(1.0, (value - lo) / span))
        index = min(int(round(normalized * (len(ticks) - 1))), len(ticks) - 1)
        chars.append(ticks[index])
    return "".join(chars)


def bar_chart_rows(
    values: list[float],
    *,
    width: int,
    height: int,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> list[str]:
    blocks = " ▁▂▃▄▅▆▇█"
    if width <= 0 or height <= 0:
        return []
    if not values:
        return [" " * width for _ in range(height)]

    if len(values) >= width:
        sampled = values[-width:]
    else:
        sampled = [values[0]] * (width - len(values)) + values

    hi = maximum if maximum is not None else max(max(sampled), minimum + 1.0)
    lo = minimum
    span = max(hi - lo, 1e-6)

    bar_levels = []
    for value in sampled:
        normalized = max(0.0, min(1.0, (value - lo) / span))
        bar_levels.append(int(round(normalized * height * 8)))

    rows: list[str] = []
    for row_index in range(height):
        threshold = (height - row_index - 1) * 8
        chars: list[str] = []
        for level in bar_levels:
            visible = max(0, min(8, level - threshold))
            chars.append(blocks[visible])
        rows.append("".join(chars))
    return rows


def colorize_chart_rows(
    rows: list[str],
    *,
    start: tuple[int, int, int],
    mid: tuple[int, int, int],
    end: tuple[int, int, int],
) -> list[str]:
    if not USE_COLOR:
        return rows
    colored: list[str] = []
    total_rows = max(len(rows), 1)
    for row_index, row in enumerate(rows):
        ratio = 1.0 - (row_index / max(total_rows - 1, 1))
        row_rgb = gradient_rgb(start, mid, end, ratio)
        out: list[str] = []
        for char in row:
            if char == " ":
                out.append(colorize(char, bg=THEME["panel_fill"]))
                continue
            out.append(
                colorize(
                    char,
                    fg=row_rgb,
                    bg=THEME["panel_fill"],
                    bold=True,
                )
            )
        colored.append("".join(out))
    return colored


def render_panel_box(
    title: str,
    value: str,
    subtitle: str,
    chart_lines: list[str],
    *,
    width: int,
    border_rgb: tuple[int, int, int],
    fill_rgb: tuple[int, int, int],
) -> list[str]:
    inner = max(width - 2, 12)
    top_text = title.center(inner, "─")
    top = (
        colorize("┌", fg=border_rgb, bold=True)
        + gradient_text(top_text, border_rgb, blend_rgb(border_rgb, (255, 255, 255), 0.15), bold=True)
        + colorize("┐", fg=border_rgb, bold=True)
    )
    bottom = (
        colorize("└", fg=border_rgb, bold=True)
        + colorize("─" * inner, fg=border_rgb)
        + colorize("┘", fg=border_rgb, bold=True)
    )
    value_line = (
        colorize("│", fg=border_rgb, bold=True)
        + colorize(truncate_line(value, inner), fg=THEME["title"], bg=fill_rgb, bold=True)
        + colorize("│", fg=border_rgb, bold=True)
    )
    subtitle_line = (
        colorize("│", fg=border_rgb, bold=True)
        + colorize(truncate_line(subtitle, inner), fg=THEME["dim"], bg=fill_rgb)
        + colorize("│", fg=border_rgb, bold=True)
    )
    rendered_chart_lines = [
        colorize("│", fg=border_rgb, bold=True)
        + line
        + colorize("│", fg=border_rgb, bold=True)
        for line in chart_lines
    ]
    return [top, value_line, subtitle_line, *rendered_chart_lines, bottom]


def print_metric_panels(snapshot: dict[str, object], history: DashboardHistory) -> int:
    width = shutil.get_terminal_size((150, 40)).columns
    gap = 2
    columns = 4 if width >= 150 else 2
    panel_width = max((width - gap * (columns - 1)) // columns, 24)

    panels = [
        (
            " HEALTH ",
            str(snapshot["stack_label"]),
            str(snapshot["system_summary"]),
            colorize_chart_rows(
                bar_chart_rows(
                    list(history.health),
                    width=max(panel_width - 2, 12),
                    height=4,
                    minimum=0.0,
                    maximum=100.0,
                ),
                start=THEME["health_start"],
                mid=THEME["health_mid"],
                end=THEME["health_end"],
            ),
            THEME["panel_health"],
        ),
        (
            " FPS ",
            f"{float(snapshot['primary_fps']):.1f} fps",
            f"video {snapshot['video_label']}",
            colorize_chart_rows(
                bar_chart_rows(
                    list(history.fps),
                    width=max(panel_width - 2, 12),
                    height=4,
                    minimum=0.0,
                    maximum=max(12.0, max(history.fps, default=12.0)),
                ),
                start=THEME["fps_start"],
                mid=THEME["fps_mid"],
                end=THEME["fps_end"],
            ),
            THEME["panel_fps"],
        ),
        (
            " QUEUES ",
            str(snapshot["queue_pressure"]),
            f"peak {snapshot['queue_peak']} | {snapshot['transport_label']}",
            colorize_chart_rows(
                bar_chart_rows(
                    list(history.queue_load),
                    width=max(panel_width - 2, 12),
                    height=4,
                    minimum=0.0,
                    maximum=100.0,
                ),
                start=THEME["queue_start"],
                mid=THEME["queue_mid"],
                end=THEME["queue_end"],
            ),
            THEME["panel_queue"],
        ),
        (
            " FRAME AGE ",
            f"{float(snapshot['frame_age_ms']):.0f} ms",
            f"latest frame | {snapshot['video_label']}",
            colorize_chart_rows(
                bar_chart_rows(
                    list(history.frame_age_ms),
                    width=max(panel_width - 2, 12),
                    height=4,
                    minimum=0.0,
                    maximum=max(400.0, max(history.frame_age_ms, default=400.0)),
                ),
                start=THEME["frame_start"],
                mid=THEME["frame_mid"],
                end=THEME["frame_end"],
            ),
            THEME["panel_frame"],
        ),
    ]

    rows = [panels[index : index + columns] for index in range(0, len(panels), columns)]
    line_count = 0
    for row in rows:
        rendered = [
            render_panel_box(
                title,
                value,
                subtitle,
                chart,
                width=panel_width,
                border_rgb=border_rgb,
                fill_rgb=THEME["panel_fill"],
            )
            for title, value, subtitle, chart, border_rgb in row
        ]
        for line_index in range(len(rendered[0])):
            print((" " * gap).join(panel[line_index] for panel in rendered))
            line_count += 1
        print()
        line_count += 1
    return line_count

def truncate_line(text: str, width: int) -> str:
    if display_text_width(text) <= width:
        return text + (" " * max(width - display_text_width(text), 0))

    target = max(width - 1, 0)
    visible = 0
    out: list[str] = []
    for char in text:
        char_width = char_display_width(char)
        if visible + char_width > target:
            break
        out.append(char)
        visible += char_width
    if width > 0:
        out.append("…")
    rendered = "".join(out)
    padding = width - display_text_width(rendered)
    if padding > 0:
        rendered += " " * padding
    return rendered


def char_display_width(char: str) -> int:
    if not char:
        return 0
    if char in {"\n", "\r"}:
        return 0
    if char == "\t":
        return 4
    category = unicodedata.category(char)
    if category.startswith("C"):
        return 0
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 2
    return 1


def display_text_width(text: str) -> int:
    return sum(char_display_width(char) for char in text)


def visible_text_width(text: str) -> int:
    return display_text_width(ANSI_ESCAPE_RE.sub("", text))


def truncate_ansi_line(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if visible_text_width(text) <= width:
        return text

    target = max(width - 1, 0)
    visible = 0
    cursor = 0
    out: list[str] = []
    while cursor < len(text) and visible < target:
        match = ANSI_ESCAPE_RE.match(text, cursor)
        if match:
            out.append(match.group(0))
            cursor = match.end()
            continue
        char = text[cursor]
        char_width = char_display_width(char)
        if visible + char_width > target:
            break
        out.append(char)
        visible += char_width
        cursor += 1

    if width > 0:
        out.append("…")
    if USE_COLOR:
        out.append(NC)
    return "".join(out)


def fit_ansi_line(text: str, width: int) -> str:
    if width <= 0:
        return ""
    sanitized = text.replace("\r", "").expandtabs(4).rstrip()
    fitted = truncate_ansi_line(sanitized, width)
    padding = width - visible_text_width(fitted)
    if padding > 0:
        fitted += " " * padding
    return fitted


def print_dashboard_line(text: str, width: int) -> None:
    print(truncate_ansi_line(text, width))


def bounce_position(distance: int, tick: int) -> tuple[int, bool]:
    if distance <= 0:
        return (0, True)
    cycle = max(distance * 2, 1)
    step = tick % cycle
    if step <= distance:
        return (step, True)
    return (cycle - step, False)


def mirror_ascii_line(text: str) -> str:
    return text.translate(ASCII_MIRROR_MAP)[::-1]


def render_robot_marquee(width: int) -> list[str]:
    sprite_frames = [
        [
            "        ==∞                         ",
            "    ___||__      o==o=C             ",
            "   |       |   //                   ",
            "   |       |_//                     ",
            "L__|______()_|                      ",
        ],
        [
            "        ==∞                         ",
            "    ___||__      o==o=C             ",
            "   |       |    //                  ",
            "   |       |_//                     ",
            "L__|______()_|                      ",
        ],
    ]

    tick = int(time.monotonic() * 6.0)
    frame = sprite_frames[tick % len(sprite_frames)]
    sprite_width = max(len(line) for line in frame)
    _, moving_right = bounce_position(max(width - sprite_width, 0), tick)
    if not moving_right:
        frame = [mirror_ascii_line(line) for line in frame]
    if width <= sprite_width:
        clipped = []
        for line in frame:
            segment = line[:width].ljust(width)
            clipped.append(colorize(segment, fg=THEME["fps_end"], bold=True))
        return clipped

    travel = width - sprite_width
    offset, _ = bounce_position(travel, tick)
    rendered: list[str] = []
    for line in frame:
        padded = line.ljust(sprite_width)
        rendered.append(
            (" " * offset)
            + colorize(padded, fg=THEME["fps_end"], bold=True)
            + (" " * (travel - offset))
        )

    ground = "." * width
    rendered.append(colorize(ground, fg=THEME["dim"], dim=True))
    return rendered


def render_log_box(
    title: str,
    lines: list[str],
    *,
    width: int,
    height: int,
    border_rgb: tuple[int, int, int],
) -> list[str]:
    inner = max(width - 2, 12)
    visible_rows = max(height - 2, 1)
    top = (
        colorize("┌", fg=border_rgb, bold=True)
        + gradient_text(title.center(inner, "─"), border_rgb, THEME["title"], bold=True)
        + colorize("┐", fg=border_rgb, bold=True)
    )
    bottom = (
        colorize("└", fg=border_rgb, bold=True)
        + colorize("─" * inner, fg=border_rgb)
        + colorize("┘", fg=border_rgb, bold=True)
    )
    visible_lines = [line.rstrip("\n") for line in lines[-visible_rows:]]
    padded_lines = visible_lines + [""] * max(visible_rows - len(visible_lines), 0)
    body = []
    for line in padded_lines:
        if not line:
            content = colorize(" " * inner, bg=THEME["panel_fill"])
        else:
            content = fit_ansi_line(line, inner)
        body.append(
            colorize("│", fg=border_rgb, bold=True)
            + content
            + (NC if USE_COLOR and line else "")
            + colorize("│", fg=border_rgb, bold=True)
        )
    return [top, *body, bottom]


def print_log_columns(
    columns: list[tuple[str, list[str], tuple[int, int, int]]], *, available_height: int
) -> None:
    width = shutil.get_terminal_size((150, 40)).columns
    if available_height < 3:
        print(
            colorize(
                "Terminal too short for live log panes. Enlarge the window to show them.",
                fg=THEME["dim"],
                dim=True,
            )
        )
        return

    if len(columns) == 1 or width < 120:
        gap_lines = 1 if available_height >= len(columns) * 5 else 0
        total_gap_lines = gap_lines * max(len(columns) - 1, 0)
        remaining_for_boxes = max(available_height - total_gap_lines, len(columns))
        box_height = remaining_for_boxes // len(columns)
        if box_height < 3:
            print(
                colorize(
                    "Terminal too short for stacked log panes. Enlarge the window to show them.",
                    fg=THEME["dim"],
                    dim=True,
                )
            )
            return

        for index, (title, lines, border_rgb) in enumerate(columns):
            if index > 0 and gap_lines:
                print()
            for row in render_log_box(
                title, lines, width=width, height=box_height, border_rgb=border_rgb
            ):
                print(row)
        return

    gap = 2
    box_height = max(available_height, 3)
    inner_width = max((width - gap * (len(columns) - 1)) // len(columns), 24)
    rendered_columns = [
        render_log_box(
            title,
            lines,
            width=inner_width,
            height=box_height,
            border_rgb=border_rgb,
        )
        for title, lines, border_rgb in columns
    ]

    for row_index in range(box_height):
        print("  ".join(column[row_index] for column in rendered_columns))


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

    if not config["sim_auto_fetch_data"]:
        details = "\n".join(f"- {label}: {path}" for label, path in missing_before)
        raise StackError(
            "Required simulator scene data is missing.\n"
            f"{details}\nAutomatic simulator data bootstrap is expected to be enabled.\n"
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


def wait_for_simulator_http(port: str, timeout_seconds: float = 90.0) -> None:
    deadline = time.time() + timeout_seconds
    url = f"http://localhost:{port}/video_feeds_ready"
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
        try:
            with urlopen(url, timeout=2) as response:
                if response.status != 200:
                    time.sleep(2)
                    continue
                payload = json.loads(response.read().decode("utf-8"))
                if payload.get("ready"):
                    return
        except URLError:
            time.sleep(2)
        except (TimeoutError, json.JSONDecodeError):
            time.sleep(2)
    tail = tail_file(SIM_LOG_PATH, limit=80)
    raise StackError(
        "Timed out waiting for the simulator HTTP endpoint.\n"
        f"Recent log output:\n{tail}"
    )


def simulator_ready(port: str) -> bool:
    try:
        with urlopen(f"http://localhost:{port}/video_feeds_ready", timeout=2) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return bool(payload.get("ready"))
    except (URLError, TimeoutError, json.JSONDecodeError):
        return False


def fetch_simulator_metrics(port: str) -> dict[str, object]:
    try:
        with urlopen(f"http://localhost:{port}/stack_metrics", timeout=2) as response:
            if response.status != 200:
                return {}
            return json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError):
        return {}


def set_simulator_log_mode(port: str, mode: str) -> bool:
    payload = json.dumps({"mode": mode}).encode("utf-8")
    request = Request(
        f"http://localhost:{port}/sim_log_config",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read().decode("utf-8"))
            return body.get("mode") == mode
    except (URLError, TimeoutError, json.JSONDecodeError):
        return False

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
    simulator_port = str(config["raw_env"].get("SIMULATOR_PORT", "8000"))  # type: ignore[index]
    rosbridge_port = 9090
    os_running = container_running("innate-dev")
    os_session_running = os_tmux_session_running(config) if os_running else False
    agent_running = container_running("stack-cloud-agent")
    sim_running = simulator_ready(simulator_port)
    rosbridge_process_live = os_process_running(config, "rws_server") if os_running else False
    brain_process_live = os_process_running(config, "brain_client_node.py") if os_running else False
    rosbridge_live = (
        os_session_running
        and rosbridge_process_live
        and tcp_port_open(rosbridge_port)
    )
    metrics = fetch_simulator_metrics(simulator_port) if sim_running else {}
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
        brain_label = "connected"
    else:
        brain_level = "warn"
        brain_label = "booting"
    agent_level = (
        "healthy"
        if config["mode"] == HOSTED_MODE or agent_running
        else "warn"
    )
    agent_label = "hosted" if config["mode"] == HOSTED_MODE else ("online" if agent_running else "offline")

    if all(level == "healthy" for level in (video_level, transport_level, brain_level, agent_level)):
        stack_mood = ("healthy", "LIVE")
    elif any(level == "error" for level in (video_level, transport_level, brain_level)):
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
        f"brain {'ok' if brain_level == 'healthy' else brain_label}"
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


def render_status(
    config: dict[str, object],
    *,
    verbose: bool = False,
    history: DashboardHistory | None = None,
    clear: bool = True,
    snapshot: dict[str, object] | None = None,
    cached_logs: dict[str, list[str]] | None = None,
) -> None:
    if clear:
        clear_screen()
    if snapshot is None:
        snapshot = collect_status_snapshot(config)
    if history is None:
        history = DashboardHistory()
        history.seed_from_snapshot(snapshot)

    term_size = shutil.get_terminal_size((150, 40))
    term_width = term_size.columns
    term_height = term_size.lines
    used_lines = 0

    show_banner = term_height >= 48 and term_width >= 170
    if show_banner:
        print_ascii_banner()
        used_lines += len(ASCII_BANNER)
    else:
        print_dashboard_line(f"{BOLD}innate dashboard{NC}", term_width)
        used_lines += 1
    print_dashboard_line(f"{DIM}innate sim dashboard{NC}", term_width)
    used_lines += 1
    print(divider_line(term_width))
    used_lines += 1
    used_lines += print_metric_panels(snapshot, history)
    print_dashboard_line(
        "  ".join(
            [
                f"{BOLD}Mood:{NC} {format_level(str(snapshot['stack_level']), str(snapshot['stack_label']))}",
                f"{BOLD}Video:{NC} {format_level(str(snapshot['video_level']), str(snapshot['video_label']))}",
                f"{BOLD}Transport:{NC} {format_level(str(snapshot['transport_level']), str(snapshot['transport_label']))}",
                f"{BOLD}Brain:{NC} {format_level(str(snapshot['brain_level']), str(snapshot['brain_label']))}",
                f"{BOLD}Agent:{NC} {format_level(str(snapshot['agent_level']), str(snapshot['agent_label']))}",
            ]
        ),
        term_width,
    )
    used_lines += 1
    print_dashboard_line(
        "  ".join(
            [
                f"{BOLD}Cloud mode:{NC} {config['mode']}",
                f"{BOLD}Viewer:{NC} {'on' if config.get('sim_visualization') else 'off'}",
                f"{BOLD}Sim logs:{NC} {format_sim_log_badge(str(snapshot['sim_log_mode']))}",
                f"{BOLD}FPS:{NC} {float(snapshot['primary_fps']):.1f}",
                f"{BOLD}Frame age:{NC} {float(snapshot['frame_age_ms']):.0f} ms",
                f"{BOLD}Queue load:{NC} {format_level(str(snapshot['transport_level']), str(snapshot['queue_pressure']))} (peak {snapshot['queue_peak']})",
            ]
        ),
        term_width,
    )
    used_lines += 1
    print_dashboard_line(describe_sim_log_mode(str(snapshot["sim_log_mode"])), term_width)
    used_lines += 1
    print_dashboard_line(
        "  ".join(
            [
                f"{BOLD}Queues:{NC} {snapshot['queue_summary']}",
                f"{BOLD}Chat:{NC} {snapshot['chat_load']}",
                f"{BOLD}System:{NC} {snapshot['system_summary']}",
            ]
        ),
        term_width,
    )
    used_lines += 1
    print()
    used_lines += 1
    print_dashboard_line(
        f"{BOLD}Simulator UI:{NC} http://localhost:{snapshot['simulator_port']}",
        term_width,
    )
    used_lines += 1
    print_dashboard_line(f"{BOLD}ROSBridge:{NC} ws://localhost:9090", term_width)
    used_lines += 1
    if config["mode"] in LOCAL_MODES:
        print_dashboard_line(
            f"{BOLD}Local agent:{NC} ws://localhost:{config['cloud_port']}",
            term_width,
        )
        used_lines += 1
    print_dashboard_line(f"{BOLD}Logs:{NC} {CLI_SIM} logs startup", term_width)
    used_lines += 1
    print_dashboard_line(
        f"{DIM}Keys: q detach  d toggle sim logs  v verbose  Ctrl+C stop runtime{NC}",
        term_width,
    )
    used_lines += 1
    if term_height - used_lines >= 12:
        marquee_lines = render_robot_marquee(term_width)
        for marquee_line in marquee_lines:
            print(marquee_line)
        used_lines += len(marquee_lines)

    if verbose:
        used_lines += 5
    available_height = max(term_height - used_lines, 0)
    visible_log_rows = max(available_height, 3)
    simulator_lines = (
        cached_logs["simulator"]
        if cached_logs is not None and "simulator" in cached_logs
        else capture_simulator_logs(
            bool(snapshot["sim_running"]),
            lines=visible_log_rows,
        )
    )
    brain_lines = (
        cached_logs["brain"]
        if cached_logs is not None and "brain" in cached_logs
        else capture_os_brain_logs(config, lines=visible_log_rows)
    )
    log_columns = [
        (
            f"SIMULATOR LOGS [{str(snapshot['sim_log_mode']).upper()}]",
            simulator_lines,
            THEME["log_sim"],
        ),
        (
            "OS BRAIN LOGS",
            brain_lines,
            THEME["log_brain"],
        ),
    ]
    if config["mode"] != HOSTED_MODE:
        agent_lines = (
            cached_logs["agent"]
            if cached_logs is not None and "agent" in cached_logs
            else capture_agent_logs(config, lines=visible_log_rows)
        )
        log_columns.insert(
            1,
            (
                "AGENT LOGS",
                agent_lines,
                THEME["log_agent"],
            ),
        )
    print_log_columns(log_columns, available_height=available_height)
    if verbose:
        print()
        print(divider_line(term_width))
        print_dashboard_line(f"{BOLD}Innate OS repo:{NC} {config['os_repo']}", term_width)
        print_dashboard_line(f"{BOLD}Innate sim repo:{NC} {config['sim_repo']}", term_width)
        if config["cloud_repo"] is not None:
            print_dashboard_line(
                f"{BOLD}Local cloud-agent repo:{NC} {config['cloud_repo']}",
                term_width,
            )
        print_dashboard_line(f"{BOLD}State dir:{NC} {STATE_DIR}", term_width)


def render_status_text(
    config: dict[str, object],
    *,
    verbose: bool = False,
    history: DashboardHistory | None = None,
    snapshot: dict[str, object] | None = None,
    cached_logs: dict[str, list[str]] | None = None,
) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render_status(
            config,
            verbose=verbose,
            history=history,
            clear=False,
            snapshot=snapshot,
            cached_logs=cached_logs,
        )
    return buffer.getvalue()


def print_status(config: dict[str, object], *, verbose: bool = False) -> None:
    render_status(config, verbose=verbose)


@contextlib.contextmanager
def dashboard_input_mode():
    if not sys.stdin.isatty() or termios is None or tty is None:
        yield False
        return

    fd = sys.stdin.fileno()
    original_state = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_state)


def read_dashboard_key(timeout_seconds: float) -> str | None:
    if not sys.stdin.isatty():
        time.sleep(timeout_seconds)
        return None
    ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
    if not ready:
        return None
    try:
        data = os.read(sys.stdin.fileno(), 1)
    except OSError:
        return None
    if not data:
        return None
    return data.decode(errors="ignore")


@contextlib.contextmanager
def live_dashboard_terminal():
    if not sys.stdout.isatty():
        yield
        return
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()
    try:
        yield
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


def watch_dashboard(
    config: dict[str, object], *, verbose: bool = False, refresh_seconds: float = 0.5
) -> str:
    redraw = True
    history = DashboardHistory()
    simulator_port = str(config["raw_env"].get("SIMULATOR_PORT", "8000"))  # type: ignore[index]
    sim_log_mode = str(config.get("sim_log_mode", "quiet"))
    try:
        with (
            dashboard_runtime(config) as runtime,
            live_dashboard_terminal(),
            dashboard_input_mode() as input_mode_enabled,
        ):
            snapshot, cached_logs, snapshot_rev, log_rev = runtime.read()
            history.seed_from_snapshot(snapshot)
            last_snapshot_rev = snapshot_rev
            last_log_rev = log_rev
            next_refresh = 0.0
            while True:
                now = time.monotonic()
                snapshot, cached_logs, snapshot_rev, log_rev = runtime.read()
                if snapshot_rev != last_snapshot_rev:
                    history.add(snapshot)
                    sim_log_mode = str(snapshot.get("sim_log_mode", sim_log_mode))
                    last_snapshot_rev = snapshot_rev
                    redraw = True
                if log_rev != last_log_rev:
                    last_log_rev = log_rev
                    redraw = True
                if redraw or now >= next_refresh:
                    sys.stdout.write("\033[H\033[J")
                    sys.stdout.write(
                        render_status_text(
                            config,
                            verbose=verbose,
                            history=history,
                            snapshot=snapshot,
                            cached_logs=cached_logs,
                        )
                    )
                    sys.stdout.flush()
                    next_refresh = now + refresh_seconds
                    redraw = False

                key = read_dashboard_key(
                    0.2 if input_mode_enabled else max(next_refresh - time.monotonic(), 0.1)
                )
                if key is None:
                    continue

                normalized = key.lower()
                if normalized == "v":
                    verbose = not verbose
                    redraw = True
                elif normalized == "d":
                    target_mode = "quiet" if sim_log_mode == "debug" else "debug"
                    if set_simulator_log_mode(simulator_port, target_mode):
                        sim_log_mode = target_mode
                        runtime.refresh_snapshot()
                    redraw = True
                    next_refresh = 0.0
                elif normalized == "q":
                    print()
                    success("Left the live dashboard. The Innate runtime is still running.")
                    return "detach"
    except KeyboardInterrupt:
        return "shutdown"

    return "detach"


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

        simulator_port = config["raw_env"].get("SIMULATOR_PORT", "8000")  # type: ignore[index]
        log("Waiting for the simulator HTTP endpoint...")
        wait_for_simulator_http(str(simulator_port))
        success("Innate sim runtime is up.")
        if watch and sys.stdout.isatty():
            dashboard_result = watch_dashboard(config)
            if dashboard_result == "shutdown":
                print()
                log("Ctrl+C received. Stopping the Innate runtime...")
                cmd_down(config)
        else:
            print_status(config)
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
            print_status(config, verbose=args.mode == "verbose")
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
