"""REST endpoints for training runs (Tab 3: Training).

Uses the training client's SkillManager and OrchestratorClient for all
cloud operations (listing runs, checking status, creating runs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import StreamingResponse
from training_client.src.skill_manager import (
    SkillManager,
    _locked_metadata,
    _read_meta,
    read_skill_id,
)
from training_client.src.types import ClientConfig, ProgressUpdate, RunInfo

logger = logging.getLogger("training_manager.api.training")

router = APIRouter(tags=["training"])

_download_jobs: dict[tuple[str, int], dict[str, Any]] = {}
_download_lock = threading.Lock()

HYPERPARAMETER_DEFAULTS: dict[str, str] = {
    "LEARNING_RATE": "5e-5",
    "LEARNING_RATE_BACKBONE": "5e-5",
    "BATCH_SIZE": "96",
    "MAX_STEPS": "120000",
    "CHUNK_SIZE": "30",
    "NUM_WORKERS": "4",
    "WORLD_SIZE": "4",
}

ARCHITECTURE_PARAMS: dict[str, Any] = {
    "vision_backbone": "resnet18",
    "DIM_MODEL": 512,
    "N_HEADS": 8,
    "N_ENCODER_LAYERS": 4,
    "N_DECODER_LAYERS": 4,
    "KL_WEIGHT": 10.0,
    "USE_VAE": True,
    "WEIGHT_DECAY": "5e-4",
    "WARMUP_STEPS": "5% of MAX_STEPS",
    "MIN_LR_RATIO": 0.1,
    "TRAIN_VAL_SPLIT": 0.9,
    "CHECKPOINT_INTERVAL": "MAX_STEPS / 10",
}

INFRASTRUCTURE_DEFAULTS: dict[str, Any] = {
    "preset": "act-default",
    "gpu_type": "5090",
    "min_gpus": 4,
    "max_gpus": 4,
    "hours": 5,
    "budget": 200,
}


def _skills_dir(request: Request) -> Path:
    return Path(request.app.state.skills_dir)


def _make_manager() -> SkillManager:
    config = ClientConfig(
        server_url=os.environ.get("TRAINING_SERVER_URL", ""),
        auth_token=os.environ.get("INNATE_SERVICE_KEY", ""),
        auth_issuer_url=os.environ.get("INNATE_AUTH_ISSUER_URL", ""),
    )
    return SkillManager(config)


def _run_to_dict(r: RunInfo, skill_dir_name: str, skill_name: str) -> dict[str, Any]:
    """Convert a RunInfo dataclass to a JSON-serialisable dict."""
    return {
        "skill_dir_name": skill_dir_name,
        "skill_name": skill_name,
        "skill_id": r.skill_id,
        "run_id": r.run_id,
        "status": r.status,
        "daemon_state": r.daemon_state,
        "error_message": r.error_message,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "instance_ip": r.instance_ip,
        "instance_type": r.instance_type,
        "training_params": r.training_params,
    }


@router.get("/defaults")
async def get_defaults() -> dict[str, Any]:
    """Return default parameter values for new training runs."""
    return {
        "hyperparameters": HYPERPARAMETER_DEFAULTS,
        "architecture": ARCHITECTURE_PARAMS,
        "infrastructure": INFRASTRUCTURE_DEFAULTS,
    }


@router.get("/runs")
async def list_all_runs(request: Request) -> list[dict[str, Any]]:
    """List all training runs across all skills, newest first.

    Uses SkillManager.list_runs() for each skill that has a
    training_skill_id.
    """
    root = _skills_dir(request)
    if not root.is_dir():
        return []

    manager = _make_manager()
    all_runs: list[dict[str, Any]] = []

    for child in sorted(root.iterdir()):
        if (
            not child.is_dir()
            or child.name.startswith(".")
            or child.name == "__pycache__"
            or not (child / "metadata.json").is_file()
        ):
            continue

        skill_id = read_skill_id(child)
        if not skill_id:
            continue

        with _locked_metadata(child) as meta_path:
            meta = _read_meta(meta_path)
        skill_display_name = meta.get("name", child.name)

        try:
            runs = manager.list_runs(skill_id)
        except Exception as e:
            logger.warning("Failed to list runs for %s: %s", child.name, e)
            continue

        for r in runs:
            all_runs.append(_run_to_dict(r, child.name, skill_display_name))

    all_runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return all_runs


@router.get("/runs/{skill_name}/{run_id}")
async def get_run(request: Request, skill_name: str, run_id: int) -> dict[str, Any]:
    """Get details for a specific training run via SkillManager.run_status()."""
    skill_path = _skills_dir(request) / skill_name
    skill_id = read_skill_id(skill_path)
    if not skill_id:
        raise HTTPException(404, f"No training_skill_id for {skill_name}")

    with _locked_metadata(skill_path) as meta_path:
        meta = _read_meta(meta_path)
    skill_display_name = meta.get("name", skill_name)

    manager = _make_manager()
    try:
        r = manager.run_status(skill_id, run_id)
    except Exception as e:
        raise HTTPException(502, f"Failed to get run status: {e}") from e

    return _run_to_dict(r, skill_name, skill_display_name)


@router.get("/runs/{skill_name}/{run_id}/watch")
async def watch_run(
    request: Request, skill_name: str, run_id: int
) -> StreamingResponse:
    """SSE endpoint that polls run status using SkillManager.run_status()."""
    skill_path = _skills_dir(request) / skill_name
    skill_id = read_skill_id(skill_path)
    if not skill_id:
        raise HTTPException(404, f"No training_skill_id for {skill_name}")

    async def generate() -> Any:
        manager = _make_manager()
        while True:
            try:
                r = manager.run_status(skill_id, run_id)
                data = json.dumps({
                    "run_id": r.run_id,
                    "status": r.status,
                    "daemon_state": r.daemon_state,
                    "error_message": r.error_message,
                    "updated_at": r.updated_at,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                })
                yield f"data: {data}\n\n"
                if r.is_terminal:
                    break
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(generate(), media_type="text/event-stream")


class CreateRunRequest(BaseModel):
    preset: str = "act-default"
    repo: str | None = None
    ref: str | None = None
    env: dict[str, str] | None = None
    gpu_type: str | None = None
    min_gpus: int | None = None
    max_gpus: int | None = None
    hours: float | None = None
    budget: float | None = None


@router.post("/runs/{skill_name}")
async def create_run(
    request: Request, skill_name: str, body: CreateRunRequest
) -> dict[str, Any]:
    """Create a new training run via OrchestratorClient.create_run().

    Builds the training_params dict in the same format as the training
    CLI's `run` command, including source_dir for result download.
    """
    skill_path = _skills_dir(request) / skill_name
    skill_id = read_skill_id(skill_path)
    if not skill_id:
        raise HTTPException(404, f"No training_skill_id for {skill_name}")

    manager = _make_manager()

    params: dict[str, Any] = {"preset": body.preset}
    params["source_dir"] = str(skill_path.resolve())

    if body.repo:
        params["repo"] = body.repo
    if body.ref:
        params["ref"] = body.ref
    if body.env:
        params["env"] = body.env
    if body.gpu_type:
        params["gpu_type"] = body.gpu_type
    if body.min_gpus is not None:
        params["min_gpus"] = body.min_gpus
    if body.max_gpus is not None:
        params["max_gpus"] = body.max_gpus
    if body.hours is not None:
        params["hours"] = body.hours
    if body.budget is not None:
        params["budget"] = body.budget

    try:
        result = manager.client.create_run(skill_id, training_params=params)
    except Exception as e:
        raise HTTPException(502, f"Failed to create run: {e}") from e

    logger.info(
        "Created run %s/%s for skill %s", skill_id, result.get("run_id"), skill_name
    )
    return result


# ── Download results ────────────────────────────────────────────────


def _apply_download_progress(
    job: dict[str, Any], update: ProgressUpdate, skill_name: str, run_id: int
) -> None:
    """Apply a ProgressUpdate from SkillManager.download to the job dict."""
    job["stage"] = update.stage.value
    job["message"] = update.message
    fp = update.file_progress
    if fp and fp.total > 0:
        if fp.bytes_total and fp.bytes_total > 0:
            per_file = (fp.bytes_done or 0) / fp.bytes_total
        else:
            per_file = 1.0 if fp.done else 0.0
        job["progress"] = ((fp.index - 1) + per_file) / fp.total
    if update.error:
        job["error"] = update.error
    logger.info(
        "[%s/%d] %s: %s", skill_name, run_id, update.stage.value, update.message
    )


def _run_download(
    skill_id: str,
    skill_name: str,
    run_id: int,
    dest_dir: str,
    job: dict[str, Any],
) -> None:
    """Background thread: download run results via SkillManager."""
    try:
        manager = _make_manager()
        job["stage"] = "downloading"
        job["message"] = f"Downloading run {skill_id}/{run_id}..."
        logger.info("[%s/%d] Starting download...", skill_name, run_id)

        for update in manager.download(skill_id, run_id, dest_dir=dest_dir):
            _apply_download_progress(job, update, skill_name, run_id)

        job["stage"] = "done"
        job["message"] = "Download complete"
        job["done"] = True
        job["progress"] = 1.0
        logger.info("[%s/%d] Download complete", skill_name, run_id)
    except Exception as e:
        job["stage"] = "error"
        job["message"] = str(e)
        job["error"] = str(e)
        job["done"] = True
        logger.error("[%s/%d] Download failed: %s", skill_name, run_id, e)


@router.post("/runs/{skill_name}/{run_id}/download")
async def download_run(
    request: Request, skill_name: str, run_id: int
) -> dict[str, str]:
    """Start a background download of a run's result files.

    Uses SkillManager.download() which writes into the run's
    ``source_dir/{run_id}/`` and marks the run ``downloaded`` on success.
    """
    skill_path = _skills_dir(request) / skill_name
    skill_id = read_skill_id(skill_path)
    if not skill_id:
        raise HTTPException(404, f"No training_skill_id for {skill_name}")

    key = (skill_name, run_id)
    with _download_lock:
        existing = _download_jobs.get(key)
        if existing and not existing.get("done"):
            raise HTTPException(409, "Download already in progress")

        # Drop any stale terminal-state job so a re-download starts fresh.
        _download_jobs.pop(key, None)

        job: dict[str, Any] = {
            "stage": "starting",
            "message": "Starting...",
            "done": False,
            "error": None,
            "progress": 0.0,
        }
        _download_jobs[key] = job

    thread = threading.Thread(
        target=_run_download,
        args=(skill_id, skill_name, run_id, str(skill_path.resolve()), job),
        daemon=True,
    )
    thread.start()

    return {"status": "started"}


@router.get("/runs/{skill_name}/{run_id}/download-status")
async def download_status(
    skill_name: str, run_id: int
) -> dict[str, Any]:
    """Poll the download progress for a run."""
    with _download_lock:
        job = _download_jobs.get((skill_name, run_id))
        if job is None:
            return {
                "stage": "idle",
                "message": "",
                "done": True,
                "error": None,
                "progress": 0.0,
            }
        # Snapshot the job fields under the lock so concurrent writes by the
        # worker thread can't produce an internally inconsistent reply (e.g.
        # done=True observed before progress=1.0 is written).
        return {
            "stage": job["stage"],
            "message": job["message"],
            "done": job["done"],
            "error": job.get("error"),
            "progress": job.get("progress", 0.0),
        }
