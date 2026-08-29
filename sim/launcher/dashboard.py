# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import contextlib
import io
import os
import re
import select
import shlex
import shutil
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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

# COLORTERM is the usual signal, but ssh forwards only TERM (it is not in the
# default SendEnv), so a session reached over ssh -- `limactl shell` included --
# arrives without it. A TERM advertising direct color is the second witness.
TRUECOLOR = USE_COLOR and (
    os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}
    or "direct" in os.environ.get("TERM", "")
    or "truecolor" in os.environ.get("TERM", "")
)
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
        "C": "Ɔ",
        "Ɔ": "C",
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
    capture_os_brain_logs: Callable[..., list[str]]
    success: Callable[[str], None]


@dataclass(frozen=True)
class DashboardOptions:
    cli_sim: str
    state_dir: Path


class DashboardRuntime:
    def __init__(
        self,
        config: dict[str, object],
        callbacks: DashboardCallbacks,
        *,
        log_cache_lines: int = 160,
    ):
        self.config = config
        self.callbacks = callbacks
        self.log_cache_lines = log_cache_lines
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.snapshot = callbacks.collect_status_snapshot(config)
        self.snapshot_rev = 1
        self.logs: dict[str, list[str]] = self._collect_logs(self.snapshot)
        self.log_rev = 1

    def _collect_logs(self, snapshot: dict[str, object]) -> dict[str, list[str]]:
        return {"brain": self.callbacks.capture_os_brain_logs(self.config, lines=self.log_cache_lines)}

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


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STEP_LABEL_WIDTH = 8
# As scripts/install-sim.sh draws them: a step that only spins cannot be told
# apart from a hang.
STEP_TAIL_ROWS = 3
SELECTED, UNSELECTED = "●", "○"


EDITORS = ("cursor", "code", "windsurf", "zed", "subl")


def detect_editor() -> str | None:
    """The first editor on PATH, matching what scripts/install-sim.sh offers at
    the end of an install. None rather than a guess: a command that is not
    installed is worse than no suggestion."""
    return next((editor for editor in EDITORS if shutil.which(editor)), None)


def menus_supported() -> bool:
    return bool(termios and tty and sys.stdin.isatty() and sys.stdout.isatty())


CURSOR_HIDE, CURSOR_SHOW = "\033[?25l", "\033[?25h"


def _set_cursor(sequence: str) -> None:
    """A cursor parked after the last thing drawn reads as an unanswered text
    prompt. Every path that hides it restores it in a finally."""
    if USE_COLOR:
        sys.stdout.write(sequence)
        sys.stdout.flush()


@contextlib.contextmanager
def _raw_terminal():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        _set_cursor(CURSOR_HIDE)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        _set_cursor(CURSOR_SHOW)


def _read_key() -> str:
    """One keypress as a name: up/down/left/right/enter, or the character."""
    char = sys.stdin.read(1)
    if char == "\x03":  # Ctrl-C: raw mode swallows the signal
        raise KeyboardInterrupt
    if char == "":
        raise EOFError
    if char in ("\r", "\n"):
        return "enter"
    if char != "\x1b":
        return char
    if sys.stdin.read(1) != "[":
        return "escape"
    return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(sys.stdin.read(1), "escape")


def _print_options(lines: list[str], *, redraw: bool) -> None:
    if redraw:
        sys.stdout.write(f"\033[{len(lines)}A")
    for line in lines:
        sys.stdout.write("\r\033[K" + line + "\r\n")
    sys.stdout.flush()


