"""Temporary, data-only experiment instrumentation; excluded from final patch."""

import functools
import json
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "skill_storage" / "pickup_probe"


def record(kind, **values):
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "events.jsonl").open("a") as out:
        out.write(json.dumps({"kind": kind, "wall": time.time(), **values}) + "\n")


class Response:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def read(self):
        raw = self.wrapped.read()
        try:
            data = json.loads(raw)
            record("usage", model=data.get("model"), usage=data.get("usage"))
        except (ValueError, TypeError):
            record("usage_missing")
        return raw


class Proxy:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    @contextmanager
    def request_stream(self, *args, **kwargs):
        config = ROOT / "budget.json"
        limit = json.loads(config.read_text())["max_calls"] if config.exists() else 40
        log = ROOT / "events.jsonl"
        calls = sum(json.loads(line)["kind"] == "provider_start" for line in log.read_text().splitlines()) if log.exists() else 0
        if calls >= limit:
            raise ValueError("Local pickup experiment call budget exhausted")
        start = time.monotonic()
        record("provider_start", model=kwargs.get("json", {}).get("model"))
        try:
            with self.wrapped.request_stream(*args, **kwargs) as response:
                yield Response(response)
        finally:
            record("provider_end", elapsed_s=time.monotonic() - start)


def install(cls):
    if not hasattr(cls._proxy, "_factory"):
        return
    factory = cls._proxy._factory
    cls._proxy._factory = lambda self: Proxy(factory(self))
    for name in ("execute", "_detect_px", "_wrist_seed", "_wrist_descend", "_wrist_done", "_goto_search_pose",
                 "_push_to_floor", "_pre_close_lift", "_close_once", "_prepare_grasp_retry",
                 "_lift_grasp", "_grasp_verified", "_rest_arm"):
        method = getattr(cls, name)

        def wrap(method, name):
            @functools.wraps(method)
            def measured(self, *args, **kwargs):
                start = time.monotonic()
                record("phase_start", phase=name)
                try:
                    result = method(self, *args, **kwargs)
                    if name in {"_detect_px", "_wrist_seed", "_wrist_done"}:
                        record("observation", phase=name, result=result)
                    return result
                finally:
                    record("phase_end", phase=name, elapsed_s=time.monotonic() - start)
            return measured

        setattr(cls, name, wrap(method, name))
