"""
Per-file upload pipeline with crash-recovery tracking.

Each file goes through:
  1. upload to signed PUT URL
  2. verify via HEAD on signed download URL
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Generator

from .client import OrchestratorClient
from .types import (
    FileProgress,
    ProgressStage,
    ProgressUpdate,
)

logger = logging.getLogger(__name__)


def upload_data_files(
    *,
    client: OrchestratorClient,
    source_dir: Path,
    filenames: list[str],
    upload_urls: dict[str, str],
    download_urls: dict[str, str],
    file_offset: int = 0,
    total_files: int | None = None,
) -> Generator[ProgressUpdate, None, None]:
    """
    Upload each file in *filenames* directly to signed URLs.

    Yields a :class:`ProgressUpdate` after each file is uploaded and verified.
    Skips files that are already uploaded (verified by HEAD).

    *file_offset* and *total_files* allow callers to supply correct global
    indices when uploading in batches (e.g. "[53/1500]" instead of "[3/50]").
    """
    total = total_files if total_files is not None else len(filenames)

    work_items: list[tuple[int, str]] = []
    for idx, name in enumerate(filenames, start=file_offset + 1):
        source_path = source_dir / name
        download_url = download_urls.get(name)
        if download_url and _is_already_uploaded(client, source_path, download_url):
            yield ProgressUpdate(
                stage=ProgressStage.UPLOADING,
                message=f"[{idx}/{total}] {name} already uploaded, skipping",
                file_progress=FileProgress(
                    filename=name,
                    index=idx,
                    total=total,
                    done=True,
                ),
            )
        else:
            work_items.append((idx, name))

    if not work_items:
        yield ProgressUpdate(
            stage=ProgressStage.VERIFYING,
            message="All files already uploaded",
        )

    for idx, name in work_items:
        source_path = source_dir / name
        upload_url = upload_urls.get(name)
        if not upload_url:
            err = f"No upload URL for {name}"
            yield ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=err,
                error=err,
            )
            raise RuntimeError(err)

        file_size = source_path.stat().st_size

        yield ProgressUpdate(
            stage=ProgressStage.UPLOADING,
            message=f"[{idx}/{total}] Uploading {name} ({file_size / 1e6:.1f} MB)…",
            file_progress=FileProgress(
                filename=name,
                index=idx,
                total=total,
                bytes_total=file_size,
            ),
        )

        try:
            for fname, sent, total_bytes in client.upload_to_signed_url(
                upload_url,
                str(source_path),
            ):
                yield ProgressUpdate(
                    stage=ProgressStage.UPLOADING,
                    message=(
                        f"[{idx}/{total}] Uploading {fname}: "
                        f"{sent / 1e6:.1f}/{total_bytes / 1e6:.1f} MB"
                    ),
                    file_progress=FileProgress(
                        filename=name,
                        index=idx,
                        total=total,
                        bytes_done=sent,
                        bytes_total=total_bytes,
                    ),
                )
        except Exception as e:
            yield ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=f"[{idx}/{total}] Upload failed for {name}: {e}",
                file_progress=FileProgress(
                    filename=name,
                    index=idx,
                    total=total,
                    error=str(e),
                ),
                error=str(e),
            )
            raise

        yield ProgressUpdate(
            stage=ProgressStage.UPLOADING,
            message=f"[{idx}/{total}] Uploaded {name}",
            file_progress=FileProgress(
                filename=name,
                index=idx,
                total=total,
                bytes_done=file_size,
                bytes_total=file_size,
                done=True,
            ),
        )

    # ── Verify all uploads ───────────────────────────────────────
    yield ProgressUpdate(
        stage=ProgressStage.VERIFYING,
        message="Verifying all uploads…",
    )

    for idx, name in enumerate(filenames, start=file_offset + 1):
        download_url = download_urls.get(name)

        if not download_url:
            logger.warning("No download URL for %s, cannot verify", name)
            continue

        if not _is_already_uploaded(client, source_dir / name, download_url):
            err = f"Verification failed for {name}: remote size mismatch"
            yield ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=err,
                error=err,
            )
            raise RuntimeError(err)

    yield ProgressUpdate(
        stage=ProgressStage.VERIFYING,
        message=f"All {len(filenames)} files verified",
    )


def _is_already_uploaded(
    client: OrchestratorClient,
    source_path: Path,
    download_url: str,
) -> bool:
    """
    Check if a file is already uploaded by comparing remote Content-Length
    to the local file size.
    """
    if not source_path.exists():
        return False

    local_size = source_path.stat().st_size
    remote_size = client.head_signed_url(download_url)

    if remote_size is None:
        return False

    return remote_size == local_size