def select_one(question: str, options: list[tuple[str, str]], *, default: int = 0) -> int:
    """Arrow-key single select; returns the chosen index.

    `options` are (label, hint) pairs. Callers must check menus_supported()
    first and offer a typed prompt when it is False -- a pipe or a dumb
    terminal has no arrow keys to press.
    """
    index = max(0, min(default, len(options) - 1))
    print(f"\n  {BOLD}{question}{NC}")
    width = max(len(label) for label, _ in options)
    redraw = False
    with _raw_terminal():
        while True:
            lines = [
                f"  {GREEN}{SELECTED}{NC} {BOLD}{label:<{width}}{NC}  {DIM}{hint}{NC}"
                if position == index
                else f"  {DIM}{UNSELECTED} {label:<{width}}  {hint}{NC}"
                for position, (label, hint) in enumerate(options)
            ]
            _print_options(lines, redraw=redraw)
            redraw = True
            key = _read_key()
            if key in ("up", "k"):
                index = (index - 1) % len(options)
            elif key in ("down", "j"):
                index = (index + 1) % len(options)
            elif key.isdigit() and 1 <= int(key) <= len(options):
                index = int(key) - 1
            elif key == "enter":
                return index


def confirm(question: str, *, default: bool = True) -> bool:
    """Left/right yes-no. Same caller contract as select_one."""
    yes = default
    redraw = False
    with _raw_terminal():
        while True:
            marks = [
                f"{GREEN}{SELECTED} {BOLD}Yes{NC}" if yes else f"{DIM}{UNSELECTED} Yes{NC}",
                f"{GREEN}{SELECTED} {BOLD}No{NC}" if not yes else f"{DIM}{UNSELECTED} No{NC}",
            ]
            _print_options([f"  {BOLD}{question}{NC}   {marks[0]}   {marks[1]}"], redraw=redraw)
            redraw = True
            key = _read_key()
            if key in ("left", "right", "h", "l", "\t"):
                yes = not yes
            elif key in ("y", "Y"):
                yes = True
            elif key in ("n", "N"):
                yes = False
            elif key == "enter":
                return yes


