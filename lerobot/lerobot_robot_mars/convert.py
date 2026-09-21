# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""``mars2lerobot``: export a skill recorded by innate-os to a LeRobotDataset v3.

A skill directory holds ``metadata.json`` (the task text) and ``data/`` with the recorder's
``episode_N.h5`` files, ``dataset_metadata.json``, and, once the background encoder has run,
per-camera MP4s next to an image-stripped h5. The originals survive in ``raw_data/`` while
they exist and are preferred as the frame source. Exported episode ids are written back into
``dataset_metadata.json`` so a rerun appends only what is new.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import h5py
import numpy as np
from huggingface_hub.errors import HfHubHTTPError
from lerobot.configs.video import RGBEncoderConfig
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from lerobot.utils.constants import ACTION, OBS_STATE

from .dataset_meta import DEFAULT_HEAD_ANGLE_DEG, read_sidecar, write_sidecar
from .hub import dataset_root, has_dataset, hub_refusal, hub_url
from .schema import CAMERA_ORDER, CAMERA_SHAPE, FPS, ROBOT_TYPE, dataset_features, image_key

DATASET_METADATA = "dataset_metadata.json"
RAW_DATA_DIR = "raw_data"
# The record of what was exported lives in a file only this converter writes. The recorder rewrites
# dataset_metadata.json from a snapshot taken when the skill was activated and would drop a key kept
# there, after which the next publish appends every episode a second time.
EXPORT_FILE = "lerobot_export.json"
LEGACY_EXPORT_KEY = "lerobot_export"  # where the record lived before it had its own file
CONVERTER = "mars2lerobot"
RECORDER_CAMERAS: tuple[str, ...] = ("camera_1", "camera_2")

Frame = dict[str, np.ndarray]
ImageSource = Callable[[int], dict[str, np.ndarray]]
Log = Callable[[str], None]
Progress = Callable[[dict], None]


@dataclass(frozen=True)
class EpisodeRef:
    episode_id: int
    file_name: str
    source: str
    outcome: str | None
    video_files: tuple[str, ...]

    @property
    def failed(self) -> bool:
        return self.outcome == "failure"


class SkillRecording:
    def __init__(self, skill_dir: Path) -> None:
        self.skill_dir = skill_dir
        self.data_dir = skill_dir / "data"
        self.skill_meta = _read_json(skill_dir / "metadata.json")
        self.dataset_meta = _read_json(self.data_dir / DATASET_METADATA)
        self.export_path = (self.data_dir if self.data_dir.is_dir() else skill_dir) / EXPORT_FILE

    @property
    def name(self) -> str:
        return str(self.skill_meta.get("name") or self.skill_dir.name)

    @property
    def task(self) -> str:
        return str(self.skill_meta.get("guidelines") or self.name)

    @property
    def fps(self) -> int:
        return int(self.dataset_meta.get("data_frequency", FPS))

    def episodes(self, include_failures: bool) -> list[EpisodeRef]:
        listed = self.dataset_meta.get("episodes")
        refs = [_ref_from(entry) for entry in listed] if listed else self._episodes_from_files()
        return [ref for ref in refs if include_failures or not ref.failed]

    def _episodes_from_files(self) -> list[EpisodeRef]:
        # A replay skill keeps its episodes beside metadata.json with no dataset_metadata.json.
        folder = self.data_dir if self.data_dir.is_dir() else self.skill_dir
        files = sorted(folder.glob("episode_*.h5"), key=lambda p: _episode_id(p.name))
        return [EpisodeRef(_episode_id(p.name), p.name, "teleop", None, ()) for p in files]

    def export(self, repo_id: str) -> dict:
        record = _read_json(self.export_path) or self.dataset_meta.get(LEGACY_EXPORT_KEY, {})
        return record if record.get("repo_id") == repo_id else {}

    def record_export(self, repo_id: str, root: Path, episode_ids: set[int], *, pushed: bool) -> None:
        record = {"repo_id": repo_id, "root": str(root), "episode_ids": sorted(episode_ids), "pushed": pushed}
        _write_json_atomic(self.export_path, record)

    def episode_path(self, ref: EpisodeRef) -> Path:
        candidates = (
            self.skill_dir / RAW_DATA_DIR / ref.file_name,
            self.data_dir / ref.file_name,
            self.skill_dir / ref.file_name,
        )
        for path in candidates:
            if path.is_file():
                return path
        raise FileNotFoundError(f"episode {ref.episode_id}: none of {[str(c) for c in candidates]} exists")

    def head_angle(self, ref: EpisodeRef) -> float | None:
        """The head tilt the recorder logged for this episode, in degrees; None when it logged none."""
        with h5py.File(self.episode_path(ref), "r") as h5:
            if "/head_command" not in h5:
                return None
            angles = np.asarray(h5["/head_command"], dtype=np.float64)
        angles = angles[np.isfinite(angles)]
        return float(np.median(angles)) if angles.size else None

    def frames(self, ref: EpisodeRef) -> Iterator[Frame]:
        with h5py.File(self.episode_path(ref), "r") as h5:
            state = np.asarray(h5["/observations/qpos"], dtype=np.float32)[:, :6]
            action = _action_columns(np.asarray(h5["/action"], dtype=np.float64))
            length = min(len(state), len(action))
            with self._images(h5, ref, length) as images:
                for t in range(length):
                    yield {OBS_STATE: state[t], ACTION: action[t], **images(t)}

    @contextlib.contextmanager
    def _images(self, h5: h5py.File, ref: EpisodeRef, length: int) -> Iterator[ImageSource]:
        if "/observations/images" in h5:
            yield _h5_images(h5)
            return
        with _video_images(self.data_dir, ref, length) as source:
            yield source


