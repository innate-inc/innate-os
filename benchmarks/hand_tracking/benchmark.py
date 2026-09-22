#!/usr/bin/env python3
"""Freeze recordings and compare complete hand detection + landmark pipelines.

This stage emits predictions and latency, not accuracy without human labels.
All paths in a frozen manifest are relative to that manifest's directory.
"""

import argparse
import hashlib
import importlib.metadata
import io
import json
import math
import platform
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = {
    "mediapipe.task": "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
    "rtmdet.onnx": "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmdet_nano_8xb32-300e_hand-267f9c8f.zip",
    "rtmpose.onnx": "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmpose-m_simcc-hand5_pt-aic-coco_210e-256x256-74fb594_20230320.zip",
}


def sha(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def setup():
    folder = ROOT / "models"
    folder.mkdir(exist_ok=True)
    provenance = folder / "provenance.json"
    if provenance.exists():
        verify_models()
        print("Existing model files verified.")
        return
    records = {}
    for name, url in MODELS.items():
        print(f"Downloading {name}", flush=True)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        archive_sha = hashlib.sha256(data).hexdigest()
        if url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                candidates = [n for n in archive.namelist() if n.endswith(".onnx")]
                if len(candidates) != 1:
                    raise ValueError(f"Expected one ONNX file in {url}, got {candidates}")
                data = archive.read(candidates[0])
        (folder / name).write_bytes(data)
        records[name] = {"url": url, "sha256": sha(folder / name), "download_sha256": archive_sha}
    write_json(provenance, records)
    print(f"Models and download hashes saved to {folder}")


def verify_models():
    provenance = json.loads((ROOT / "models/provenance.json").read_text())
    for name, info in provenance.items():
        if sha(ROOT / "models" / name) != info["sha256"]:
            raise ValueError(f"Model changed: {name}")
    return provenance


def freeze(data):
    data = data.resolve()
    destination = data / "manifest.json"
    if destination.exists():
        raise ValueError("Manifest already frozen. Use a new data folder for a new dataset.")
    clips = []
    for path in sorted(data.glob("*/*/metadata.json")):
        metadata = json.loads(path.read_text())
        if metadata.get("complete") is not True:
            continue
        video = path.parent / metadata["video"]
        if sha(video) != metadata["sha256"]:
            raise ValueError(f"Recording hash mismatch: {video}")
        clips.append(
            {
                "id": metadata["recording_id"],
                "session": metadata["session"],
                "clip": metadata["clip"],
                "split": metadata["split"],
                "video": str(video.relative_to(data)),
                "sha256": metadata["sha256"],
                "metadata": str(path.relative_to(data)),
                "metadata_sha256": sha(path),
            }
        )
    if not clips:
        raise ValueError("No completed recordings yet. Record with serve.py first.")
    if len({c["id"] for c in clips}) != len(clips):
        raise ValueError("Duplicate recording IDs")
    # Freeze whole recording sessions; never random-split neighboring frames.
    sessions = {}
    for clip in clips:
        sessions.setdefault(clip["session"], set()).add(clip["split"])
    if any(len(splits) != 1 for splits in sessions.values()):
        raise ValueError("A session cannot belong to multiple splits")
    with destination.open("x") as file:
        json.dump({"version": 1, "protocol_sha256": sha(ROOT / "protocol.json"), "clips": clips}, file, indent=2)
        file.write("\n")
    print(f"Frozen {len(clips)} clips from {len(sessions)} sessions: {destination}")


class MediaPipe:
    def __init__(self):
        import mediapipe as mp

        self.mp = mp
        self.options = mp.tasks.vision.HandLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(
                model_asset_path=str(ROOT / "models/mediapipe.task"), delegate=mp.tasks.BaseOptions.Delegate.CPU
            ),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.tracker = None
        self.reset()
        self.info = {
            "backend": "MediaPipe CPU delegate",
            "max_hands": 1,
            "thresholds": [0.5, 0.5, 0.5],
            "mode": "VIDEO",
        }

    def reset(self):
        if self.tracker:
            self.tracker.close()
        self.tracker = self.mp.tasks.vision.HandLandmarker.create_from_options(self.options)

    def predict(self, frame, timestamp_ms):
        import cv2

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self.tracker.detect_for_video(
            self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb), timestamp_ms
        )
        height, width = frame.shape[:2]
        hands = []
        for points, side in zip(result.hand_landmarks, result.handedness, strict=True):
            hands.append(
                {
                    "xy": [[p.x * width, p.y * height] for p in points],
                    "handedness": side[0].category_name,
                    "handedness_score": side[0].score,
                }
            )
        # The handedness score is NOT a hand detection confidence.
        return hands

    def warmup(self, frame):
        for i in range(10):
            self.predict(frame, i * 34)

    def close(self):
        self.tracker.close()


