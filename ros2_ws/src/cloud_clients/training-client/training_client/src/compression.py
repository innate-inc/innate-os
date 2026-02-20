"""
Atomic zstd compression with crash-recovery semantics.

Strategy:
  source.bag  →  source.bag.zst.tmp   (compressing…)
               →  source.bag.zst       (atomic rename on success)

On recovery:
  .zst.tmp exists  →  delete, re-compress
  .zst exists      →  already done, skip
  neither          →  compress from scratch
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class CompressionError(Exception):
    """Raised when zstd compression fails."""


def _detect_threads(configured: int) -> int:
    """If *configured* is 0, auto-detect via nproc."""
    if configured > 0:
        return configured
    try:
        result = subprocess.run(["nproc"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            n = int(result.stdout.strip())
            logger.info("Auto-detected %d cores for compression", n)
            return n
    except Exception:
        pass
    return 4  # safe fallback


def compress_file(
    source: Path,
    *,
    level: int = 10,
    threads: int = 0,
) -> Path:
    """
    Compress *source* → ``source.zst`` atomically.

    Returns the path to the compressed file.  Idempotent — if the ``.zst``
    file already exists (and no ``.zst.tmp``), returns immediately.
    """
    zst_path = source.with_suffix(source.suffix + ".zst")
    tmp_path = source.with_suffix(source.suffix + ".zst.tmp")

    # Recovery: stale tmp means previous attempt died mid-compress
    if tmp_path.exists():
        logger.info("Removing stale tmp file %s (previous crash?)", tmp_path)
        tmp_path.unlink()

    # Already compressed — skip
    if zst_path.exists():
        logger.debug("Already compressed: %s", zst_path)
        return zst_path

    if not source.exists():
        raise CompressionError(f"Source file does not exist: {source}")

    real_threads = _detect_threads(threads)

    cmd = [
        "zstd",
        f"-T{real_threads // 2}",
        f"-{level}",
        "-f",
        str(source),
        "-o",
        str(tmp_path),
    ]
    logger.info("Compressing %s → %s", source, tmp_path)

    try:
        result = subprocess.run(
            cmd, check=True, capture_output=True, text=True, timeout=3600
        )
    except subprocess.CalledProcessError as e:
        tmp_path.unlink(missing_ok=True)
        raise CompressionError(f"zstd failed: {e.stderr}") from e
    except subprocess.TimeoutExpired:
        tmp_path.unlink(missing_ok=True)
        raise CompressionError("zstd compression timed out (1h)")

    # Atomic rename: if we crash between here and the rename, recovery
    # will see the .tmp and re-compress.
    os.rename(tmp_path, zst_path)
    logger.info(
        "Compressed %s (%.1f MB → %.1f MB)",
        source.name,
        source.stat().st_size / 1e6,
        zst_path.stat().st_size / 1e6,
    )
    return zst_path


def decompress_file(source: Path, dest: Path | None = None) -> Path:
    """Decompress a ``.zst`` file. Returns path to decompressed output."""
    if dest is None:
        if source.suffix != ".zst":
            raise CompressionError(f"Cannot infer output for {source}")
        dest = source.with_suffix("")

    cmd = ["zstd", "-d", "-f", str(source), "-o", str(dest)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
    except subprocess.CalledProcessError as e:
        raise CompressionError(f"zstd decompress failed: {e.stderr}") from e

    return dest


def cleanup_compressed(source_dir: Path, filenames: list[str]) -> None:
    """Remove .zst files for the given source filenames."""
    for name in filenames:
        zst = (source_dir / name).with_suffix((source_dir / name).suffix + ".zst")
        zst.unlink(missing_ok=True)
        tmp = (source_dir / name).with_suffix((source_dir / name).suffix + ".zst.tmp")
        tmp.unlink(missing_ok=True)
