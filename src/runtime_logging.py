import io
import re
import sys
import threading


SIM_LOG_MODE_DEBUG = "debug"
SIM_LOG_MODE_QUIET = "quiet"
SIM_LOG_MODES = (SIM_LOG_MODE_DEBUG, SIM_LOG_MODE_QUIET)
DEFAULT_SIM_LOG_MODE = SIM_LOG_MODE_DEBUG

NOISY_PATTERNS = [
    re.compile(r'^INFO:\s+\d+\.\d+\.\d+\.\d+:\d+\s+-\s+"GET /(video_feeds_ready|stack_metrics)\b'),
    re.compile(r"^\[ROSBridge\] Received navigation path with \d+ waypoints"),
    re.compile(r"^\[ROSBridge\] Target final orientation: "),
    re.compile(r"^\[NavController\] Received navigation path with \d+ waypoints"),
    re.compile(r"^\[NavController\] Reached waypoint \d+"),
    re.compile(r"^\[NavController\] Path: NavigationPathMsg"),
    re.compile(r"^\[NavController\] Path: \("),
    re.compile(r"^\[NavController\] Starting path following with \d+ waypoints"),
    re.compile(r"^\[NavController\] Sent trajectory visualization command"),
    re.compile(r"^\[NavController\] Position reached but orientation off by "),
    re.compile(r"^\[NavController\] Reached final goal at "),
    re.compile(r"^\[NavController\] Cleared trajectory visualization"),
    re.compile(r"^\[NavController\] Navigation ended with status: "),
    re.compile(r"^Waypoint \d+"),
    re.compile(r"^Commanded vel: "),
    re.compile(r"^\[SimulationNode\] Drawing trajectory: "),
    re.compile(r"^\[SimulationNode\] Trajectory visualization complete: "),
    re.compile(r"^\[SimulationNode\] Clearing \d+ trajectory objects"),
    re.compile(r"^\[ROSBridge\] Queue status: "),
    re.compile(r"^\[ROSBridge\] Chat message latency: "),
]


def normalize_sim_log_mode(value: str | None) -> str:
    if not value:
        return DEFAULT_SIM_LOG_MODE
    normalized = value.strip().lower()
    if normalized not in SIM_LOG_MODES:
        return DEFAULT_SIM_LOG_MODE
    return normalized


def is_debug_log_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False

    lowered = stripped.lower()
    if any(token in lowered for token in ("error", "warning", "failed", "exception")):
        return False

    return any(pattern.search(stripped) for pattern in NOISY_PATTERNS)


class RuntimeLogStream(io.TextIOBase):
    def __init__(self, wrapped, get_mode):
        self._wrapped = wrapped
        self._get_mode = get_mode
        self._buffer = ""
        self._lock = threading.Lock()

    def _should_emit(self, chunk: str) -> bool:
        mode = normalize_sim_log_mode(self._get_mode())
        if mode == SIM_LOG_MODE_DEBUG:
            return True
        return not is_debug_log_line(chunk)

    def _emit(self, chunk: str) -> None:
        if not chunk:
            return
        if self._should_emit(chunk):
            self._wrapped.write(chunk)

    def write(self, data: str) -> int:
        if not data:
            return 0

        with self._lock:
            self._buffer += data
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._emit(line + "\n")
        return len(data)

    def flush(self) -> None:
        with self._lock:
            if self._buffer:
                self._emit(self._buffer)
                self._buffer = ""
            self._wrapped.flush()

    def isatty(self) -> bool:
        return self._wrapped.isatty()

    def fileno(self) -> int:
        return self._wrapped.fileno()

    def writable(self) -> bool:
        return True

    @property
    def encoding(self):
        return getattr(self._wrapped, "encoding", None)

    @property
    def errors(self):
        return getattr(self._wrapped, "errors", None)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def install_runtime_log_filter(shared_queues) -> None:
    if not isinstance(sys.stdout, RuntimeLogStream):
        sys.stdout = RuntimeLogStream(sys.stdout, shared_queues.get_sim_log_mode)
    if not isinstance(sys.stderr, RuntimeLogStream):
        sys.stderr = RuntimeLogStream(sys.stderr, shared_queues.get_sim_log_mode)