def _h5_images(h5: h5py.File) -> ImageSource:
    datasets = {
        camera: h5[f"/observations/images/{recorded}"]
        for camera, recorded in zip(CAMERA_ORDER, RECORDER_CAMERAS, strict=True)
    }
    for camera, dataset in datasets.items():
        _check_shape(camera, tuple(dataset.shape[1:]))

    def read(t: int) -> dict[str, np.ndarray]:
        return {image_key(camera): cv2.cvtColor(dataset[t], cv2.COLOR_BGR2RGB) for camera, dataset in datasets.items()}

    return read


@contextlib.contextmanager
def _video_images(data_dir: Path, ref: EpisodeRef, length: int) -> Iterator[ImageSource]:
    captures: dict[str, cv2.VideoCapture] = {}
    for camera, recorded in zip(CAMERA_ORDER, RECORDER_CAMERAS, strict=True):
        matches = [name for name in ref.video_files if name.endswith(f"_{recorded}.mp4")]
        if not matches:
            raise FileNotFoundError(
                f"episode {ref.episode_id}: images were stripped and no MP4 for {recorded} is listed"
            )
        capture = cv2.VideoCapture(str(data_dir / matches[0]))
        if not capture.isOpened():
            raise FileNotFoundError(f"episode {ref.episode_id}: cannot open {matches[0]}")
        captures[camera] = capture
    expected = 0

    def read(t: int) -> dict[str, np.ndarray]:
        nonlocal expected
        if t != expected:
            raise ValueError("video frames must be read in order")
        expected += 1
        frames: dict[str, np.ndarray] = {}
        for camera, capture in captures.items():
            ok, bgr = capture.read()
            if not ok:
                raise ValueError(f"episode {ref.episode_id}: {camera} video ended at frame {t} of {length}")
            _check_shape(camera, bgr.shape)
            frames[image_key(camera)] = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return frames

    try:
        yield read
    finally:
        for capture in captures.values():
            capture.release()


def _check_shape(camera: str, shape: tuple[int, ...]) -> None:
    if tuple(shape) != CAMERA_SHAPE:
        raise ValueError(f"{camera} frames are {shape}; the MARS schema is {CAMERA_SHAPE}")


def _action_columns(action: np.ndarray) -> np.ndarray:
    """Recorder rows are ``[6 arm rad, <extras>, linear.x, angular.z, progress, termination]``.

    The two trailing columns are appended at finalize, so base velocity is always the pair
    before them; an unfinalized 8-wide row has the pair at the end.
    """
    if action.ndim != 2 or action.shape[1] < 8:
        raise ValueError(f"unusable recorder action shape {action.shape}; expected (T, >=8)")
    base = action[:, -4:-2] if action.shape[1] >= 10 else action[:, 6:8]
    return np.hstack([action[:, :6], base]).astype(np.float32)


