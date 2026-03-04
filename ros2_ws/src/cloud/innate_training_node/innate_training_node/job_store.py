"""Thread-safe shared state for training jobs and active transfers."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from builtin_interfaces.msg import Time as RosTime

from innate_cloud_msgs.msg import (
    TrainingRunStatus,
    TrainingSkillStatus,
    TransferProgress,
)

from training_client.src.types import (
    ProgressStage,
    ProgressUpdate,
    RunInfo,
    SkillInfo,
)

# ── Enum maps ───────────────────────────────────────────────────────

_STATUS_MAP: dict[str, int] = {
    "waiting_for_approval": TrainingRunStatus.STATUS_WAITING_FOR_APPROVAL,
    "approved": TrainingRunStatus.STATUS_APPROVED,
    "rejected": TrainingRunStatus.STATUS_REJECTED,
    "booting": TrainingRunStatus.STATUS_BOOTING,
    "running": TrainingRunStatus.STATUS_RUNNING,
    "done": TrainingRunStatus.STATUS_DONE,
    "downloaded": TrainingRunStatus.STATUS_DOWNLOADED,
}

_STAGE_MAP: dict[str, int] = {
    ProgressStage.COMPRESSING.value: TransferProgress.STAGE_COMPRESSING,
    ProgressStage.UPLOADING.value: TransferProgress.STAGE_UPLOADING,
    ProgressStage.DOWNLOADING.value: TransferProgress.STAGE_DOWNLOADING,
    ProgressStage.VERIFYING.value: TransferProgress.STAGE_VERIFYING,
    ProgressStage.DONE.value: TransferProgress.STAGE_DONE,
    ProgressStage.ERROR.value: TransferProgress.STAGE_ERROR,
}


def parse_iso_to_ros(iso: str | None) -> RosTime:
    """Parse an ISO-8601 timestamp to ``builtin_interfaces/Time``.

    Returns a zero-valued ``Time`` when *iso* is falsy or unparsable.
    """
    if not iso:
        return RosTime()
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        epoch = dt.timestamp()
        sec = int(epoch)
        nanosec = int((epoch - sec) * 1e9)
        return RosTime(sec=sec, nanosec=nanosec)
    except (ValueError, OSError):
        return RosTime()


# ── Store ───────────────────────────────────────────────────────────


class JobStore:
    """Thread-safe cache of jobs, skills, and in-flight transfers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, int], RunInfo] = {}
        self._skills: dict[str, SkillInfo] = {}
        # Keyed by (direction, skill_id, run_id).
        self._transfers: dict[tuple[int, str, int], TransferProgress] = {}
        # Tracks transfers that finished successfully (for persistent ✓ in UI).
        self._completed_transfers: set[tuple[int, str, int]] = set()
        self._download_initiated: set[tuple[str, int]] = set()
        self._dir_map: dict[str, str] = {}  # skill_id → skill_dir

    # ── Jobs ────────────────────────────────────────────────────────

    def put_job(self, run: RunInfo) -> None:
        with self._lock:
            self._jobs[(run.skill_id, run.run_id)] = run

    def get_job(self, skill_id: str, run_id: int) -> RunInfo | None:
        with self._lock:
            return self._jobs.get((skill_id, run_id))

    def active_jobs(self) -> list[tuple[tuple[str, int], RunInfo]]:
        with self._lock:
            return [(k, v) for k, v in self._jobs.items() if not v.is_terminal]

    # ── Skills ──────────────────────────────────────────────────────

    def put_skill(self, skill: SkillInfo) -> None:
        with self._lock:
            self._skills[skill.skill_id] = skill

    def get_skill(self, skill_id: str) -> SkillInfo | None:
        with self._lock:
            return self._skills.get(skill_id)

    # ── Downloads tracking ──────────────────────────────────────────

    def mark_download_started(self, skill_id: str, run_id: int) -> bool:
        key = (skill_id, run_id)
        with self._lock:
            if key in self._download_initiated:
                return False
            self._download_initiated.add(key)
            return True

    def unmark_download(self, skill_id: str, run_id: int) -> None:
        with self._lock:
            self._download_initiated.discard((skill_id, run_id))

    # ── Directory map ─────────────────────────────────────────────

    def register_dir(self, skill_id: str, skill_dir: str) -> None:
        """Record the on-disk directory for a skill."""
        with self._lock:
            self._dir_map[skill_id] = skill_dir

    def dir_for_skill(self, skill_id: str) -> str | None:
        """Return the known directory for *skill_id*, or ``None``."""
        with self._lock:
            return self._dir_map.get(skill_id)

    # ── Transfers ───────────────────────────────────────────────────

    def update_transfer(
        self, direction: int, skill_id: str, run_id: int, update: ProgressUpdate
    ) -> None:
        key = (direction, skill_id, run_id)
        finished = update.stage in (ProgressStage.DONE, ProgressStage.ERROR)
        with self._lock:
            if finished:
                self._transfers.pop(key, None)
                if update.stage == ProgressStage.DONE:
                    self._completed_transfers.add(key)
            else:
                self._transfers[key] = _progress_to_transfer(
                    direction, skill_id, run_id, update
                )

    def mark_upload_pending(self, skill_id: str) -> None:
        """Insert a placeholder upload transfer so the very next publish
        cycle reports ``has_active_transfer=true`` before the upload thread
        has sent its first real progress update."""
        key = (TransferProgress.UPLOAD, skill_id, -1)
        placeholder = TransferProgress()
        placeholder.direction = TransferProgress.UPLOAD
        placeholder.stage = TransferProgress.STAGE_COMPRESSING
        placeholder.message = "Preparing upload…"
        placeholder.transfer_done = False
        with self._lock:
            # Only set if there isn't already a real transfer in-flight.
            if key not in self._transfers:
                self._transfers[key] = placeholder
            # Clear any stale "completed" marker from a previous upload.
            self._completed_transfers.discard(key)

    # ── Snapshot for publish ────────────────────────────────────────

    def snapshot(
        self,
    ) -> tuple[
        list[RunInfo],
        dict[str, SkillInfo],
        dict[tuple[int, str, int], TransferProgress],
        set[tuple[int, str, int]],
        dict[str, str],
    ]:
        """Atomically grab everything needed for one publish cycle."""
        with self._lock:
            return (
                list(self._jobs.values()),
                dict(self._skills),
                dict(self._transfers),
                set(self._completed_transfers),
                dict(self._dir_map),
            )


