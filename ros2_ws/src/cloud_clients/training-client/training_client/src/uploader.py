"""
Per-file compress → upload pipeline with crash-recovery tracking.

Compression and upload are **pipelined**: while file N is uploading,
file N+1 is compressed in a background thread.

Each file goes through:
  1. compress (atomic .zst.tmp → .zst)
  2. upload to signed PUT URL
  3. verify via HEAD on signed download URL
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Generator

from .client import OrchestratorClient
from .compression import compress_file
from .types import (
    ClientConfig,
    FileProgress,
    ProgressStage,
    ProgressUpdate,
)

logger = logging.getLogger(__name__)


def _compress_one(
    source_path: Path,
    config: ClientConfig,
) -> Path:
    """Compress a single file (runs in a background thread)."""
    return compress_file(
        source_path,
        level=config.zstd_compression_level,
        threads=config.zstd_threads,
    )


def upload_data_files(
    *,
    client: OrchestratorClient,
    config: ClientConfig,
    source_dir: Path,
    filenames: list[str],
    upload_urls: dict[str, str],
    download_urls: dict[str, str],
) -> Generator[ProgressUpdate, None, None]:
    """
    Compress and upload each file in *filenames*.

    Compression of the next file runs in a background thread while the
    current file is uploading, so the two I/O-bound stages overlap.

    Yields a :class:`ProgressUpdate` after each file is compressed, uploaded,
    and verified.  Skips files that are already uploaded (verified by HEAD).
    """
    total = len(filenames)

    # Pre-filter to find files that actually need work
    work_items: list[tuple[int, str]] = []  # (1-based idx, filename)
    for idx, name in enumerate(filenames, start=1):
        source_path = source_dir / name
        zst_name = name + ".zst"
        download_url = download_urls.get(zst_name) or download_urls.get(name)
        if download_url and _is_already_uploaded(
            client, source_path, download_url, config
        ):
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
        # Still fall through to verification below

    # ── Pipelined compress + upload ─────────────────────────────────
    # Use a single-thread pool to compress the *next* file while the
    # current one is uploading.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="compress") as pool:
        # Kick off compression of the first file immediately
        pending_future: Future[Path] | None = None
        pending_idx: int = 0
        pending_name: str = ""

        if work_items:
            first_idx, first_name = work_items[0]
            pending_idx, pending_name = first_idx, first_name
            yield ProgressUpdate(
                stage=ProgressStage.COMPRESSING,
                message=f"[{first_idx}/{total}] Compressing {first_name}…",
                file_progress=FileProgress(
                    filename=first_name, index=first_idx, total=total
                ),
            )
            pending_future = pool.submit(_compress_one, source_dir / first_name, config)

        for wi_pos, (idx, name) in enumerate(work_items):
            # ── Wait for this file's compression to finish ──────────
            assert pending_future is not None
            try:
                zst_path = pending_future.result()
            except Exception as e:
                yield ProgressUpdate(
                    stage=ProgressStage.ERROR,
                    message=f"[{idx}/{total}] Compression failed for {name}: {e}",
                    file_progress=FileProgress(
                        filename=name,
                        index=idx,
                        total=total,
                        error=str(e),
                    ),
                    error=str(e),
                )
                raise

            # ── Start compressing the *next* file in background ─────
            next_pos = wi_pos + 1
            if next_pos < len(work_items):
                next_idx, next_name = work_items[next_pos]
                yield ProgressUpdate(
                    stage=ProgressStage.COMPRESSING,
                    message=f"[{next_idx}/{total}] Compressing {next_name}…",
                    file_progress=FileProgress(
                        filename=next_name, index=next_idx, total=total
                    ),
                )
                pending_future = pool.submit(
                    _compress_one, source_dir / next_name, config
                )
                pending_idx, pending_name = next_idx, next_name
            else:
                pending_future = None

            # ── Upload the just-compressed file ─────────────────────
            zst_name = name + ".zst"
            upload_url = upload_urls.get(zst_name) or upload_urls.get(name)
            if not upload_url:
                err = f"No upload URL for {name} (or {zst_name})"
                yield ProgressUpdate(
                    stage=ProgressStage.ERROR,
                    message=err,
                    error=err,
                )
                raise RuntimeError(err)

            zst_size = zst_path.stat().st_size

            yield ProgressUpdate(
                stage=ProgressStage.UPLOADING,
                message=f"[{idx}/{total}] Uploading {name} ({zst_size / 1e6:.1f} MB)…",
                file_progress=FileProgress(
                    filename=name,
                    index=idx,
                    total=total,
                    bytes_total=zst_size,
                ),
            )

            try:
                for fname, sent, total_bytes in client.upload_to_signed_url(
                    upload_url,
                    str(zst_path),
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
                    bytes_done=zst_size,
                    bytes_total=zst_size,
                    done=True,
                ),
            )

    # ── 4. Verify all uploads ───────────────────────────────────────
    yield ProgressUpdate(
        stage=ProgressStage.VERIFYING,
        message="Verifying all uploads…",
    )

    for idx, name in enumerate(filenames, start=1):
        zst_name = name + ".zst"
        zst_path = (source_dir / name).with_suffix((source_dir / name).suffix + ".zst")
        download_url = download_urls.get(zst_name) or download_urls.get(name)

        if not download_url:
            logger.warning("No download URL for %s, cannot verify", name)
            continue

        if not _is_already_uploaded(client, source_dir / name, download_url, config):
            err = f"Verification failed for {name}: remote size mismatch"
            yield ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=err,
                error=err,
            )
            raise RuntimeError(err)

    yield ProgressUpdate(
        stage=ProgressStage.VERIFYING,
        message=f"All {total} files verified",
    )


def _is_already_uploaded(
    client: OrchestratorClient,
    source_path: Path,
    download_url: str,
    config: ClientConfig,
) -> bool:
    """
    Check if a file is already uploaded by comparing remote Content-Length
    to the local .zst file size.
    """
    zst_path = source_path.with_suffix(source_path.suffix + ".zst")
    if not zst_path.exists():
        return False

    local_size = zst_path.stat().st_size
    remote_size = client.head_signed_url(download_url)

    if remote_size is None:
        return False

    return remote_size == local_size
