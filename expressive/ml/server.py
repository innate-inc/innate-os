"""Planner service (CONTRACTS §7): prompt in, MARS plan-space motion out. The robot calls it over the LAN.

  POST /generate-sparse  {prompt, n=1, seed=0, effort="medium", retries=2} -> {idea, recipe, plans, timing_ms}
  POST /generate-dense   same input                                       -> {idea, recipe, plans, clips, timing_ms}
  GET  /health                                                            -> {planners, generator}

  python -m ml.server --bundle medium=runs/planner-4b/merged --bundle low=runs/planner-08b/merged \\
                      --generator runs/generator/generator.pt [--fp8] [--spec-tokens 3] [--port 8000]

  curl -s innate52.local:8000/generate-dense -H 'content-type: application/json' \\
       -d '{"prompt": "proud. You finally solved the puzzle.", "n": 2}'

``plans`` are the keyframe plans behind each clip (keys every 0.25 s, all 9 channels); ``clips`` are Clip JSON in plan
space (8 channels at 25 Hz), mapped through the basis on the robot at play time.
"""

from __future__ import annotations

import argparse
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .engine import MotionEngine, Planner, PlannerError
from .generator.sample import Generator

app = FastAPI(title="mars-expressive", docs_url=None, redoc_url=None)
# the Expression Studio page calls the service straight from the browser
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["content-type"])
ENGINE: list[MotionEngine] = []

# Fraction of GPU memory each tier's vLLM engine may take (weights + KV/state cache); the generator needs < 1 GB.
GPU_SHARE = {"high": 0.5, "medium": 0.4, "low": 0.15}
# FP8 halves the planners' weights, so the same tiers fit beside other jobs on the shared 5090.
GPU_SHARE_FP8 = {"high": 0.35, "medium": 0.25, "low": 0.08}


class Request(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)
    n: int = Field(default=1, ge=1, le=16)
    seed: int = 0
    effort: Literal["high", "medium", "low"] | None = None
    retries: int = Field(default=2, ge=0, le=8)


def _engine() -> MotionEngine:
    if not ENGINE:
        raise HTTPException(status_code=503, detail="engine still loading")
    return ENGINE[0]


@app.get("/health")
def health() -> dict[str, Any]:
    return _engine().health()


@app.post("/generate-sparse")
def generate_sparse(req: Request) -> dict[str, Any]:
    try:
        return _engine().sparse(req.prompt, req.n, req.seed, req.effort, req.retries)
    except PlannerError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.post("/generate-dense")
def generate_dense(req: Request) -> dict[str, Any]:
    try:
        return _engine().dense(req.prompt, req.n, req.seed, req.effort, req.retries)
    except PlannerError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(
        prog="ml.server", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bundle", action="append", required=True, help="effort=merged planner dir; repeat per tier")
    ap.add_argument("--generator", required=True)
    ap.add_argument("--fp8", action="store_true", help="FP8 planner weights (default bf16)")
    ap.add_argument("--spec-tokens", type=int, default=0, help="MTP draft tokens per step (0 = off)")
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--cfg", type=float, default=1.5)
    ap.add_argument("--gpu-share", type=float, help="GPU memory fraction per planner (default: by tier and precision)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    specs = [b.split("=", 1) for b in a.bundle]
    planners = {
        effort: Planner(
            path,
            a.gpu_share or (GPU_SHARE_FP8 if a.fp8 else GPU_SHARE).get(effort, 0.3),
            fp8=a.fp8,
            spec_tokens=a.spec_tokens,
        )
        for effort, path in specs
    }
    ENGINE.append(MotionEngine(planners, Generator(a.generator, "cuda"), steps=a.steps, cfg=a.cfg))
    print(f"ready: http://{a.host}:{a.port}/generate-dense (effort: {', '.join(sorted(planners))})", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