# ── Message builders ────────────────────────────────────────────────


def build_run_status(
    run: RunInfo,
    transfer: TransferProgress | None,
    transfer_done: bool,
) -> TrainingRunStatus:
    """Build a ``TrainingRunStatus`` msg from a ``RunInfo``."""
    s = TrainingRunStatus()
    s.run_id = run.run_id
    s.status = _STATUS_MAP.get(run.status, TrainingRunStatus.STATUS_UNKNOWN)
    s.daemon_state = run.daemon_state or ""
    s.error_message = run.error_message or ""
    s.started_at = parse_iso_to_ros(run.started_at)
    s.finished_at = parse_iso_to_ros(run.finished_at)
    s.instance_type = run.instance_type or ""
    s.transfer_done = transfer_done
    if transfer is not None:
        s.has_active_transfer = True
        s.active_transfer = transfer
    return s


def build_skill_status(
    skill_id: str,
    skill: SkillInfo | None,
    runs: list[RunInfo],
    upload_transfer: TransferProgress | None,
    upload_done: bool,
    download_transfers: dict[int, TransferProgress],
    downloads_done: set[int],
    skill_dir: str = "",
) -> TrainingSkillStatus:
    """Build a ``TrainingSkillStatus`` msg (skill + nested runs)."""
    s = TrainingSkillStatus()
    s.training_skill_id = skill_id
    s.skill_name = skill.name if skill else ""
    s.skill_dir = skill_dir
    s.transfer_done = upload_done
    if upload_transfer is not None:
        s.has_active_transfer = True
        s.active_transfer = upload_transfer
    s.runs = [
        build_run_status(
            r,
            download_transfers.get(r.run_id),
            r.run_id in downloads_done,
        )
        for r in runs
    ]
    return s


def _progress_to_transfer(
    direction: int, skill_id: str, run_id: int, update: ProgressUpdate
) -> TransferProgress:
    msg = TransferProgress()
    msg.direction = direction
    msg.stage = _STAGE_MAP.get(update.stage.value, TransferProgress.STAGE_ERROR)
    msg.message = update.message

    fp = update.file_progress
    if fp is not None:
        msg.filename = fp.filename
        msg.file_index = fp.index
        msg.file_total = fp.total
        msg.bytes_done = fp.bytes_done
        msg.bytes_total = fp.bytes_total
        msg.file_done = fp.done
        msg.error = fp.error or ""

    msg.transfer_done = update.stage in (ProgressStage.DONE, ProgressStage.ERROR)
    if update.error:
        msg.error = update.error
    return msg
