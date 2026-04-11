"""REST endpoints for skill metadata (Tab 1: Skills)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from training_client.src.skill_manager import (
    _locked_metadata,
    _read_meta,
    _write_meta,
    read_skill_id,
)

logger = logging.getLogger("training_manager.api.skills")

router = APIRouter(tags=["skills"])


def _skills_dir(request: Request) -> Path:
    return Path(request.app.state.skills_dir)


def _read_dataset_metadata(skill_path: Path) -> dict[str, Any] | None:
    ds_path = skill_path / "data" / "dataset_metadata.json"
    if not ds_path.is_file():
        return None
    import json

    return json.loads(ds_path.read_text())


def _skill_summary(skill_path: Path) -> dict[str, Any] | None:
    """Build a summary dict for a single skill directory."""
    with _locked_metadata(skill_path) as meta_path:
        meta = _read_meta(meta_path)
    if not meta:
        return None

    ds_meta = _read_dataset_metadata(skill_path)
    episode_count = ds_meta.get("number_of_episodes", 0) if ds_meta else 0

    execution = meta.get("execution", {})
    has_checkpoint = bool(execution.get("checkpoint"))

    return {
        "dir_name": skill_path.name,
        "name": meta.get("name", skill_path.name),
        "type": meta.get("type", "learned"),
        "episode_count": episode_count,
        "has_checkpoint": has_checkpoint,
        "training_skill_id": meta.get("training_skill_id"),
        "guidelines": meta.get("guidelines", ""),
    }


@router.get("")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    """List all skills found in the skills directory."""
    root = _skills_dir(request)
    if not root.is_dir():
        return []

    skills: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        summary = _skill_summary(child)
        if summary is not None:
            skills.append(summary)

    return skills


@router.get("/{skill_name}")
async def get_skill(request: Request, skill_name: str) -> dict[str, Any]:
    """Return the full metadata.json for a skill."""
    skill_path = _skills_dir(request) / skill_name
    if not skill_path.is_dir():
        raise HTTPException(404, f"Skill directory not found: {skill_name}")

    with _locked_metadata(skill_path) as meta_path:
        meta = _read_meta(meta_path)
    if not meta:
        raise HTTPException(404, f"No metadata.json in {skill_name}")

    ds_meta = _read_dataset_metadata(skill_path)

    return {
        "dir_name": skill_name,
        "metadata": meta,
        "dataset_metadata": ds_meta,
    }


class SkillUpdate(BaseModel):
    name: str | None = None
    guidelines: str | None = None
    guidelines_when_running: str | None = None
    inputs: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None


@router.put("/{skill_name}")
async def update_skill(
    request: Request, skill_name: str, body: SkillUpdate
) -> dict[str, Any]:
    """Update editable fields in a skill's metadata.json.

    Uses the training client's locked metadata pattern for safe
    concurrent access.
    """
    skill_path = _skills_dir(request) / skill_name
    if not skill_path.is_dir():
        raise HTTPException(404, f"Skill directory not found: {skill_name}")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")

    with _locked_metadata(skill_path) as meta_path:
        meta = _read_meta(meta_path)
        if not meta:
            raise HTTPException(404, f"No metadata.json in {skill_name}")

        if "execution" in updates:
            existing_exec = meta.get("execution", {})
            existing_exec.update(updates["execution"])
            updates["execution"] = existing_exec

        meta.update(updates)
        _write_meta(meta_path, meta)

    logger.info("Updated metadata for skill %s: %s", skill_name, list(updates.keys()))
    return {"status": "ok", "updated_fields": list(updates.keys())}
