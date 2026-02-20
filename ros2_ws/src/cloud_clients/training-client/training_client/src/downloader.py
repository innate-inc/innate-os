"""
Download run results (outputs/checkpoints).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Generator

from .client import OrchestratorClient
from .compression import decompress_file
from .types import (
    ClientConfig,
    FileProgress,
    ProgressStage,
    ProgressUpdate,
)

logger = logging.getLogger(__name__)


def download_results(
    *,
    client: OrchestratorClient,
    config: ClientConfig,
    skill_id: str,
    run_id: int,
    dest_dir: Path,
) -> Generator[ProgressUpdate, None, None]:
    """
    List and download all output files for a run.

    Files are saved into *dest_dir*.  ``.zst`` files are auto-decompressed.

    Yields :class:`ProgressUpdate` for each file downloaded.
    """
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

    total = len(files)
    yield ProgressUpdate(
        stage=ProgressStage.DOWNLOADING,
        message=f"Found {total} result file(s) to download",
        skill_id=skill_id,
        run_id=run_id,
    )

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
        message=f"All {total} result file(s) downloaded to {dest_dir}",
        skill_id=skill_id,
        run_id=run_id,
    )