def _ref_from(entry: dict) -> EpisodeRef:
    return EpisodeRef(
        episode_id=int(entry["episode_id"]),
        file_name=str(entry.get("file_name") or f"episode_{entry['episode_id']}.h5"),
        source=str(entry.get("source", "teleop")),
        outcome=entry.get("outcome"),
        video_files=tuple(entry.get("video_files", [])),
    )


def _episode_id(file_name: str) -> int:
    stem = file_name.removesuffix(".h5")
    try:
        return int(stem.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return -1


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path) as f:
        return json.load(f)


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def open_dataset(
    repo_id: str, root: Path, fps: int, *, vcodec: str | None, image_writer_threads: int
) -> LeRobotDataset:
    encoder = RGBEncoderConfig(vcodec=vcodec) if vcodec else None
    if has_dataset(root):
        dataset = LeRobotDataset.resume(
            repo_id, root=root, rgb_encoder=encoder, image_writer_threads=image_writer_threads
        )
        if dataset.fps != fps:
            raise ValueError(f"{root} was recorded at {dataset.fps} fps; this skill is {fps} fps")
        return dataset
    return LeRobotDataset.create(
        repo_id,
        fps,
        features=dataset_features(),
        root=root,
        robot_type=ROBOT_TYPE,
        use_videos=True,
        rgb_encoder=encoder,
        image_writer_threads=image_writer_threads,
    )


def convert_skill(
    skill_dir: str | Path,
    *,
    repo_id: str,
    root: str | Path | None = None,
    task: str | None = None,
    include_failures: bool = False,
    episode_ids: set[int] | None = None,
    vcodec: str | None = None,
    push: bool = False,
    private: bool = False,
    image_writer_threads: int = 8,
    log: Log = print,
    progress: Progress = lambda _event: None,
) -> Path:
    recording = SkillRecording(Path(skill_dir).expanduser())
    out = dataset_root(repo_id, root)
    task_text = task or recording.task
    exported = _episodes_in_copy(recording, repo_id, out, log)
    todo = [
        ref
        for ref in recording.episodes(include_failures)
        if ref.episode_id not in exported and (episode_ids is None or ref.episode_id in episode_ids)
    ]
    if not todo:
        log(f"{recording.name}: nothing new to export to {repo_id}")
        if push and exported:
            progress({"event": "push"})
            LeRobotDataset(repo_id, root=out).push_to_hub(private=private)
            recording.record_export(repo_id, out, exported, pushed=True)
            progress({"event": "done", "url": hub_url(repo_id), "message": "Already converted; uploaded again"})
        else:
            progress({"event": "done", "url": "", "message": "Nothing new to publish"})
        return out

    dataset = open_dataset(repo_id, out, recording.fps, vcodec=vcodec, image_writer_threads=image_writer_threads)
    if read_sidecar(out) is None:
        # Marks the folder as this converter's from its first moment: only a folder that says so
        # itself may ever be cleared for a rebuild. The head angle follows once the episodes are read.
        write_sidecar(out, head_angle_deg=None, source=CONVERTER)
    head_angles: list[float] = []
    progress({"event": "start", "total": len(todo)})
    for done, ref in enumerate(todo, start=1):
        angle = recording.head_angle(ref)
        if angle is not None:
            head_angles.append(angle)
        count = 0
        for frame in recording.frames(ref):
            dataset.add_frame({**frame, "task": task_text})
            count += 1
        dataset.save_episode()
        log(f"{recording.name}: episode {ref.episode_id} ({ref.source}, {count} frames) -> {repo_id}")
        progress({"event": "episode", "index": done, "total": len(todo), "frames": count})
    dataset.finalize()
    # Only now are the episodes durable: until finalize() the parquet files have no footer.
    exported |= {ref.episode_id for ref in todo}
    recording.record_export(repo_id, out, exported, pushed=False)
    if (read_sidecar(out) or {}).get("head_angle_deg") is None:
        write_sidecar(
            out,
            head_angle_deg=float(np.median(head_angles)) if head_angles else DEFAULT_HEAD_ANGLE_DEG,
            head_angle_assumed=not head_angles,
            source=CONVERTER,
        )
    log(f"{repo_id}: {dataset.num_episodes} episodes, {dataset.num_frames} frames at {out}")
    if push:
        progress({"event": "push"})
        dataset.push_to_hub(private=private)
        recording.record_export(repo_id, out, exported, pushed=True)
        log(f"pushed to {hub_url(repo_id)}")
    episodes = f"{len(todo)} episode{'s' if len(todo) != 1 else ''}"
    progress({"event": "done", "url": hub_url(repo_id) if push else "", "message": f"Published {episodes}"})
    return out