class LiveStep:
    """One line that spins while work happens and settles into a check mark.

    The same shape scripts/install-sim.sh draws for Docker and uv, so an install
    that starts in the shell installer and finishes in the launcher reads as
    one process. Whatever the step logs while it runs becomes the detail after
    the message, rather than scrolling the line away.
    """

    def __init__(self, label: str, message: str, done: str | None = None) -> None:
        self.label = label
        self.message = message
        self.tail: list[str] = []
        # What it says once finished; the message is what it says while
        # working, which wants a verb.
        self.done = done or message
        self.detail = ""
        # For work that reports failure by returning rather than raising.
        self.ok = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def live(self) -> bool:
        return USE_COLOR and sys.stdout.isatty()

    def start(self) -> None:
        if not self.live:
            print(f"  {self.label:>{STEP_LABEL_WIDTH}}  {self.message}")
            return
        _set_cursor(CURSOR_HIDE)
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()

    def _width(self) -> int:
        # A line wider than the terminal wraps, and then \r returns to the
        # start of the WRAPPED row -- every redraw leaves the previous one
        # behind, which is how a spinner becomes a wall of repeated lines.
        return shutil.get_terminal_size((100, 40)).columns - 1

    def _render(self, mark: str) -> str:
        detail = f"  {DIM}{self.detail}{NC}" if self.detail else ""
        line = f"  {CYAN}{self.label:>{STEP_LABEL_WIDTH}}{NC}  {mark} {self.message}{detail}"
        return clip_to_width(line, self._width())

    def _draw(self, mark: str) -> None:
        """Status line plus the log tail, written once and stepped back over.

        One write, not a clear then a draw: the gap between them reads as
        flicker. \033[J from the top wipes whatever the last frame used, so the
        tail can grow and shrink without bookkeeping.
        """
        width = self._width()
        gutter = " " * STEP_LABEL_WIDTH
        rows = [self._render(mark)]
        rows += [clip_to_width(f"  {DIM}{gutter}  │ {line}{NC}", width) for line in self.tail]
        body = "\r\033[J" + "\n".join(f"\033[K{row}" for row in rows)
        step_back = f"\033[{len(rows) - 1}A" if len(rows) > 1 else ""
        sys.stdout.write(body + step_back + "\r")
        sys.stdout.flush()

    def _animate(self) -> None:
        frame = 0
        while not self._stop.wait(0.12):
            self._draw(SPINNER_FRAMES[frame % len(SPINNER_FRAMES)])
            frame += 1

    def retitle(self, message: str) -> None:
        self.message = message

    def note(self, message: str) -> None:
        """Print a full line above the spinner, which redraws on its next tick."""
        if self.live:
            sys.stdout.write("\r\033[J")
        print(message)

    def finish(self, *, ok: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self.live:
            # \033[J, not \033[K: the tail rows below have to go too.
            sys.stdout.write("\r\033[J")
            _set_cursor(CURSOR_SHOW)
        self.tail = []
        self.detail = ""
        self.message = self.done
        print(self._render(f"{GREEN}✔{NC}" if ok else f"{RED}✗{NC}"))


_active_step: LiveStep | None = None


def active_step() -> LiveStep | None:
    return _active_step


@contextlib.contextmanager
def live_step(label: str, message: str, done: str | None = None):
    global _active_step
    step = LiveStep(label, message, done)
    previous, _active_step = _active_step, step
    step.start()
    try:
        yield step
    except BaseException:
        step.finish(ok=False)
        raise
    else:
        step.finish(ok=step.ok)
    finally:
        _active_step = previous


def clean_log_line(line: str) -> str:
    """Safe to draw inside a step's frame: an escape would walk the cursor out of
    the redrawn region, and only the last \r segment is what a terminal shows."""
    return ANSI_ESCAPE_RE.sub("", line.split("\r")[-1]).replace("\t", " ").rstrip()


def clip_to_width(text: str, width: int) -> str:
    """Cut to `width` visible columns, keeping colour codes (which occupy
    none) and closing with a reset so a cut mid-sequence cannot bleed."""
    if visible_text_width(text) <= width:
        return text
    out: list[str] = []
    visible = 0
    index = 0
    while index < len(text) and visible < width:
        escape = ANSI_ESCAPE_RE.match(text, index)
        if escape:
            out.append(escape.group())
            index = escape.end()
            continue
        out.append(text[index])
        visible += char_display_width(text[index])
        index += 1
    return "".join(out) + NC


def render_progress_bar(fraction: float, width: int = 22) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return f"{GREEN}{'█' * filled}{DIM}{'░' * (width - filled)}{NC}"


def format_bytes(count: float) -> str:
    if count >= 1 << 30:
        return f"{count / (1 << 30):.1f} GB"
    return f"{count / (1 << 20):.0f} MB"


def divider() -> None:
    print(f"{DIM}{'━' * 72}{NC}")


def divider_line(width: int) -> str:
    return colorize("━" * max(width, 1), fg=THEME["dim"], dim=True)


def clear_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


BANNER_SHOWN_ENV = "INNATE_BANNER_SHOWN"


def print_banner() -> None:
    # No clear_screen: the user's scrollback (earlier runs, their own
    # commands) is theirs. The live dashboard uses the alternate screen
    # buffer, so it never needs a destructive clear either.
    #
    # Once per session, not once per command: the installer runs `setup` and
    # then `up`, and `up` re-execs itself under `sg`, so the same wordmark
    # would greet the same person four times in one install. Exported, so the
    # commands this process spawns inherit the answer.
    if os.environ.get(BANNER_SHOWN_ENV):
        return
    os.environ[BANNER_SHOWN_ENV] = "1"
    divider()
    print_ascii_banner()
    print(f"{DIM}one env // one cli // os + sim{NC}")
    divider()


def format_level(level: str, label: str) -> str:
    if level == "healthy":
        color = GREEN
    elif level == "warn":
        color = YELLOW
    else:
        color = RED
    return f"{color}{label}{NC}"


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


def blend_rgb(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    ratio = max(0.0, min(1.0, ratio))
    return tuple(int(round(start[index] + (end[index] - start[index]) * ratio)) for index in range(3))


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


def dashboard_snapshot_worker(runtime: DashboardRuntime, interval_seconds: float = 1.0) -> None:
    while not runtime.stop_event.is_set():
        runtime.refresh_snapshot()
        runtime.stop_event.wait(interval_seconds)


def dashboard_brain_log_worker(runtime: DashboardRuntime, interval_seconds: float = 0.75) -> None:
    while not runtime.stop_event.is_set():
        runtime.set_log(
            "brain",
            runtime.callbacks.capture_os_brain_logs(runtime.config, lines=runtime.log_cache_lines),
        )
        runtime.stop_event.wait(interval_seconds)


@contextlib.contextmanager
def dashboard_runtime(
    config: dict[str, object],
    callbacks: DashboardCallbacks,
):
    runtime = DashboardRuntime(config, callbacks)
    threads = [
        threading.Thread(target=dashboard_snapshot_worker, args=(runtime,), daemon=True),
        threading.Thread(target=dashboard_brain_log_worker, args=(runtime,), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        yield runtime
    finally:
        runtime.stop_event.set()
        for thread in threads:
            thread.join(timeout=1.0)


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
        rendered.append((" " * offset) + colorize(padded, fg=THEME["fps_end"], bold=True) + (" " * (travel - offset)))

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


def print_log_pane(
    title: str,
    lines: list[str],
    border_rgb: tuple[int, int, int],
    *,
    available_height: int,
) -> None:
    if available_height < 3:
        print(
            colorize(
                "Terminal too short for the live log pane. Enlarge the window to show it.",
                fg=THEME["dim"],
                dim=True,
            )
        )
        return

    width = shutil.get_terminal_size((150, 40)).columns
    for row in render_log_box(title, lines, width=width, height=available_height, border_rgb=border_rgb):
        print(row)


def runtime_is_down(snapshot: dict[str, object]) -> bool:
    return not bool(snapshot["os_running"]) and not bool(snapshot["sim_running"])


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
    clear: bool = True,
    snapshot: dict[str, object] | None = None,
    cached_logs: dict[str, list[str]] | None = None,
    reserved_top_rows: int = 0,
) -> None:
    if clear:
        clear_screen()
    if snapshot is None:
        snapshot = callbacks.collect_status_snapshot(config)
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
    print_dashboard_line(
        "  ".join(
            [
                f"{BOLD}Mood:{NC} {format_level(str(snapshot['stack_level']), str(snapshot['stack_label']))}",
                f"{BOLD}World:{NC} {format_level(str(snapshot['world_level']), str(snapshot['world_label']))}",
                f"{BOLD}Sim driver:{NC} {format_level(str(snapshot['sim_level']), str(snapshot['sim_label']))}",
                f"{BOLD}Transport:{NC} {format_level(str(snapshot['transport_level']), str(snapshot['transport_label']))}",
                f"{BOLD}Brain:{NC} {format_level(str(snapshot['brain_level']), str(snapshot['brain_label']))}",
                f"{BOLD}LLM:{NC} {format_level(str(snapshot['llm_level']), str(snapshot['llm_label']))}",
            ]
        ),
        term_width,
    )
    used_lines += 1
    print_dashboard_line(f"{BOLD}System:{NC} {snapshot['system_summary']}", term_width)
    used_lines += 1
    if term_height >= 24:
        for marquee_line in render_robot_marquee(term_width):
            print(marquee_line)
            used_lines += 1

    # The two places someone actually goes while the sim runs: the browser,
    # and the checkout they write skills in. Everything else on this screen is
    # for when something has gone wrong, so spend the whitespace here.
    print()
    checkout = str(config["os_repo"])
    # What to do, then where -- an instruction reads better than a URL with a
    # caption after it. The editor row is only offered when one is installed.
    destinations = [
        (GREEN, "▸", "Open simulator in browser", str(config["webapp_url"])),
        (CYAN, "✎", "Edit code in", checkout),
    ]
    editor = detect_editor()
    if editor:
        destinations.append((DIM, "⌨", "Open it in your editor", f"{editor} {shlex.quote(checkout)}"))
    label_width = max(len(label) for _, _, label, _ in destinations)
    for colour, mark, label, value in destinations:
        print_dashboard_line(
            f"    {colour}{mark}{NC}  {BOLD}{label:<{label_width}}{NC}   {colour}{value}{NC}",
            term_width,
        )
    print()
    used_lines += len(destinations) + 2
    print_dashboard_line(
        "   ".join(
            [
                f"{DIM}rosbridge{NC} {config['rosbridge_url']}",
                f"{DIM}foxglove{NC} ws://localhost:{config['foxglove_port']}",
                f"{DIM}logs{NC} {options.cli_sim} logs startup",
                f"{DIM}shell{NC} {options.cli_sim} sh",
            ]
        ),
        term_width,
    )
    used_lines += 1
    print_dashboard_line(
        f"{DIM}Keys: q detach  v verbose  Ctrl+C stop runtime{NC}",
        term_width,
    )
    used_lines += 1

    if runtime_is_down(snapshot):
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
            print_dashboard_line(f"{BOLD}State dir:{NC} {options.state_dir}", term_width)
        return

    if verbose:
        used_lines += 5
    available_height = max(term_height - used_lines, 0)
    visible_log_rows = max(available_height, 3)
    brain_lines = (
        cached_logs["brain"]
        if cached_logs is not None and "brain" in cached_logs
        else callbacks.capture_os_brain_logs(config, lines=visible_log_rows)
    )
    print_log_pane("OS BRAIN LOGS", brain_lines, THEME["log_brain"], available_height=available_height)
    if verbose:
        print()
        print(divider_line(term_width))
        print_dashboard_line(f"{BOLD}Innate OS repo:{NC} {config['os_repo']}", term_width)
        print_dashboard_line(f"{BOLD}Innate sim repo:{NC} {config['sim_repo']}", term_width)
        print_dashboard_line(f"{BOLD}State dir:{NC} {options.state_dir}", term_width)


def render_status_text(
    config: dict[str, object],
    callbacks: DashboardCallbacks,
    options: DashboardOptions,
    *,
    verbose: bool = False,
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
    if runtime_is_down(snapshot):
        print_down_status(options)
        if verbose:
            print(f"Innate OS repo: {config['os_repo']}")
            print(f"Innate sim repo: {config['sim_repo']}")
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
    top_padding_rows = 1
    try:
        with (
            dashboard_runtime(config, callbacks) as runtime,
            live_dashboard_terminal(),
            dashboard_input_mode() as input_mode_enabled,
        ):
            snapshot, cached_logs, snapshot_rev, log_rev = runtime.read()
            last_snapshot_rev = snapshot_rev
            last_log_rev = log_rev
            next_refresh = 0.0
            while True:
                now = time.monotonic()
                snapshot, cached_logs, snapshot_rev, log_rev = runtime.read()
                if snapshot_rev != last_snapshot_rev:
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
                            snapshot=snapshot,
                            cached_logs=cached_logs,
                            reserved_top_rows=top_padding_rows,
                        ),
                        top_padding_rows=top_padding_rows,
                    )
                    sys.stdout.flush()
                    next_refresh = now + refresh_seconds
                    redraw = False

                key = read_dashboard_key(0.2 if input_mode_enabled else max(next_refresh - time.monotonic(), 0.1))
                if key is None:
                    continue

                normalized = key.lower()
                if normalized == "v":
                    verbose = not verbose
                    redraw = True
                elif normalized == "q":
                    print()
                    callbacks.success("Left the live dashboard. The Innate runtime is still running.")
                    return "detach"
    except KeyboardInterrupt:
        return "shutdown"

    return "detach"
