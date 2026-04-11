"""REST endpoints for dataset management (Tab 2: Datasets).

Handles episode browsing, video streaming, skill submission/upload
(via the training client's SkillManager), and the copy-with-exclusion
("delete") and merge operations.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from training_client.src.skill_manager import (
    SkillManager,
    _locked_metadata,
    _read_meta,
    _write_meta,
    read_skill_id,
)
from training_client.src.types import ClientConfig, ProgressUpdate

logger = logging.getLogger("training_manager.api.datasets")

router = APIRouter(tags=["datasets"])

_submit_jobs: dict[str, dict[str, Any]] = {}
_submit_lock = threading.Lock()


def _skills_dir(request: Request) -> Path:
    return Path(request.app.state.skills_dir)


def _read_dataset_metadata(skill_path: Path) -> dict[str, Any] | None:
    ds_path = skill_path / "data" / "dataset_metadata.json"
    if not ds_path.is_file():
        return None
    return json.loads(ds_path.read_text())


def _dataset_status(skill_path: Path) -> str:
    """Determine the dataset status string."""
    with _submit_lock:
        job = _submit_jobs.get(skill_path.name)
        if job and not job.get("done"):
            return job.get("stage", "compressing")

    skill_id = read_skill_id(skill_path)
    if skill_id:
        return "uploaded"

    return "not_submitted"


@router.get("")
async def list_datasets(request: Request) -> list[dict[str, Any]]:
    """List all skills with their dataset info."""
    root = _skills_dir(request)
    if not root.is_dir():
        return []

    datasets: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue

        with _locked_metadata(child) as meta_path:
            meta = _read_meta(meta_path)
        if not meta:
            continue

        ds_meta = _read_dataset_metadata(child)
        episode_count = ds_meta.get("number_of_episodes", 0) if ds_meta else 0
        dataset_type = ds_meta.get("dataset_type", "h5") if ds_meta else "unknown"

        datasets.append({
            "dir_name": child.name,
            "name": meta.get("name", child.name),
            "type": meta.get("type", "learned"),
            "episode_count": episode_count,
            "dataset_type": dataset_type,
            "status": _dataset_status(child),
            "training_skill_id": meta.get("training_skill_id"),
        })

    return datasets


@router.get("/{skill_name}")
async def get_dataset(request: Request, skill_name: str) -> dict[str, Any]:
    """Full dataset details including per-episode info."""
    skill_path = _skills_dir(request) / skill_name
    if not skill_path.is_dir():
        raise HTTPException(404, f"Skill not found: {skill_name}")

    with _locked_metadata(skill_path) as meta_path:
        meta = _read_meta(meta_path)
    if not meta:
        raise HTTPException(404, f"No metadata.json for {skill_name}")

    ds_meta = _read_dataset_metadata(skill_path)
    if ds_meta is None:
        raise HTTPException(404, f"No dataset_metadata.json for {skill_name}")

    episodes = ds_meta.get("episodes", [])
    for ep in episodes:
        video_files = ep.get("video_files", [])
        ep["has_video"] = len(video_files) > 0

    return {
        "dir_name": skill_name,
        "name": meta.get("name", skill_name),
        "training_skill_id": meta.get("training_skill_id"),
        "status": _dataset_status(skill_path),
        "dataset_metadata": ds_meta,
    }


@router.get("/{skill_name}/episodes/{episode_id}/video")
async def get_episode_video(
    request: Request, skill_name: str, episode_id: int, camera: int = 0
) -> FileResponse:
    """Stream an episode MP4 file."""
    skill_path = _skills_dir(request) / skill_name
    ds_meta = _read_dataset_metadata(skill_path)
    if ds_meta is None:
        raise HTTPException(404, "No dataset metadata")

    episodes = ds_meta.get("episodes", [])
    episode = next((e for e in episodes if e.get("episode_id") == episode_id), None)
    if episode is None:
        raise HTTPException(404, f"Episode {episode_id} not found")

    video_files = episode.get("video_files", [])
    if camera >= len(video_files):
        raise HTTPException(404, f"Camera {camera} not found for episode {episode_id}")

    video_path = skill_path / "data" / video_files[camera]
    if not video_path.is_file():
        raise HTTPException(404, f"Video file not found: {video_files[camera]}")

    return FileResponse(str(video_path), media_type="video/mp4")


class SubmitRequest(BaseModel):
    server_url: str | None = None
    auth_token: str | None = None
    auth_issuer_url: str | None = None


@router.post("/{skill_name}/submit")
async def submit_skill(
    request: Request, skill_name: str, body: SubmitRequest
) -> dict[str, str]:
    """Trigger skill submission and upload in the background.

    Uses SkillManager.submit() + SkillManager.upload_files() from the
    training client, which handles H.264 conversion, signed-URL uploads,
    verification, and metadata updates.
    """
    skill_path = _skills_dir(request) / skill_name
    if not skill_path.is_dir():
        raise HTTPException(404, f"Skill not found: {skill_name}")

    with _submit_lock:
        existing = _submit_jobs.get(skill_name)
        if existing and not existing.get("done"):
            raise HTTPException(409, "Submission already in progress")

    job: dict[str, Any] = {
        "stage": "compressing",
        "message": "Starting...",
        "done": False,
        "error": None,
        "progress": 0.0,
    }
    with _submit_lock:
        _submit_jobs[skill_name] = job

    thread = threading.Thread(
        target=_run_submit,
        args=(str(skill_path), skill_name, job, body),
        daemon=True,
    )
    thread.start()

    return {"status": "started"}


def _run_submit(
    skill_dir: str,
    skill_name: str,
    job: dict[str, Any],
    params: SubmitRequest,
) -> None:
    """Background thread: submit + upload via SkillManager.

    Iterates the SkillManager generators which handle the full pipeline:
    skill creation, H.264 episode conversion, signed-URL upload, and
    verification.
    """
    try:
        import os

        config = ClientConfig(
            server_url=params.server_url or os.environ.get("TRAINING_SERVER_URL", ""),
            auth_token=params.auth_token or os.environ.get("INNATE_SERVICE_KEY", ""),
            auth_issuer_url=params.auth_issuer_url
            or os.environ.get("INNATE_AUTH_ISSUER_URL", ""),
        )
        manager = SkillManager(config)

        job["stage"] = "creating"
        job["message"] = "Submitting skill..."
        logger.info("[%s] Submitting skill...", skill_name)

        skill_info = None
        for update in manager.submit(skill_dir):
            _apply_progress(job, update, skill_name)
            skill_info = update

        skill_id = read_skill_id(skill_dir)
        if not skill_id:
            raise RuntimeError("No skill_id after submit")

        job["stage"] = "compressing"
        job["message"] = "Converting and uploading data files..."
        logger.info("[%s] Starting upload (skill_id=%s)...", skill_name, skill_id)

        for update in manager.upload_files(skill_id, skill_dir):
            _apply_progress(job, update, skill_name)

        job["stage"] = "done"
        job["message"] = "Upload complete"
        job["done"] = True
        job["progress"] = 1.0
        logger.info("[%s] Submit + upload complete", skill_name)

    except Exception as e:
        job["stage"] = "error"
        job["message"] = str(e)
        job["error"] = str(e)
        job["done"] = True
        logger.error("[%s] Submit failed: %s", skill_name, e)


def _apply_progress(
    job: dict[str, Any], update: ProgressUpdate, skill_name: str
) -> None:
    """Apply a ProgressUpdate from SkillManager to the job dict."""
    job["stage"] = update.stage.value
    job["message"] = update.message
    fp = update.file_progress
    if fp and fp.total > 0:
        job["progress"] = fp.index / fp.total
    if update.error:
        job["error"] = update.error
    logger.info("[%s] %s: %s", skill_name, update.stage.value, update.message)


@router.get("/{skill_name}/submit-status")
async def submit_status(request: Request, skill_name: str) -> dict[str, Any]:
    """Poll the submit/upload progress for a skill."""
    with _submit_lock:
        job = _submit_jobs.get(skill_name)
    if job is None:
        return {"stage": "idle", "message": "", "done": True, "progress": 0.0}
    return {
        "stage": job["stage"],
        "message": job["message"],
        "done": job["done"],
        "error": job.get("error"),
        "progress": job.get("progress", 0.0),
    }


class CopyRequest(BaseModel):
    new_name: str
    excluded_episode_ids: list[int]


@router.post("/{skill_name}/copy")
async def copy_dataset(
    request: Request, skill_name: str, body: CopyRequest
) -> dict[str, str]:
    """Copy a dataset excluding certain episodes (the "delete episodes" operation).

    Creates a new skill directory with all files except the excluded
    episodes.  Uses the training client's locked metadata for safe access.
    Clears training_skill_id, checkpoint, stats_file, and removes run
    directories from the copy.
    """
    root = _skills_dir(request)
    source = root / skill_name
    if not source.is_dir():
        raise HTTPException(404, f"Skill not found: {skill_name}")

    dest_dir_name = body.new_name.replace(" ", "-").lower()
    dest = root / dest_dir_name
    if dest.exists():
        raise HTTPException(409, f"Destination already exists: {dest_dir_name}")

    logger.info(
        "Copying %s -> %s (excluding episodes %s)",
        skill_name,
        dest_dir_name,
        body.excluded_episode_ids,
    )

    dest.mkdir(parents=True)

    with _locked_metadata(source) as meta_path:
        meta = _read_meta(meta_path)

    meta["name"] = body.new_name
    meta.pop("training_skill_id", None)
    execution = meta.get("execution", {})
    execution.pop("checkpoint", None)
    execution.pop("stats_file", None)
    meta["execution"] = execution
    _write_meta(dest / "metadata.json", meta)

    ds_meta = _read_dataset_metadata(source)
    new_idx = 0
    if ds_meta:
        data_dest = dest / "data"
        data_dest.mkdir(parents=True, exist_ok=True)

        kept_episodes: list[dict[str, Any]] = []

        for ep in ds_meta.get("episodes", []):
            if ep.get("episode_id") in body.excluded_episode_ids:
                continue

            old_filename = ep.get("file_name", "")
            new_filename = f"episode_{new_idx}.h5"
            old_h5 = source / "data" / old_filename
            if old_h5.is_file():
                shutil.copy2(str(old_h5), str(data_dest / new_filename))

            new_ep = {**ep, "episode_id": new_idx, "file_name": new_filename}
            new_video_files: list[str] = []
            for vf in ep.get("video_files", []):
                old_vf_path = source / "data" / vf
                stem = Path(old_filename).stem
                new_stem = f"episode_{new_idx}"
                new_vf = vf.replace(stem, new_stem)
                if old_vf_path.is_file():
                    shutil.copy2(str(old_vf_path), str(data_dest / new_vf))
                    new_video_files.append(new_vf)
            if new_video_files:
                new_ep["video_files"] = new_video_files

            kept_episodes.append(new_ep)
            new_idx += 1

        new_ds_meta = {
            "data_frequency": ds_meta.get("data_frequency", 10),
            "dataset_type": ds_meta.get("dataset_type", "h5"),
            "number_of_episodes": len(kept_episodes),
            "episodes": kept_episodes,
        }
        (data_dest / "dataset_metadata.json").write_text(
            json.dumps(new_ds_meta, indent=4) + "\n"
        )

    logger.info("Copy complete: %s (%d episodes)", dest_dir_name, new_idx)
    return {"status": "ok", "new_dir_name": dest_dir_name}


class MergeSource(BaseModel):
    skill_name: str
    episode_ids: list[int]


class MergeRequest(BaseModel):
    new_name: str
    sources: list[MergeSource]


@router.post("/merge")
async def merge_datasets(request: Request, body: MergeRequest) -> dict[str, str]:
    """Merge selected episodes from multiple skills into a new skill directory.

    Uses the training client's locked metadata for safe reads from source
    skills.  The new skill gets a fresh metadata.json with empty execution.
    """
    root = _skills_dir(request)
    dest_dir_name = body.new_name.replace(" ", "-").lower()
    dest = root / dest_dir_name
    if dest.exists():
        raise HTTPException(409, f"Destination already exists: {dest_dir_name}")

    logger.info("Merging into %s from %d sources", dest_dir_name, len(body.sources))

    dest.mkdir(parents=True)
    data_dest = dest / "data"
    data_dest.mkdir()

    merged_episodes: list[dict[str, Any]] = []
    data_frequency: int | None = None
    dataset_type: str = "h5"
    new_idx = 0

    for src in body.sources:
        src_path = root / src.skill_name
        if not src_path.is_dir():
            raise HTTPException(404, f"Source skill not found: {src.skill_name}")

        src_ds = _read_dataset_metadata(src_path)
        if src_ds is None:
            raise HTTPException(404, f"No dataset metadata in {src.skill_name}")

        src_type = src_ds.get("dataset_type", "h5")
        if src_type != "h264":
            raise HTTPException(
                400,
                f"Source {src.skill_name} has dataset_type '{src_type}'; "
                "only h264 datasets can be merged",
            )

        if data_frequency is None:
            data_frequency = src_ds.get("data_frequency", 10)
        dataset_type = src_type

        for ep in src_ds.get("episodes", []):
            if ep.get("episode_id") not in src.episode_ids:
                continue

            old_filename = ep.get("file_name", "")
            new_filename = f"episode_{new_idx}.h5"
            old_h5 = src_path / "data" / old_filename
            if old_h5.is_file():
                shutil.copy2(str(old_h5), str(data_dest / new_filename))

            new_ep: dict[str, Any] = {
                "episode_id": new_idx,
                "file_name": new_filename,
                "start_timestamp": ep.get("start_timestamp", ""),
                "end_timestamp": ep.get("end_timestamp", ""),
            }

            new_video_files: list[str] = []
            stem = Path(old_filename).stem
            new_stem = f"episode_{new_idx}"
            for vf in ep.get("video_files", []):
                old_vf = src_path / "data" / vf
                new_vf = vf.replace(stem, new_stem)
                if old_vf.is_file():
                    shutil.copy2(str(old_vf), str(data_dest / new_vf))
                    new_video_files.append(new_vf)
            if new_video_files:
                new_ep["video_files"] = new_video_files

            merged_episodes.append(new_ep)
            new_idx += 1

    new_ds_meta = {
        "data_frequency": data_frequency or 10,
        "dataset_type": dataset_type,
        "number_of_episodes": len(merged_episodes),
        "episodes": merged_episodes,
    }
    (data_dest / "dataset_metadata.json").write_text(
        json.dumps(new_ds_meta, indent=4) + "\n"
    )

    meta: dict[str, Any] = {
        "name": body.new_name,
        "type": "learned",
        "guidelines": "",
        "guidelines_when_running": "",
        "inputs": {},
        "execution": {},
    }
    _write_meta(dest / "metadata.json", meta)

    logger.info("Merge complete: %s (%d episodes)", dest_dir_name, new_idx)
    return {"status": "ok", "new_dir_name": dest_dir_name}
