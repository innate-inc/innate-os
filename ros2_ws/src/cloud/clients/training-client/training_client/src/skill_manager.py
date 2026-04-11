"""
Core skill+run lifecycle manager.

Exposes generator functions that yield :class:`ProgressUpdate` at each step.
Consumers (CLI, ROS node) iterate these generators to drive the workflow and
present progress however they like.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from requests.exceptions import ConnectionError, Timeout

from .client import APIError, OrchestratorClient
from .downloader import download_results, download_skill_data, download_files
from .types import (
    ClientConfig,
    RunInfo,
    SkillInfo,
    ProgressStage,
    ProgressUpdate,
)
from .episode_converter import convert_episodes_to_h264
from .uploader import upload_data_files

logger = logging.getLogger(__name__)

SKILL_JSON = "server-skill.json"
METADATA_JSON = "metadata.json"
_LOCK_TIMEOUT = 10  # seconds

_URL_REQUEST_MAX_RETRIES = 3
_URL_REQUEST_BACKOFF_BASE = 5  # seconds


@contextmanager
def _locked_metadata(skill_dir: Path):
    """Context manager that holds an exclusive flock on metadata.json itself.

    Tries a non-blocking acquire first; if the lock is held by another
    process, retries for up to ``_LOCK_TIMEOUT`` seconds then takes it
    with a blocking call.

    Yields the Path to metadata.json.  The lock is released when the
    context exits.
    """
    meta_path = skill_dir / METADATA_JSON
    meta_path.touch(exist_ok=True)
    fd = meta_path.open("r+")
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Lock on %s not acquired after %ds — forcing",
                        meta_path,
                        _LOCK_TIMEOUT,
                    )
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    break
                time.sleep(0.2)
        yield meta_path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _read_meta(meta_path: Path) -> dict:
    """Read and parse metadata.json, returning {} on missing/corrupt file."""
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_meta(meta_path: Path, meta: dict) -> None:
    """Write metadata dict to metadata.json."""
    meta_path.write_text(json.dumps(meta, indent=4) + "\n")


def _migrate_skill_id(skill_dir: Path) -> None:
    """Migrate skill_id from server-skill.json to metadata.json if needed.

    Must be called inside a ``_locked_metadata`` context.
    """
    old_path = skill_dir / SKILL_JSON
    meta_path = skill_dir / METADATA_JSON

    if not old_path.is_file():
        return

    try:
        old_data = json.loads(old_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read %s during migration: %s", old_path, e)
        return

    old_id = old_data.get("id")
    if not old_id:
        return

    meta = _read_meta(meta_path)
    meta["training_skill_id"] = old_id
    _write_meta(meta_path, meta)
    old_path.unlink(missing_ok=True)
    logger.info(
        "Migrated skill_id %s from %s → %s", old_id, old_path.name, meta_path.name
    )


def read_skill_id(skill_dir: str | Path) -> str | None:
    """Read the training_skill_id from metadata.json inside *skill_dir*.

    Automatically migrates from the legacy server-skill.json format first.
    """
    skill_dir = Path(skill_dir)
    with _locked_metadata(skill_dir) as meta_path:
        _migrate_skill_id(skill_dir)
        data = _read_meta(meta_path)
        return data.get("training_skill_id")


def write_skill_id(skill_dir: str | Path, skill_id: str) -> None:
    """Write the training_skill_id to metadata.json inside *skill_dir*."""
    skill_dir = Path(skill_dir)
    with _locked_metadata(skill_dir) as meta_path:
        meta = _read_meta(meta_path)
        meta["training_skill_id"] = skill_id
        _write_meta(meta_path, meta)
        logger.info("Wrote training_skill_id %s to %s", skill_id, meta_path)


def read_local_episode_count(skill_dir: str | Path) -> int:
    """Read ``number_of_episodes`` from ``data/dataset_metadata.json``.

    This file is written by the C++ TaskManager when episodes are recorded.
    Returns 0 if the file is missing or the key is absent.
    """
    meta_path = Path(skill_dir) / "data" / "dataset_metadata.json"
    if not meta_path.is_file():
        return 0
    try:
        data = json.loads(meta_path.read_text())
        return int(data.get("number_of_episodes", 0))
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return 0


def read_uploaded_episode_count(skill_dir: str | Path) -> int:
    """Read ``uploaded_episode_count`` from ``metadata.json``.

    Returns -1 if the key is absent (meaning never uploaded).
    Plain JSON read without file locking since it's read-only.
    """
    meta_path = Path(skill_dir) / METADATA_JSON
    data = _read_meta(meta_path)
    return int(data.get("uploaded_episode_count", -1))


def write_uploaded_episode_count(skill_dir: str | Path, count: int) -> None:
    """Persist ``uploaded_episode_count`` into ``metadata.json`` under the file lock."""
    skill_dir = Path(skill_dir)
    with _locked_metadata(skill_dir) as meta_path:
        meta = _read_meta(meta_path)
        meta["uploaded_episode_count"] = count
        _write_meta(meta_path, meta)
        logger.info("Wrote uploaded_episode_count=%d to %s", count, meta_path)


class SkillManager:
    """
    High-level API for the training client.

    Every public method is a **generator** that yields ``ProgressUpdate``
    objects.  The caller iterates to drive the workflow forward.
    """

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.client = OrchestratorClient(config)

    # ── Full lifecycle ──────────────────────────────────────────────

    def submit(
        self,
        source_dir: str | Path,
    ) -> Generator[ProgressUpdate, None, SkillInfo]:
        """
        Create a skill (or reuse one from metadata.json).

        Does **not** upload files — use ``upload_files`` for that.

        Yields progress updates throughout.  The final return value is
        the ``SkillInfo``.

        Parameters
        ----------
        source_dir
            Skill directory.  ``metadata.json`` will be updated here.
        """
        source_dir = Path(source_dir).resolve()
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {source_dir}")

        with _locked_metadata(source_dir) as meta_path:
            metadata = _read_meta(meta_path)
        skill_name = str(metadata.get("name") or "").strip() or source_dir.name

        # ── Create or reuse skill ───────────────────────────────────
        existing_id = read_skill_id(source_dir)
        if existing_id:
            yield ProgressUpdate(
                stage=ProgressStage.CREATING,
                message=f"Reusing existing skill {existing_id} from metadata.json",
                skill_id=existing_id,
            )
            try:
                skill = self.client.get_skill(existing_id)
            except APIError as e:
                if e.status_code in (403, 404):
                    logger.warning(
                        "Skill %s from metadata.json not accessible on server "
                        "(HTTP %d) — creating a new skill",
                        existing_id,
                        e.status_code,
                    )
                    skill = self.client.create_skill(name=skill_name)
                    write_skill_id(source_dir, skill.skill_id)
                    yield ProgressUpdate(
                        stage=ProgressStage.CREATING,
                        message=f"Replaced stale skill {existing_id} → {skill.skill_id}",
                        skill_id=skill.skill_id,
                    )
                else:
                    raise
        else:
            yield ProgressUpdate(
                stage=ProgressStage.CREATING,
                message="Creating skill on server…",
            )

            skill = self.client.create_skill(name=skill_name)
            write_skill_id(source_dir, skill.skill_id)

            yield ProgressUpdate(
                stage=ProgressStage.CREATING,
                message=f"Skill created: {skill.skill_id}",
                skill_id=skill.skill_id,
            )

        return skill

    # ── Re-upload files for an existing skill ───────────────────────

    def upload_files(
        self,
        skill_id: str,
        source_dir: str | Path,
    ) -> Generator[ProgressUpdate, None, None]:
        """
        Upload data files for an existing skill.

        This can be used to upload more data files to a skill that
        already exists (e.g. after a failed upload).
        """
        source_dir = Path(source_dir).resolve()
        if not source_dir.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {source_dir}")

        yield from convert_episodes_to_h264(source_dir / "data")

        filenames = _enumerate_files(source_dir)
        if not filenames:
            raise ValueError(f"No files found in {source_dir}")

        yield ProgressUpdate(
            stage=ProgressStage.UPLOADING,
            message=f"Uploading {len(filenames)} file(s) to skill {skill_id}",
            skill_id=skill_id,
        )

        batch_size = self.config.url_batch_size
        total_batches = (len(filenames) + batch_size - 1) // batch_size

        for batch_start in range(0, len(filenames), batch_size):
            batch = filenames[batch_start : batch_start + batch_size]
            batch_num = batch_start // batch_size + 1

            for attempt in range(1, _URL_REQUEST_MAX_RETRIES + 1):
                try:
                    url_resp = self.client.request_file_urls(skill_id, batch)
                    break
                except (ConnectionError, Timeout, OSError) as e:
                    if attempt == _URL_REQUEST_MAX_RETRIES:
                        raise
                    delay = _URL_REQUEST_BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "URL request batch %d/%d attempt %d/%d failed: %s "
                        "— retrying in %ds",
                        batch_num, total_batches,
                        attempt, _URL_REQUEST_MAX_RETRIES,
                        e, delay,
                    )
                    time.sleep(delay)

            yield from upload_data_files(
                client=self.client,
                source_dir=source_dir,
                filenames=batch,
                upload_urls=url_resp.get("upload_urls", {}),
                download_urls=url_resp.get("download_urls", {}),
                file_offset=batch_start,
                total_files=len(filenames),
            )

        yield ProgressUpdate(
            stage=ProgressStage.DONE,
            message=f"All files uploaded for skill {skill_id}",
            skill_id=skill_id,
        )

    # ── Poll run status ─────────────────────────────────────────────

    def watch(
        self,
        skill_id: str,
        run_id: int,
        *,
        interval: float | None = None,
    ) -> Generator[ProgressUpdate, None, RunInfo]:
        """
        Poll run status every *interval* seconds until terminal state.

        Yields a :class:`ProgressUpdate` on every poll.
        Returns the final :class:`RunInfo`.
        """
        interval = interval or self.config.poll_interval_seconds
        prev_status = None
        prev_daemon_state = None

        while True:
            run = self.client.get_run(skill_id, run_id)

            changed = run.status != prev_status or run.daemon_state != prev_daemon_state
            if changed or prev_status is None:
                state_str = run.status
                if run.daemon_state:
                    state_str += f" ({run.daemon_state})"
                if run.error_message:
                    state_str += f" — {run.error_message}"

                yield ProgressUpdate(
                    stage=ProgressStage.WAITING,
                    message=f"Run {skill_id}/{run_id}: {state_str}",
                    skill_id=skill_id,
                    run_id=run_id,
                )

                prev_status = run.status
                prev_daemon_state = run.daemon_state

            if run.is_terminal:
                return run

            time.sleep(interval)

    # ── Status snapshot ─────────────────────────────────────────────

    def run_status(self, skill_id: str, run_id: int) -> RunInfo:
        """Get current status of a run (single poll)."""
        return self.client.get_run(skill_id, run_id)

    def skill_info(self, skill_id: str) -> SkillInfo:
        """Get skill details."""
        return self.client.get_skill(skill_id)

    def list_skills(self) -> list[SkillInfo]:
        """List all skills for the authenticated user."""
        return self.client.list_skills()

    def list_runs(self, skill_id: str) -> list[RunInfo]:
        """List all runs for a skill."""
        return self.client.list_runs(skill_id)

    # ── Download results ────────────────────────────────────────────

    def download(
        self,
        skill_id: str,
        run_id: int,
        dest_dir: str | Path | None = None,
    ) -> Generator[ProgressUpdate, None, None]:
        """
        Download all result files for a completed run.

        If *dest_dir* is not given, downloads to the source_dir from
        the run's training_params.

        After download, marks the run as ``downloaded``.
        """
        run = self.client.get_run(skill_id, run_id)

        if dest_dir:
            dest = Path(dest_dir).resolve()
        elif run.source_dir:
            dest = Path(run.source_dir)
        else:
            raise ValueError(
                f"No dest_dir provided and run {skill_id}/{run_id} has no source_dir in params"
            )

        # Download into a run-specific subdirectory
        dest = dest / str(run_id)

        yield from download_results(
            client=self.client,
            skill_id=skill_id,
            run_id=run_id,
            dest_dir=dest,
        )

        # Mark as downloaded
        self.client.update_run(skill_id, run_id, status="downloaded")

        yield ProgressUpdate(
            stage=ProgressStage.DONE,
            message=f"Run {skill_id}/{run_id} results downloaded to {dest}",
            skill_id=skill_id,
            run_id=run_id,
        )

    # ── Activate ────────────────────────────────────────────────────

    def activate_run(
        self,
        skill_dir: str | Path,
        run_id: int,
    ) -> dict[str, str]:
        """
        Activate a trained run by setting checkpoint and stats_file in metadata.json.

        Looks inside ``skill_dir/run_id/`` for the largest ``*_step_*.pth``
        checkpoint and a ``*stats*.pt`` file, then writes them into
        ``metadata.json``'s ``execution`` block.

        Returns a dict with ``checkpoint`` and ``stats_file`` paths
        (relative to *skill_dir*).

        Raises ``FileNotFoundError`` if the run directory or required files
        are missing, or ``ValueError`` if metadata.json cannot be read.
        """
        skill_path = Path(skill_dir).resolve()
        run_dir = skill_path / str(run_id)

        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

        # Find checkpoint and stats
        checkpoint = _find_latest_checkpoint(run_dir)
        if not checkpoint:
            raise FileNotFoundError(
                f"No *_step_*.pth checkpoint file found in {run_dir}"
            )

        stats_file = _find_stats_file(run_dir)
        if not stats_file:
            raise FileNotFoundError(f"No *stats*.pt file found in {run_dir}")

        # Prefix with run_id subdir so paths are relative to skill_dir
        checkpoint = f"{run_id}/{checkpoint}"
        stats_file = f"{run_id}/{stats_file}"

        # Update metadata.json under lock
        with _locked_metadata(skill_path) as meta_path:
            if not meta_path.is_file():
                raise ValueError(f"No metadata.json found in {skill_path}")

            try:
                meta = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                raise ValueError(f"Failed to read metadata.json: {e}") from e

            # Ensure execution block exists
            if "execution" not in meta:
                meta["execution"] = {}

            meta["execution"]["checkpoint"] = checkpoint
            meta["execution"]["stats_file"] = stats_file

            _write_meta(meta_path, meta)
        logger.info(
            "Activated run %d: checkpoint=%s stats_file=%s",
            run_id,
            checkpoint,
            stats_file,
        )

        return {"checkpoint": checkpoint, "stats_file": stats_file}

    # ── Fetch input data ────────────────────────────────────────────

    def fetch_data(
        self,
        skill_id: str,
        dest_dir: str | Path,
    ) -> Generator[ProgressUpdate, None, None]:
        """Download the input training data files for a skill.

        Files are saved into *dest_dir*.
        Yields :class:`ProgressUpdate` for each file downloaded.
        """
        yield from download_skill_data(
            client=self.client,
            skill_id=skill_id,
            dest_dir=Path(dest_dir),
        )



# ── Helpers ─────────────────────────────────────────────────────────


def _enumerate_files(source_dir: Path) -> list[str]:
    """
    List uploadable files in *source_dir*: root-level files and
    everything under ``data/``.  Skips hidden dirs and __pycache__.
    """
    files: list[str] = []

    def _should_skip(rel: Path) -> bool:
        if any(p.startswith(".") or p == "__pycache__" for p in rel.parts):
            return True
        if rel.name in (SKILL_JSON, METADATA_JSON):
            return True
        return False

    # Root-level files only (no recursion)
    for path in sorted(source_dir.iterdir()):
        if not path.is_file():
            continue
        rel = path.relative_to(source_dir)
        if _should_skip(rel):
            continue
        files.append(str(rel))

    # Everything under data/
    data_dir = source_dir / "data"
    if data_dir.is_dir():
        for path in sorted(data_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(source_dir)
            if _should_skip(rel):
                continue
            files.append(str(rel))

    return files


def _find_latest_checkpoint(run_dir: Path) -> str | None:
    """Find the checkpoint file with the largest step number in *run_dir*."""
    pattern = re.compile(r"_step_(\d+)\.pth$")
    best_step = -1
    best_file: str | None = None
    for pth in run_dir.rglob("*_step_*.pth"):
        m = pattern.search(pth.name)
        if m:
            step = int(m.group(1))
            if step > best_step:
                best_step = step
                best_file = str(pth.relative_to(run_dir))
    return best_file


def _find_stats_file(run_dir: Path) -> str | None:
    """Find a dataset stats file in *run_dir*."""
    for pt in run_dir.rglob("*stats*.pt"):
        return str(pt.relative_to(run_dir))
    return None
