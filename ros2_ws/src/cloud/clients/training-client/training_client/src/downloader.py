"""
Download run results (outputs/checkpoints) and skill input data.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Generator

from .client import OrchestratorClient
from .compression import decompress_file
from .types import (
    FileProgress,
    ProgressStage,
    ProgressUpdate,
)

logger = logging.getLogger(__name__)


def download_files(
    *,
    client: OrchestratorClient,
    files: dict[str, str],
    dest_dir: Path,
    skill_id: str | None = None,
    run_id: int | None = None,
) -> Generator[ProgressUpdate, None, None]:
    """Download a ``{filename: signed_url}`` mapping into *dest_dir*.

    Shared by :func:`download_results` (run outputs) and skill input data.
    ``.zst`` files are auto-decompressed after download.
    """
    total = len(files)
    dest_dir.mkdir(parents=True, exist_ok=True)

    for idx, (filename, signed_url) in enumerate(files.items(), start=1):
        local_path = dest_dir / filename

        yield ProgressUpdate(
            stage=ProgressStage.DOWNLOADING,
            message=f"[{idx}/{total}] Downloading {filename}…",
            file_progress=FileProgress(
                filename=filename,
                index=idx,
                total=total,
            ),
            skill_id=skill_id,
            run_id=run_id,
        )

        try:
            for fname, received, file_total in client.download_signed_url(
                signed_url,
                str(local_path),
            ):
                yield ProgressUpdate(
                    stage=ProgressStage.DOWNLOADING,
                    message=(
                        f"[{idx}/{total}] Downloading {fname}: "
                        f"{received / 1e6:.1f}/{file_total / 1e6:.1f} MB"
                    ),
                    file_progress=FileProgress(
                        filename=filename,
                        index=idx,
                        total=total,
                        bytes_done=received,
                        bytes_total=file_total,
                    ),
                    skill_id=skill_id,
                    run_id=run_id,
                )

            # Auto-decompress .zst files
            if filename.endswith(".zst"):
                decompressed = decompress_file(local_path)
                local_path.unlink(missing_ok=True)
                logger.info("Decompressed %s → %s", filename, decompressed.name)

            yield ProgressUpdate(
                stage=ProgressStage.DOWNLOADING,
                message=f"[{idx}/{total}] Downloaded {filename}",
                file_progress=FileProgress(
                    filename=filename,
                    index=idx,
                    total=total,
                    done=True,
                ),
                skill_id=skill_id,
                run_id=run_id,
            )
        except Exception as e:
            yield ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=f"[{idx}/{total}] Download failed for {filename}: {e}",
                file_progress=FileProgress(
                    filename=filename,
                    index=idx,
                    total=total,
                    error=str(e),
                ),
                skill_id=skill_id,
                run_id=run_id,
                error=str(e),
            )
            raise

    yield ProgressUpdate(
        stage=ProgressStage.DOWNLOADING,
        message=f"All {total} file(s) downloaded to {dest_dir}",
        skill_id=skill_id,
        run_id=run_id,
    )


def download_results(
    *,
    client: OrchestratorClient,
    skill_id: str,
    run_id: int,
    dest_dir: Path,
) -> Generator[ProgressUpdate, None, None]:
    """List and download all output files for a run into *dest_dir*."""
    yield ProgressUpdate(
        stage=ProgressStage.DOWNLOADING,
        message=f"Listing result files for run {skill_id}/{run_id}…",
        skill_id=skill_id,
        run_id=run_id,
    )

    files = client.list_run_files(skill_id, run_id)

    if not files:
        yield ProgressUpdate(
            stage=ProgressStage.DOWNLOADING,
            message="No result files found",
            skill_id=skill_id,
            run_id=run_id,
        )
        return

    yield ProgressUpdate(
        stage=ProgressStage.DOWNLOADING,
        message=f"Found {len(files)} result file(s) to download",
        skill_id=skill_id,
        run_id=run_id,
    )

    yield from download_files(
        client=client,
        files=files,
        dest_dir=dest_dir,
        skill_id=skill_id,
        run_id=run_id,
    )


def download_skill_data(
    *,
    client: OrchestratorClient,
    skill_id: str,
    dest_dir: Path,
) -> Generator[ProgressUpdate, None, None]:
    """List and download all input data files for a skill into *dest_dir*."""
    dest_dir = dest_dir.resolve()

    yield ProgressUpdate(
        stage=ProgressStage.DOWNLOADING,
        message=f"Listing input data files for skill {skill_id}…",
        skill_id=skill_id,
    )

    files = client.list_skill_files(skill_id)

    if not files:
        yield ProgressUpdate(
            stage=ProgressStage.DOWNLOADING,
            message="No input data files found for this skill.",
            skill_id=skill_id,
        )
        yield ProgressUpdate(
            stage=ProgressStage.DONE,
            message=f"Skill {skill_id} input data downloaded to {dest_dir}",
            skill_id=skill_id,
        )
        return

    yield ProgressUpdate(
        stage=ProgressStage.DOWNLOADING,
        message=f"Found {len(files)} input data file(s) to download",
        skill_id=skill_id,
    )

    yield from download_files(
        client=client,
        files=files,
        dest_dir=dest_dir,
        skill_id=skill_id,
    )

    yield ProgressUpdate(
        stage=ProgressStage.DONE,
        message=f"Skill {skill_id} input data downloaded to {dest_dir}",
        skill_id=skill_id,
    )
