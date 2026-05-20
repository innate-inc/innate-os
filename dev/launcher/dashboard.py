from __future__ import annotations

import contextlib
import io
import os
import re
import select
import shutil
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
NC = "\033[0m" if USE_COLOR else ""
BOLD = "\033[1m" if USE_COLOR else ""
DIM = "\033[2m" if USE_COLOR else ""
CYAN = "\033[0;36m" if USE_COLOR else ""
GREEN = "\033[0;32m" if USE_COLOR else ""
YELLOW = "\033[1;33m" if USE_COLOR else ""
RED = "\033[0;31m" if USE_COLOR else ""

ASCII_BANNER = [
    r" ___ _   _ _   _    _  _____ _____",
    r"|_ _| \ | | \ | |  / \|_   _| ____|",
    r" | ||  \| |  \| | / _ \ | | |  _|",
    r" | || |\  | |\  |/ ___ \| | | |___",
    r"|___|_| \_|_| \_/_/   \_\_| |_____|",
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


@dataclass(frozen=True)
class DashboardCallbacks:
    collect_status_snapshot: Callable[[dict[str, object]], dict[str, object]]
    capture_simulator_logs: Callable[..., list[str]]
    capture_os_brain_logs: Callable[..., list[str]]
    capture_agent_logs: Callable[..., list[str]]
    set_simulator_log_mode: Callable[[str, str], bool]
    success: Callable[[str], None]


@dataclass(frozen=True)
class DashboardOptions:
    hosted_mode: str
    local_modes: set[str]
    cli_sim: str
    state_dir: Path


def config_simulator_port(config: dict[str, object]) -> str:
    raw_env = config.get("raw_env")
    if isinstance(raw_env, dict):
        return str(raw_env.get("SIMULATOR_PORT", "8000"))
    return "8000"


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
    def __init__(
        self,
        config: dict[str, object],
        callbacks: DashboardCallbacks,
        options: DashboardOptions,
        *,
        log_cache_lines: int = 160,
    ):
        self.config = config
        self.callbacks = callbacks
        self.options = options
        self.log_cache_lines = log_cache_lines
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.snapshot = callbacks.collect_status_snapshot(config)
        self.snapshot_rev = 1
        self.logs: dict[str, list[str]] = self._collect_logs(self.snapshot)
        self.log_rev = 1

    def _collect_logs(self, snapshot: dict[str, object]) -> dict[str, list[str]]:
        logs = {
            "simulator": self.callbacks.capture_simulator_logs(
                bool(snapshot["sim_running"]), lines=self.log_cache_lines
            ),
            "brain": self.callbacks.capture_os_brain_logs(
                self.config, lines=self.log_cache_lines
            ),
        }
        if self.config["mode"] != self.options.hosted_mode:
            logs["agent"] = self.callbacks.capture_agent_logs(
                self.config, lines=self.log_cache_lines
            )
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
        self.set_snapshot(self.callbacks.collect_status_snapshot(self.config))

    def set_log(self, name: str, lines: list[str]) -> None:
        with self.lock:
            if self.logs.get(name) == lines:
                return
            self.logs[name] = lines
            self.log_rev += 1


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
            runtime.callbacks.capture_simulator_logs(
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
            runtime.callbacks.capture_os_brain_logs(
                runtime.config, lines=runtime.log_cache_lines
            ),
        )
        runtime.stop_event.wait(interval_seconds)


def dashboard_agent_log_worker(
    runtime: DashboardRuntime, interval_seconds: float = 0.5
) -> None:
    while not runtime.stop_event.is_set():
        runtime.set_log(
            "agent",
            runtime.callbacks.capture_agent_logs(
                runtime.config, lines=runtime.log_cache_lines
            ),
        )
        runtime.stop_event.wait(interval_seconds)


@contextlib.contextmanager
def dashboard_runtime(
    config: dict[str, object],
    callbacks: DashboardCallbacks,
    options: DashboardOptions,
):
    runtime = DashboardRuntime(config, callbacks, options)
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
    if config["mode"] != options.hosted_mode:
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


def paint_terminal_frame(text: str, *, top_padding_rows: int = 0) -> None:
    term_size = shutil.get_terminal_size((150, 40))
    width = term_size.columns
    height = term_size.lines
    if width <= 0 or height <= 0:
        return

    top_padding_rows = max(0, min(top_padding_rows, max(height - 1, 0)))
    render_height = max(height - top_padding_rows, 0)
    lines = text.splitlines()
    visible_rows = min(len(lines), render_height)
    output = ["\033[H"]

    for row in range(1, top_padding_rows + 1):
        output.append(f"\033[{row};1H\033[K")

    for row, line in enumerate(lines[:render_height], start=top_padding_rows + 1):
        output.append(f"\033[{row};1H")
        output.append(truncate_ansi_line(line, width))
        if USE_COLOR:
            output.append(NC)
        output.append("\033[K")

    if visible_rows < render_height:
        output.append(f"\033[{top_padding_rows + visible_rows + 1};1H\033[J")

    sys.stdout.write("".join(output))


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


def runtime_is_down(
    config: dict[str, object],
    options: DashboardOptions,
    snapshot: dict[str, object],
) -> bool:
    local_agent_running = (
        config["mode"] != options.hosted_mode and bool(snapshot["agent_running"])
    )
    return (
        not bool(snapshot["os_running"])
        and not bool(snapshot["sim_running"])
        and not local_agent_running
    )


def print_down_status(options: DashboardOptions) -> None:
    print("Innate sim runtime is down.")
    print(f"Start it with: {options.cli_sim} up")
    print(f"Historical logs: {options.cli_sim} logs startup")


def render_status(
    config: dict[str, object],
    callbacks: DashboardCallbacks,
    options: DashboardOptions,
    *,
    verbose: bool = False,
    history: DashboardHistory | None = None,
    clear: bool = True,
    snapshot: dict[str, object] | None = None,
    cached_logs: dict[str, list[str]] | None = None,
    reserved_top_rows: int = 0,
) -> None:
    if clear:
        clear_screen()
    if snapshot is None:
        snapshot = callbacks.collect_status_snapshot(config)
    if history is None:
        history = DashboardHistory()
        history.seed_from_snapshot(snapshot)

    term_size = shutil.get_terminal_size((150, 40))
    term_width = term_size.columns
    term_height = max(term_size.lines - reserved_top_rows, 1)
    used_lines = 0

    show_banner = term_height >= 48 and term_width >= 170
    if show_banner:
        print_ascii_banner()
        used_lines += len(ASCII_BANNER)
    else:
        print_dashboard_line(f"{BOLD}Innate{NC}", term_width)
        used_lines += 1
    print_dashboard_line(f"{DIM}Innate sim dashboard{NC}", term_width)
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
                f"{BOLD}Backend:{NC} {format_level(str(snapshot['backend_level']), str(snapshot['backend_label']))}",
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
    if config["mode"] in options.local_modes:
        print_dashboard_line(
            f"{BOLD}Local agent:{NC} ws://localhost:{config['cloud_port']}",
            term_width,
        )
        used_lines += 1
    print_dashboard_line(f"{BOLD}Logs:{NC} {options.cli_sim} logs startup", term_width)
    used_lines += 1
    print_dashboard_line(
        f"{DIM}Keys: q detach  d toggle sim logs  v verbose  Ctrl+C stop runtime{NC}",
        term_width,
    )
    used_lines += 1

    if runtime_is_down(config, options, snapshot):
        print()
        used_lines += 1
        print_dashboard_line(
            "  ".join(
                [
                    f"{BOLD}Runtime:{NC} {format_level('error', 'down')}",
                    f"{BOLD}Start:{NC} {options.cli_sim} up",
                    f"{BOLD}Historical logs:{NC} {options.cli_sim} logs startup",
                ]
            ),
            term_width,
        )
        used_lines += 1
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
            print_dashboard_line(f"{BOLD}State dir:{NC} {options.state_dir}", term_width)
        return

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
        else callbacks.capture_simulator_logs(
            bool(snapshot["sim_running"]),
            lines=visible_log_rows,
        )
    )
    brain_lines = (
        cached_logs["brain"]
        if cached_logs is not None and "brain" in cached_logs
        else callbacks.capture_os_brain_logs(config, lines=visible_log_rows)
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
    if config["mode"] != options.hosted_mode:
        agent_lines = (
            cached_logs["agent"]
            if cached_logs is not None and "agent" in cached_logs
            else callbacks.capture_agent_logs(config, lines=visible_log_rows)
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
        print_dashboard_line(f"{BOLD}State dir:{NC} {options.state_dir}", term_width)


def render_status_text(
    config: dict[str, object],
    callbacks: DashboardCallbacks,
    options: DashboardOptions,
    *,
    verbose: bool = False,
    history: DashboardHistory | None = None,
    snapshot: dict[str, object] | None = None,
    cached_logs: dict[str, list[str]] | None = None,
    reserved_top_rows: int = 0,
) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render_status(
            config,
            callbacks,
            options,
            verbose=verbose,
            history=history,
            clear=False,
            snapshot=snapshot,
            cached_logs=cached_logs,
            reserved_top_rows=reserved_top_rows,
        )
    return buffer.getvalue()


def print_status(
    config: dict[str, object],
    callbacks: DashboardCallbacks,
    options: DashboardOptions,
    *,
    verbose: bool = False,
) -> None:
    snapshot = callbacks.collect_status_snapshot(config)
    if runtime_is_down(config, options, snapshot):
        print_down_status(options)
        if verbose:
            print(f"Innate OS repo: {config['os_repo']}")
            print(f"Innate sim repo: {config['sim_repo']}")
            if config["cloud_repo"] is not None:
                print(f"Local cloud-agent repo: {config['cloud_repo']}")
            print(f"State dir: {options.state_dir}")
        return
    render_status(config, callbacks, options, verbose=verbose, snapshot=snapshot)


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
    config: dict[str, object],
    callbacks: DashboardCallbacks,
    options: DashboardOptions,
    *,
    verbose: bool = False,
    refresh_seconds: float = 0.5,
) -> str:
    redraw = True
    history = DashboardHistory()
    simulator_port = config_simulator_port(config)
    sim_log_mode = str(config.get("sim_log_mode", "quiet"))
    top_padding_rows = 1
    try:
        with (
            dashboard_runtime(config, callbacks, options) as runtime,
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
                    paint_terminal_frame(
                        render_status_text(
                            config,
                            callbacks,
                            options,
                            verbose=verbose,
                            history=history,
                            snapshot=snapshot,
                            cached_logs=cached_logs,
                            reserved_top_rows=top_padding_rows,
                        ),
                        top_padding_rows=top_padding_rows,
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
                    if callbacks.set_simulator_log_mode(simulator_port, target_mode):
                        sim_log_mode = target_mode
                        runtime.refresh_snapshot()
                    redraw = True
                    next_refresh = 0.0
                elif normalized == "q":
                    print()
                    callbacks.success(
                        "Left the live dashboard. The Innate runtime is still running."
                    )
                    return "detach"
    except KeyboardInterrupt:
        return "shutdown"

    return "detach"
