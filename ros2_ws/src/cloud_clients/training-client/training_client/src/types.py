"""Dataclasses for training client configuration, progress, and state."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

# ── Configuration ───────────────────────────────────────────────────

DEFAULT_SERVER_URL = "https://training-v1.innate.bot"
DEFAULT_AUTH_ISSUER_URL = "https://auth-v1.innate.bot"


@dataclass
class ClientConfig:
    """All configuration needed to talk to the orchestrator."""

    server_url: str = DEFAULT_SERVER_URL
    auth_token: str = ""

    # OIDC auth — auth_token is treated as a service key and exchanged
    # for a JWT via the issuer's token endpoint.  Set to "" to disable
    # OIDC and use auth_token as a plain bearer token (development only).
    auth_issuer_url: str = DEFAULT_AUTH_ISSUER_URL

    # Compression
    zstd_compression_level: int = 9
    zstd_threads: int = 0  # 0 = auto-detect via nproc

    # Polling
    poll_interval_seconds: float = 20.0

    # HTTP
    request_timeout_seconds: int = 30
    upload_timeout_seconds: int = 600
    download_timeout_seconds: int = 600
    upload_chunk_size: int = 8 * 1024 * 1024  # 8 MiB


# ── Progress / Status ──────────────────────────────────────────────


class ProgressStage(str, enum.Enum):
    """Stages of the client-side lifecycle."""

    CREATING = "creating"
    COMPRESSING = "compressing"
    UPLOADING = "uploading"
    VERIFYING = "verifying"
    SUBMITTED = "submitted"
    WAITING = "waiting"  # polling remote status
    DOWNLOADING = "downloading"
    DONE = "done"
    ERROR = "error"


@dataclass
class FileProgress:
    """Progress for a single file operation."""

    filename: str
    index: int  # 1-based position of this file in the batch (e.g. 3 in "[3/5]")
    total: int  # total number of files in the batch
    bytes_done: int = 0  # bytes transferred so far for this file
    bytes_total: int = 0  # total size of this file in bytes
    done: bool = False
    error: str | None = None


@dataclass
class ProgressUpdate:
    """Yielded by skill_manager generators to report progress."""

    stage: ProgressStage
    message: str
    file_progress: FileProgress | None = None
    skill_id: str | None = None
    run_id: int | None = None
    error: str | None = None


@dataclass
class SkillInfo:
    """Snapshot of a skill."""

    skill_id: str
    user_id: str
    name: str
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SkillInfo:
        return cls(
            skill_id=data["skill_id"],
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


@dataclass
class RunInfo:
    """Snapshot of a run (one execution of a skill)."""

    skill_id: str
    run_id: int
    status: str
    daemon_state: str | None = None
    training_params: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    approved_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    instance_ip: str | None = None
    instance_type: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> RunInfo:
        return cls(
            skill_id=data["skill_id"],
            run_id=data["run_id"],
            status=data["status"],
            daemon_state=data.get("daemon_state"),
            training_params=data.get("training_params") or {},
            error_message=data.get("error_message"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            approved_at=data.get("approved_at"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            instance_ip=data.get("instance_ip"),
            instance_type=data.get("instance_type"),
        )

    @property
    def source_dir(self) -> str | None:
        """Retrieve the source_dir stashed in training_params."""
        return self.training_params.get("source_dir")

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "rejected", "downloaded")