class RTMPose:
    def __init__(self):
        from rtmlib import Hand

        self.tracker = Hand(
            det=str(ROOT / "models/rtmdet.onnx"),
            pose=str(ROOT / "models/rtmpose.onnx"),
            mode="lightweight",
            backend="onnxruntime",
            device="cpu",
            to_openpose=False,
        )
        self.info = {
            "backend": "ONNX Runtime CPU",
            "mode": "framewise",
            "detector_score_threshold": 0.3,
            "detector_nms": "embedded in pinned ONNX model",
            "keypoint_threshold": 0.3,
            "min_palm_points": 4,
            "max_hands": "detector native (all)",
            "providers": self.tracker.det_model.session.get_providers(),
            "onnx_intra_op_threads": self.tracker.det_model.session.get_session_options().intra_op_num_threads,
            "onnx_inter_op_threads": self.tracker.det_model.session.get_session_options().inter_op_num_threads,
        }

    def reset(self):
        pass  # Framewise pipeline; no temporal state.

    def predict(self, frame, timestamp_ms):
        boxes = self.tracker.det_model(frame)
        # rtmlib otherwise estimates a hand from the entire image when there are
        # zero boxes. That fallback must not count as a detected hand.
        if len(boxes) == 0:
            return []
        points, scores = self.tracker.pose_model(frame, bboxes=boxes)
        hands = []
        for xy, score, box in zip(points, scores, boxes, strict=True):
            if sum(float(score[i]) >= 0.3 for i in (0, 5, 9, 13, 17)) >= 4:
                hands.append({"xy": xy.tolist(), "scores": score.tolist(), "bbox": box.tolist()})
        return hands

    def warmup(self, frame):
        h, w = frame.shape[:2]
        for _ in range(10):
            self.tracker.det_model(frame)
            self.tracker.pose_model(frame, bboxes=[[0, 0, w, h]])

    def close(self):
        pass


def summarize(rows):
    import numpy as np

    latency = np.array([r["latency_ms"] for r in rows])
    coverage = sum(bool(r["hands"]) for r in rows)
    return {
        "frames": len(rows),
        "frames_with_any_hand": coverage,
        "detection_coverage_not_recall": coverage / len(rows),
        "latency_ms_p50": float(np.percentile(latency, 50)),
        "latency_ms_p95": float(np.percentile(latency, 95)),
        "latency_ms_mean": float(latency.mean()),
        "accuracy": None,
        "accuracy_status": "Requires independent reviewed annotations",
        "timing_scope": "synchronous detector + crop + pose + Python output conversion; excludes decode, display, camera, network, robot",
        "real_time_performance": "not measured by offline replay",
    }


def model_timestamp(pts, previous_pts, previous_timestamp):
    """Keep original PTS; only break millisecond ties for the MediaPipe API."""
    if not math.isfinite(pts) or pts < 0 or (previous_pts is not None and pts < previous_pts):
        raise ValueError("Invalid/decreasing video timestamp")
    return max(previous_timestamp + 1, round(pts))