def _episodes_in_copy(recording: SkillRecording, repo_id: str, out: Path, log: Log) -> set[int]:
    """The episode ids the converted copy at `out` already holds, after clearing a copy that cannot be trusted.

    Whatever already sits at `out` is touched, appended to or cleared, only if the folder itself says it
    is this converter's (its meta/mars.json): the export record merely names a path, and what is at that
    path may since have been replaced by a dataset someone recorded.

    Our own copy is appended to only when it holds exactly the recorded episodes. One left by a run
    killed before finalize() is unreadable, and one that lost episodes must not be topped up and uploaded
    over the full dataset; both are rebuilt from every episode.
    """
    if not out.exists():
        return set()
    if (read_sidecar(out) or {}).get("source") != CONVERTER:
        raise FileExistsError(
            f"{out} holds something this converter did not write; move it away or choose another --root"
        )
    exported = {int(i) for i in recording.export(repo_id).get("episode_ids", [])}
    if exported and _read_json(out / "meta" / "info.json").get("total_episodes") == len(exported):
        return exported
    log(f"{recording.name}: the converted copy at {out} is incomplete; rebuilding it from every episode")
    shutil.rmtree(out)
    return set()


def _print_event(event: dict) -> None:
    print(json.dumps(event), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mars2lerobot", description="Export a recorded MARS skill to LeRobotDataset v3."
    )
    parser.add_argument("skill_dir", help="skill directory, e.g. workspace/custom_skills/pick_cube")
    parser.add_argument("--repo-id", help="Hub-style id; default innate/mars-<skill name>")
    parser.add_argument("--root", help="where to write the dataset; default $HF_LEROBOT_HOME/<repo-id>")
    parser.add_argument("--task", help="task text stored with every frame; default the skill's guidelines")
    parser.add_argument("--include-failures", action="store_true", help="also export episodes marked failure")
    parser.add_argument("--episode", type=int, action="append", dest="episodes", help="only these episode ids")
    parser.add_argument("--vcodec", help="video codec, e.g. h264 or libsvtav1; default lerobot's")
    parser.add_argument("--image-writer-threads", type=int, default=8)
    parser.add_argument("--push", action="store_true", help="push to the Hugging Face Hub when done")
    parser.add_argument("--private", action="store_true", help="create the Hub repo as private")
    parser.add_argument("--progress-json", action="store_true", help="print one JSON event per line for a UI")
    args = parser.parse_args(argv)

    skill_dir = Path(args.skill_dir).expanduser()
    if not skill_dir.is_dir():
        parser.error(f"{skill_dir} is not a directory")
    repo_id = args.repo_id or f"innate/mars-{SkillRecording(skill_dir).name}"
    report = _print_event if args.progress_json else (lambda _event: None)
    try:
        convert_skill(
            skill_dir,
            repo_id=repo_id,
            root=args.root,
            task=args.task,
            include_failures=args.include_failures,
            episode_ids=set(args.episodes) if args.episodes else None,
            vcodec=args.vcodec,
            push=args.push,
            private=args.private,
            image_writer_threads=args.image_writer_threads,
            log=(lambda _message: None) if args.progress_json else print,
            progress=report,
        )
    except (HfHubHTTPError, FileExistsError) as e:
        message = hub_refusal(e, repo_id) if isinstance(e, HfHubHTTPError) else str(e)
        report({"event": "error", "message": message})
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
