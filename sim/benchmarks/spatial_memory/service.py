"""Host-side benchmark service: a small HTTP API the sim webapp's Benchmark
panel drives. Runs the capture/annotate/benchmark scripts as subprocesses (one
job at a time) and serves datasets, frames, and results. Stdlib only.

Started by the sim launcher next to the world server (`innate-sim up`); it
never runs on a robot. Binds host-only addresses passed via --bind, exactly
like the world server -- the unauthenticated port must not face the LAN.

    uv run --group benchmark benchmarks/spatial_memory/service.py --bind 127.0.0.1 --port 8801
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIM_DIR = HERE.parents[1]
DATASETS_DIR = HERE / "dataset"
RESULTS_DIR = HERE / "results"

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MODELS_RE = re.compile(r"^[A-Za-z0-9_.,-]{1,200}$")


class Job:
    """One running benchmark subprocess; the service runs at most one."""

    def __init__(self, kind: str, argv: list[str], env: dict[str, str]):
        self.kind = kind
        self.started = time.time()
        self.lines: deque[str] = deque(maxlen=400)
        self.code: int | None = None
        self._proc = subprocess.Popen(
            argv,
            cwd=SIM_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self.lines.append(line.rstrip("\n"))
        self.code = self._proc.wait()

    @property
    def running(self) -> bool:
        return self.code is None and self._proc.poll() is None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "running": self.running,
            "started": self.started,
            "code": self.code,
            "log": list(self.lines),
        }


_job_lock = threading.Lock()
_job: Job | None = None


def _spawn(kind: str, argv: list[str]) -> tuple[int, dict]:
    global _job
    with _job_lock:
        if _job is not None and _job.running:
            return 409, {"error": f"a {_job.kind} job is already running"}
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        _job = Job(kind, [sys.executable, *argv], env)
        return 200, {"ok": True, "kind": kind}


def _dataset_dir(name: str) -> Path | None:
    if not _NAME_RE.match(name):
        return None
    path = (DATASETS_DIR / name).resolve()
    if not path.is_dir() or not path.is_relative_to(DATASETS_DIR):
        return None
    return path


def _dataset_summary(path: Path) -> dict:
    out: dict = {"name": path.name}
    try:
        scene = json.loads((path / "scene.json").read_text())
        out["capture"] = scene.get("capture", {})
        out["props"] = [p["prop"] for p in scene.get("props", [])]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    try:
        index = json.loads((path / "data/spatial_memory/sim_apartment/index.json").read_text())
        out["memories"] = len(index.get("memories", []))
    except (OSError, json.JSONDecodeError):
        out["memories"] = 0
    out["annotated"] = (path / "visibility.json").is_file()
    return out


def _memories_payload(path: Path) -> dict:
    index = json.loads((path / "data/spatial_memory/sim_apartment/index.json").read_text())
    try:
        seen = json.loads((path / "visibility.json").read_text()).get("memories", {})
    except (OSError, json.JSONDecodeError):
        seen = {}
    memories = [dict(m, props=sorted(seen.get(str(m["id"]), {}))) for m in index.get("memories", [])]
    return {"map": index.get("map"), "memories": memories}


def _results_list() -> list[dict]:
    out = []
    if RESULTS_DIR.is_dir():
        for run in sorted(RESULTS_DIR.iterdir(), reverse=True):
            payload = run / "results.json"
            if not payload.is_file():
                continue
            try:
                data = json.loads(payload.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"name": run.name, "meta": data.get("meta", {})})
    return out


def _run_request(body: dict) -> tuple[int, dict]:
    kind = body.get("kind")
    dataset = body.get("dataset") or "home_tour"
    if not _NAME_RE.match(dataset):
        return 400, {"error": "bad dataset name"}
    out_dir = DATASETS_DIR / dataset
    if kind == "capture":
        mode = body.get("mode", "glide")
        if mode not in ("glide", "drive"):
            return 400, {"error": "mode must be glide or drive"}
        return _spawn(kind, [str(HERE / "capture.py"), "--out", str(out_dir), "--mode", mode])
    if kind == "annotate":
        return _spawn(kind, [str(HERE / "annotate.py"), "--dataset", str(out_dir)])
    if kind == "benchmark":
        argv = [str(HERE / "run_benchmark.py"), "--dataset", str(out_dir)]
        fake = body.get("fake")
        if fake:
            if fake not in ("oracle", "first"):
                return 400, {"error": "fake must be oracle or first"}
            argv += ["--fake", fake]
        else:
            models = body.get("models", "gemini-3.6-flash")
            if not _MODELS_RE.match(models):
                return 400, {"error": "bad models list"}
            argv += ["--models", models]
        if body.get("cache") is False:
            argv.append("--no-cache")
        if body.get("query_style") in ("raw", "context"):
            argv += ["--query-style", body["query_style"]]
        return _spawn(kind, argv)
    return 400, {"error": "kind must be capture, annotate or benchmark"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 -- quiet the default request spam
        pass

    def _send(self, code: int, payload: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        if cache:
            # Short-lived: a re-capture rewrites the same dataset URLs in place.
            self.send_header("Cache-Control", "max-age=60")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, payload: dict | list) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler contract
        try:
            self._get()
        except BrokenPipeError:
            pass
        except Exception as error:  # noqa: BLE001 -- one bad request must not kill the thread silently
            self._json(500, {"error": str(error)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._post()
        except BrokenPipeError:
            pass
        except Exception as error:  # noqa: BLE001 -- one bad request must not kill the thread silently
            self._json(500, {"error": str(error)})

    def _post(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/run":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad json"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return
        code, payload = _run_request(body)
        self._json(code, payload)

    def _get(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, {"ok": True, "gemini_key": bool(os.environ.get("GEMINI_API_KEY", "").strip())})
            return
        if path == "/api/state":
            datasets = [_dataset_summary(p) for p in sorted(DATASETS_DIR.iterdir())] if DATASETS_DIR.is_dir() else []
            self._json(
                200,
                {
                    "gemini_key": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
                    "datasets": [d for d in datasets if "capture" in d],
                    "results": _results_list(),
                    "job": _job.as_dict() if _job else None,
                },
            )
            return
        if path == "/api/job":
            self._json(200, _job.as_dict() if _job else {"running": False})
            return

        if match := re.match(r"^/api/results/([A-Za-z0-9_.-]{1,80})$", path):
            payload = RESULTS_DIR / match.group(1) / "results.json"
            if not payload.is_file():
                self._json(404, {"error": "no such run"})
                return
            self._send(200, payload.read_bytes(), "application/json")
            return

        if match := re.match(r"^/api/dataset/([A-Za-z0-9_.-]{1,64})/(scene|manifest|memories)$", path):
            root = _dataset_dir(match.group(1))
            if root is None:
                self._json(404, {"error": "no such dataset"})
                return
            what = match.group(2)
            if what == "scene":
                self._send(200, (root / "scene.json").read_bytes(), "application/json")
            elif what == "manifest":
                rows = [json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines()]
                self._json(200, rows)
            else:
                self._json(200, _memories_payload(root))
            return

        if match := re.match(r"^/api/dataset/([A-Za-z0-9_.-]{1,64})/(memory|frame)/(\d{1,5})\.jpg$", path):
            root = _dataset_dir(match.group(1))
            if root is None:
                self._json(404, {"error": "no such dataset"})
                return
            if match.group(2) == "memory":
                image = root / "data/spatial_memory/sim_apartment" / f"{int(match.group(3))}.jpg"
            else:
                image = root / "frames" / f"{int(match.group(3)):04d}.jpg"
            if not image.is_file():
                self._json(404, {"error": "no such image"})
                return
            self._send(200, image.read_bytes(), "image/jpeg", cache=True)
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind", default="127.0.0.1", help="comma-separated addresses to listen on")
    ap.add_argument("--port", type=int, default=8801)
    args = ap.parse_args()

    servers = []
    for addr in [a.strip() for a in args.bind.split(",") if a.strip()]:
        server = ThreadingHTTPServer((addr, args.port), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"[benchmark-service] listening on http://{addr}:{args.port}", flush=True)
    if not servers:
        raise SystemExit("no bind addresses")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