def run(args):
    import cv2
    import numpy as np

    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    clips = [c for c in manifest["clips"] if c["split"] == args.split]
    if not clips:
        raise ValueError(f"No clips in split {args.split}")
    for clip in clips:
        for field, digest in (("video", "sha256"), ("metadata", "metadata_sha256")):
            if sha(manifest_path.parent / clip[field]) != clip[digest]:
                raise ValueError(f"Frozen input changed: {clip[field]}")
    provenance = verify_models()
    args.output.mkdir(parents=True, exist_ok=False)
    cv2.setNumThreads(1)
    config = {
        "model": args.model,
        "split": args.split,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "versions": {
            p: importlib.metadata.version(p) for p in ("mediapipe", "rtmlib", "onnxruntime", "numpy", "opencv-python")
        },
        "manifest_sha256": sha(manifest_path),
        "models": provenance,
        "harness_sha256": sha(__file__),
        "opencv_threads": cv2.getNumThreads(),
        "warmup": "10 blank frames, separate from each replay; pose stage explicitly warmed for RTMPose",
        "timestamp_policy": "Original decoded PTS retained; equal/rounded millisecond timestamps advanced minimally for model API",
        "status": "running",
    }
    write_json(args.output / "config.json", config)
    model = None
    try:
        model = MediaPipe() if args.model == "mediapipe" else RTMPose()
        config["pipeline"] = model.info
        model.warmup(np.zeros((480, 640, 3), dtype=np.uint8))
        all_rows, summaries = [], []
        with (args.output / "predictions.jsonl").open("x") as output:
            for clip in clips:
                model.reset()
                capture = cv2.VideoCapture(str(manifest_path.parent / clip["video"]))
                rows, previous_timestamp = [], -1
                try:
                    if not capture.isOpened():
                        raise ValueError(f"Cannot decode {clip['video']}")
                    while True:
                        ok, frame = capture.read()
                        if not ok:
                            break
                        pts = capture.get(cv2.CAP_PROP_POS_MSEC)
                        timestamp = model_timestamp(pts, rows[-1]["pts_ms"] if rows else None, previous_timestamp)
                        previous_timestamp = timestamp
                        start = time.perf_counter_ns()
                        hands = model.predict(frame, timestamp)
                        elapsed = (time.perf_counter_ns() - start) / 1e6
                        row = {
                            "clip_id": clip["id"],
                            "frame": len(rows),
                            "pts_ms": pts,
                            "model_timestamp_ms": timestamp,
                            "width": frame.shape[1],
                            "height": frame.shape[0],
                            "latency_ms": elapsed,
                            "hands": hands,
                        }
                        # No truth/prompt metadata enters the model.
                        output.write(json.dumps(row, allow_nan=False) + "\n")
                        rows.append(row)
                    if not rows:
                        raise ValueError(f"Video has no decoded frames: {clip['video']}")
                    expected_ms = json.loads((manifest_path.parent / clip["metadata"]).read_text())["duration_ms"]
                    if rows[-1]["pts_ms"] < expected_ms - 1000:
                        raise ValueError(f"Video is truncated relative to recording duration: {clip['video']}")
                finally:
                    capture.release()
                summary = dict(summarize(rows), clip_id=clip["id"], condition=clip["clip"], session=clip["session"])
                summaries.append(summary)
                all_rows.extend(rows)
                print(f"{clip['clip']}: {len(rows)} frames, p95 {summary['latency_ms_p95']:.1f} ms", flush=True)
        write_json(args.output / "summary.json", {"overall": summarize(all_rows), "clips": summaries})
        config["status"] = "complete"
    except Exception as error:
        config.update(status="failed", error=str(error))
        raise
    finally:
        if model:
            model.close()
        write_json(args.output / "config.json", config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup")
    freeze_parser = commands.add_parser("freeze")
    freeze_parser.add_argument("--data", type=Path, default=ROOT / "data")
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--manifest", type=Path, default=ROOT / "data/manifest.json")
    run_parser.add_argument("--model", choices=("mediapipe", "rtmpose"), required=True)
    run_parser.add_argument("--split", choices=("calibration", "test_normal", "test_challenging"), required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "setup":
        setup()
    elif args.command == "freeze":
        freeze(args.data)
    else:
        run(args)


if __name__ == "__main__":
    main()
