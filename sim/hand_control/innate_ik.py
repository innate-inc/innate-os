"""Run Innate's KDL solver locally, or in an isolated Innate runtime on macOS."""

import atexit
import importlib.util
import json
import os
import select
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

Answer = dict[str, Any]


class InnateIK:
    def __init__(self) -> None:
        if importlib.util.find_spec("PyKDL") and importlib.util.find_spec("urdf_parser_py"):
            command = [sys.executable, "-u", str(ROOT / "ik_worker.py")]
        else:
            image = os.environ.get("INNATE_IK_IMAGE")
            if not image:
                images = subprocess.check_output(
                    ["docker", "ps", "--format", "{{.Image}}"], text=True, timeout=5
                ).splitlines()
                image = next((name for name in images if name.startswith("innate-os-")), None)
            if not image:
                raise RuntimeError(
                    "Innate IK needs PyKDL or an Innate Docker runtime. Start innate-sim or set INNATE_IK_IMAGE."
                )
            command = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network=none",
                "--entrypoint=/usr/bin/python3",
                "-v",
                f"{ROOT.parents[1]}:/repo:ro",
                image,
                "-u",
                "/repo/sim/hand_control/ik_worker.py",
            ]
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="innate-ik")
        self.failed = False
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
        self.buffer = b""
        try:
            hello = self.read(20)
            if hello.get("joints") != [f"joint{i}" for i in range(1, 6)]:
                raise RuntimeError(f"Unexpected Innate IK chain: {hello}")
        except Exception:
            self.close()
            raise
        atexit.register(self.close)

    def read(self, timeout: float) -> Answer:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = max(0, deadline - time.monotonic())
            if not select.select([self.process.stdout], [], [], remaining)[0]:
                raise RuntimeError("Innate IK worker timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Innate IK worker stopped")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def submit(
        self, position: Sequence[float], rpy: Sequence[float], seed: Sequence[float], offset: Sequence[float]
    ) -> Future[Answer]:
        return self.executor.submit(self.solve, position, rpy, seed, offset)

    def solve(
        self,
        position: Sequence[float],
        rpy: Sequence[float],
        seed: Sequence[float],
        offset: Sequence[float] = (0, 0, 0),
    ) -> Answer:
        with self.lock:
            if self.failed:
                raise RuntimeError("Innate IK worker is unavailable; restart the studio")
            try:
                payload = json.dumps(
                    {"position": list(position), "rpy": list(rpy), "seed": list(seed), "offset": list(offset)},
                    allow_nan=False,
                )
                self.process.stdin.write(payload.encode() + b"\n")
                answer = self.read(0.8)
                if "error" in answer:
                    raise RuntimeError(answer["error"])
                return answer
            except (RuntimeError, OSError):
                # Never consume a late reply as the answer to a newer pose.
                self.failed = True
                self.process.terminate()
                raise

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=2)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()
        self.executor.shutdown(wait=False, cancel_futures=True)


_worker: InnateIK | None = None


def get_ik() -> InnateIK:
    global _worker
    if _worker is None:
        _worker = InnateIK()
    return _worker
