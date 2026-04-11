"""
Convert HDF5 episodes from raw-image format to H.264 MP4 + stripped HDF5.

For each episode_N.h5 in the data directory:
  1. Move the original to raw_data/ (sibling of data/)
  2. Encode each camera's image stack to an MP4 via GStreamer x264enc
  3. Write a new episode_N.h5 without /observations/images/
  4. Update dataset_metadata.json (dataset_type h5 -> h264)

The conversion is incremental: episodes whose MP4s already exist on disk
are skipped.  New episodes added after a prior conversion are detected and
converted.  Partially-completed conversions are detected by the presence
of files in raw_data/ and resumed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Generator

import h5py
import numpy as np

from .types import FileProgress, ProgressStage, ProgressUpdate

logger = logging.getLogger(__name__)

DATASET_METADATA = "dataset_metadata.json"
RAW_DATA_DIR = "raw_data"


def convert_episodes_to_h264(
    data_dir: Path,
) -> Generator[ProgressUpdate, None, None]:
    """Convert all episodes under *data_dir* from raw-image HDF5 to H.264.

    Yields :class:`ProgressUpdate` with ``stage=COMPRESSING`` matching the
    existing upload progress pattern.
    """
    meta_path = data_dir / DATASET_METADATA
    if not meta_path.is_file():
        logger.debug("No %s in %s — skipping H.264 conversion", DATASET_METADATA, data_dir)
        return

    meta = json.loads(meta_path.read_text())

    fps = int(meta.get("data_frequency", 30))
    episodes = meta.get("episodes", [])

    if not episodes:
        return

    raw_dir = data_dir.parent / RAW_DATA_DIR
    raw_dir.mkdir(exist_ok=True)

    work_items = _build_work_list(data_dir, raw_dir, episodes)
    if not work_items:
        logger.debug("No episodes need H.264 conversion")
        return

    total = len(work_items)

    for idx, item in enumerate(work_items, start=1):
        if item["kind"] == "mp4":
            h5_path = data_dir / item["h5_file"]
            raw_path = raw_dir / item["h5_file"]
            mp4_path = data_dir / item["filename"]
            cam_name = item["camera"]

            if mp4_path.exists():
                yield ProgressUpdate(
                    stage=ProgressStage.COMPRESSING,
                    message=f"[{idx}/{total}] {item['filename']} already exists, skipping",
                    file_progress=FileProgress(
                        filename=item["filename"], index=idx, total=total, done=True
                    ),
                )
                continue

            if not raw_path.exists():
                shutil.move(str(h5_path), str(raw_path))
                logger.info("Moved %s -> raw_data/%s", h5_path.name, raw_path.name)

            yield ProgressUpdate(
                stage=ProgressStage.COMPRESSING,
                message=f"[{idx}/{total}] Encoding {item['filename']}…",
                file_progress=FileProgress(
                    filename=item["filename"], index=idx, total=total
                ),
            )

            try:
                _encode_camera_to_mp4(raw_path, cam_name, mp4_path, fps)
            except Exception as e:
                yield ProgressUpdate(
                    stage=ProgressStage.ERROR,
                    message=f"[{idx}/{total}] Encoding failed for {item['filename']}: {e}",
                    file_progress=FileProgress(
                        filename=item["filename"],
                        index=idx,
                        total=total,
                        error=str(e),
                    ),
                    error=str(e),
                )
                raise

        elif item["kind"] == "strip":
            h5_path = data_dir / item["filename"]
            raw_path = raw_dir / item["filename"]

            yield ProgressUpdate(
                stage=ProgressStage.COMPRESSING,
                message=f"[{idx}/{total}] Stripping images from {item['filename']}…",
                file_progress=FileProgress(
                    filename=item["filename"], index=idx, total=total
                ),
            )

            try:
                _strip_images_from_h5(raw_path, h5_path)
            except Exception as e:
                yield ProgressUpdate(
                    stage=ProgressStage.ERROR,
                    message=f"[{idx}/{total}] Stripping failed for {item['filename']}: {e}",
                    file_progress=FileProgress(
                        filename=item["filename"],
                        index=idx,
                        total=total,
                        error=str(e),
                    ),
                    error=str(e),
                )
                raise

    _update_metadata(data_dir, meta, episodes)

    yield ProgressUpdate(
        stage=ProgressStage.COMPRESSING,
        message="H.264 conversion complete",
    )


def _build_work_list(
    data_dir: Path, raw_dir: Path, episodes: list[dict]
) -> list[dict]:
    """Build a flat list of work items (MP4 encodes + H5 strips)."""
    items: list[dict] = []
    for ep in episodes:
        h5_file = ep.get("file_name", "")
        if not h5_file:
            continue

        raw_path = raw_dir / h5_file
        h5_path = data_dir / h5_file
        source = raw_path if raw_path.exists() else h5_path

        if not source.exists():
            logger.warning("Episode file %s not found, skipping", h5_file)
            continue

        stem = h5_path.stem

        with h5py.File(str(source), "r") as f:
            obs = f.get("observations")
            if obs is None:
                continue
            images_grp = obs.get("images")
            if images_grp is None:
                continue

            all_mp4s_exist = True
            cam_items: list[dict] = []
            for cam_name in sorted(images_grp.keys()):
                mp4_name = f"{stem}_{cam_name}.mp4"
                if not (data_dir / mp4_name).exists():
                    all_mp4s_exist = False
                cam_items.append({
                    "kind": "mp4",
                    "filename": mp4_name,
                    "h5_file": h5_file,
                    "camera": cam_name,
                })

        if all_mp4s_exist and cam_items:
            if h5_path.exists():
                logger.debug("All MP4s exist for %s — skipping", h5_file)
                continue
            logger.info("All MP4s exist for %s but stripped H5 missing — queuing strip", h5_file)
            items.append({"kind": "strip", "filename": h5_file})
            continue

        items.extend(cam_items)
        items.append({
            "kind": "strip",
            "filename": h5_file,
        })

    return items


def _encode_camera_to_mp4(
    h5_path: Path,
    camera_name: str,
    mp4_path: Path,
    fps: int,
) -> None:
    """Read images for *camera_name* from *h5_path* and encode to MP4."""
    with h5py.File(str(h5_path), "r") as f:
        dset = f[f"observations/images/{camera_name}"]
        num_frames, height, width, channels = dset.shape

        gst_args = [
            "nice", "-n", "0", "gst-launch-1.0", "-e",
            "fdsrc", "fd=0", "!",
            "rawvideoparse", f"width={width}", f"height={height}",
            "format=bgr", f"framerate={fps}/1", "!",
            "videoconvert", "!",
            "x264enc", "pass=qual", "quantizer=23", "speed-preset=ultrafast", "threads=4", "!",
            "h264parse", "!",
            "mp4mux", "!",
            "filesink", f"location={mp4_path}",
        ]

        proc = subprocess.Popen(
            gst_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        try:
            chunk_size = 64
            for start in range(0, num_frames, chunk_size):
                end = min(start + chunk_size, num_frames)
                frames = dset[start:end]
                if not np.isfortran(frames):
                    frames = np.ascontiguousarray(frames)
                proc.stdin.write(frames.tobytes())
        except BrokenPipeError:
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        stderr_bytes = proc.stderr.read()
        proc.wait()

        if proc.returncode != 0:
            stderr = stderr_bytes.decode(errors="replace")
            mp4_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"gst-launch-1.0 exited with code {proc.returncode}: {stderr}"
            )

    raw_size = num_frames * height * width * channels
    mp4_size = mp4_path.stat().st_size
    logger.info(
        "Encoded %s/%s: %d frames, %.1f MB raw -> %.1f MB H.264 (%.1fx)",
        h5_path.name,
        camera_name,
        num_frames,
        raw_size / 1e6,
        mp4_size / 1e6,
        raw_size / max(mp4_size, 1),
    )


def _strip_images_from_h5(raw_path: Path, dest_path: Path) -> None:
    """Create a copy of *raw_path* at *dest_path* without /observations/images/."""
    tmp_path = dest_path.with_suffix(".h5.tmp")
    try:
        with h5py.File(str(raw_path), "r") as src, h5py.File(str(tmp_path), "w") as dst:
            for key in src:
                if key == "observations":
                    obs_grp = dst.create_group("observations")
                    for obs_key in src["observations"]:
                        if obs_key != "images":
                            src["observations"].copy(obs_key, obs_grp)
                else:
                    src.copy(key, dst)

        os.replace(str(tmp_path), str(dest_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    raw_size = raw_path.stat().st_size
    stripped_size = dest_path.stat().st_size
    logger.info(
        "Stripped %s: %.1f MB -> %.1f MB",
        dest_path.name,
        raw_size / 1e6,
        stripped_size / 1e6,
    )


def _update_metadata(
    data_dir: Path, meta: dict, episodes: list[dict]
) -> None:
    """Update dataset_metadata.json with dataset_type and per-episode video_files."""
    meta_path = data_dir / DATASET_METADATA

    meta["dataset_type"] = "h264"

    for ep in meta.get("episodes", []):
        h5_file = ep.get("file_name", "")
        if not h5_file:
            continue
        stem = Path(h5_file).stem
        video_files = sorted(
            p.name
            for p in data_dir.iterdir()
            if p.name.startswith(stem + "_") and p.suffix == ".mp4"
        )
        if video_files:
            ep["video_files"] = video_files

    meta_path.write_text(json.dumps(meta, indent=4) + "\n")
    logger.info("Updated %s: dataset_type -> h264", DATASET_METADATA)
