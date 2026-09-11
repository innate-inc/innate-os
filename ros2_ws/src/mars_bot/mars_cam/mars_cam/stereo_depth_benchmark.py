#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import rosbag2_py
import tf2_ros
import yaml
from rclpy.duration import Duration
from rclpy.serialization import deserialize_message
from rclpy.time import Time
from rosidl_runtime_py.utilities import get_message
from sensor_msgs.msg import CameraInfo, CompressedImage, Image, LaserScan, PointCloud2
from sensor_msgs_py import point_cloud2
from tf2_msgs.msg import TFMessage

CANONICAL_TOPICS = (
    "/mars/main_camera/left/image_raw",
    "/mars/main_camera/right/image_raw",
    "/mars/main_camera/left/camera_info",
    "/mars/main_camera/right/camera_info",
    "/tf",
    "/tf_static",
    "/scan",
    "/odom",
    "/imu",
)

BIN_EDGES_METERS = (0.5, 1.0, 1.5, 2.0, 3.0)
SOURCE_TAG = "stereo_depth_benchmark"


@dataclass(frozen=True)
class CameraIntrinsics:
    frame_id: str
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float


@dataclass(frozen=True)
class LidarFrame:
    stamp_ns: int
    header_stamp_ns: int
    frame_id: str
    points_xyz: np.ndarray


@dataclass(frozen=True)
class DepthFrame:
    stamp_ns: int
    header_stamp_ns: int
    depth_m: np.ndarray


@dataclass(frozen=True)
class ErrorSamples:
    width: int
    height: int
    lidar_depth_m: np.ndarray
    model_depth_m: np.ndarray
    abs_error_m: np.ndarray
    rel_error: np.ndarray
    u_px: np.ndarray
    v_px: np.ndarray


@dataclass(frozen=True)
class ModelSpec:
    name: str
    depth_topic: str
    lidar_topic: str
    camera_info_topic: str
    input_camera_topic: str
    inference_topic: str
    disparity_topic: str | None
    pointcloud_topic: str | None
    costmap_topic: str | None
    tegrastats_log: Path | None
    launch_command: str | None
    run_bag: Path | None
    warmup_sec: float
    settle_sec: float


@dataclass
class BinAccumulator:
    count: int = 0
    abs_error_sum: float = 0.0
    sq_error_sum: float = 0.0

    def add(self, errors: np.ndarray) -> None:
        if errors.size == 0:
            return
        abs_err = np.abs(errors)
        self.count += int(errors.size)
        self.abs_error_sum += float(abs_err.sum())
        self.sq_error_sum += float(np.square(errors).sum())

    def summary(self) -> dict[str, float | int]:
        if self.count == 0:
            return {"count": 0, "mae_m": float("nan"), "rmse_m": float("nan")}
        return {
            "count": self.count,
            "mae_m": self.abs_error_sum / self.count,
            "rmse_m": math.sqrt(self.sq_error_sum / self.count),
        }


class MetricsAccumulator:
    def __init__(self) -> None:
        self.depth_frames_seen = 0
        self.depth_frames_with_sync = 0
        self.depth_frames_with_transform = 0
        self.projected_points = 0
        self.valid_pairs = 0
        self.abs_error_sum = 0.0
        self.sq_error_sum = 0.0
        self.rel_error_sum = 0.0
        self.abs_errors: list[float] = []
        self.within_5cm = 0
        self.within_10cm = 0
        self.bins = _make_bin_map()

    def add(self, gt: np.ndarray, pred: np.ndarray) -> None:
        if gt.size == 0:
            return
        err = pred - gt
        abs_err = np.abs(err)
        rel_err = abs_err / np.maximum(gt, 1e-6)

        self.valid_pairs += int(gt.size)
        self.abs_error_sum += float(abs_err.sum())
        self.sq_error_sum += float(np.square(err).sum())
        self.rel_error_sum += float(rel_err.sum())
        self.within_5cm += int(np.count_nonzero(abs_err <= 0.05))
        self.within_10cm += int(np.count_nonzero(abs_err <= 0.10))
        self.abs_errors.extend(abs_err.tolist())
        _accumulate_bins(self.bins, gt, err)

    def summary(self) -> dict[str, Any]:
        if self.valid_pairs == 0:
            return {
                "depth_frames_seen": self.depth_frames_seen,
                "depth_frames_with_sync": self.depth_frames_with_sync,
                "depth_frames_with_transform": self.depth_frames_with_transform,
                "projected_points": self.projected_points,
                "valid_pairs": 0,
                "mae_m": float("nan"),
                "rmse_m": float("nan"),
                "mean_relative_error": float("nan"),
                "median_abs_error_m": float("nan"),
                "p90_abs_error_m": float("nan"),
                "pct_within_5cm": float("nan"),
                "pct_within_10cm": float("nan"),
                "distance_bins_m": {label: acc.summary() for label, acc in self.bins.items()},
            }

        abs_errors = np.array(self.abs_errors, dtype=np.float64)
        return {
            "depth_frames_seen": self.depth_frames_seen,
            "depth_frames_with_sync": self.depth_frames_with_sync,
            "depth_frames_with_transform": self.depth_frames_with_transform,
            "projected_points": self.projected_points,
            "valid_pairs": self.valid_pairs,
            "mae_m": self.abs_error_sum / self.valid_pairs,
            "rmse_m": math.sqrt(self.sq_error_sum / self.valid_pairs),
            "mean_relative_error": self.rel_error_sum / self.valid_pairs,
            "median_abs_error_m": float(np.median(abs_errors)),
            "p90_abs_error_m": float(np.quantile(abs_errors, 0.90)),
            "pct_within_5cm": 100.0 * self.within_5cm / self.valid_pairs,
            "pct_within_10cm": 100.0 * self.within_10cm / self.valid_pairs,
            "distance_bins_m": {label: acc.summary() for label, acc in self.bins.items()},
        }


@dataclass
class TegrastatsSession:
    proc: subprocess.Popen[Any]
    file_handle: Any
    log_path: Path


def _make_bin_map() -> dict[str, BinAccumulator]:
    labels: list[str] = []
    lower = 0.0
    for upper in BIN_EDGES_METERS:
        labels.append(f"{lower:.2f}-{upper:.2f}")
        lower = upper
    labels.append(f"{BIN_EDGES_METERS[-1]:.2f}+")
    return {label: BinAccumulator() for label in labels}


def _accumulate_bins(bins: dict[str, BinAccumulator], gt: np.ndarray, err: np.ndarray) -> None:
    lower = 0.0
    labels = list(bins.keys())
    for idx, upper in enumerate(BIN_EDGES_METERS):
        sel = (gt >= lower) & (gt < upper)
        bins[labels[idx]].add(err[sel])
        lower = upper
    bins[labels[-1]].add(err[gt >= BIN_EDGES_METERS[-1]])


def _reader_for_bag(bag_path: Path) -> tuple[rosbag2_py.SequentialReader, dict[str, str]]:
    storage_id = "sqlite3"
    metadata_path = bag_path / "metadata.yaml"
    if metadata_path.exists():
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
        storage_id = (
            metadata.get("rosbag2_bagfile_information", {}).get("storage_identifier")
            or metadata.get("storage_identifier")
            or "sqlite3"
        )
    reader = rosbag2_py.SequentialReader()
    storage = rosbag2_py.StorageOptions(uri=str(bag_path), storage_id=storage_id)
    converter = rosbag2_py.ConverterOptions(input_serialization_format="cdr", output_serialization_format="cdr")
    reader.open(storage, converter)
    topic_types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    return reader, topic_types


def _ensure_topic(topic_types: dict[str, str], topic: str, bag_path: Path) -> str:
    if topic not in topic_types:
        known = ", ".join(sorted(topic_types))
        raise RuntimeError(f"Topic '{topic}' not found in {bag_path}. Topics: {known}")
    return topic_types[topic]


def _stop_process(proc: subprocess.Popen[Any], name: str) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=10)
        return
    except (ProcessLookupError, PermissionError):
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
        return
    except (ProcessLookupError, PermissionError):
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=2)
        print(f"[{name}] force-killed after timeout")
    except (ProcessLookupError, PermissionError):
        return


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    arr = np.array(values, dtype=np.float64)
    return float(np.quantile(arr, q))


def _series_stats(values: list[float], prefix: str, unit: str = "") -> dict[str, float]:
    suffix = f"_{unit}" if unit else ""
    if not values:
        return {
            f"{prefix}_median{suffix}": float("nan"),
            f"{prefix}_p95{suffix}": float("nan"),
            f"{prefix}_p99{suffix}": float("nan"),
            f"{prefix}_mean{suffix}": float("nan"),
            f"{prefix}_max{suffix}": float("nan"),
        }
    arr = np.array(values, dtype=np.float64)
    return {
        f"{prefix}_median{suffix}": float(np.median(arr)),
        f"{prefix}_p95{suffix}": float(np.quantile(arr, 0.95)),
        f"{prefix}_p99{suffix}": float(np.quantile(arr, 0.99)),
        f"{prefix}_mean{suffix}": float(np.mean(arr)),
        f"{prefix}_max{suffix}": float(np.max(arr)),
    }


def _start_tegrastats(log_path: Path, interval_ms: int) -> TegrastatsSession | None:
    try:
        handle = open(log_path, "w", encoding="utf-8")
    except OSError:
        return None
    try:
        proc = subprocess.Popen(
            ["tegrastats", "--interval", str(max(100, interval_ms))],
            stdout=handle,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
    except (FileNotFoundError, OSError):
        handle.close()
        return None
    return TegrastatsSession(proc=proc, file_handle=handle, log_path=log_path)


def _stop_tegrastats(session: TegrastatsSession | None) -> None:
    if session is None:
        return
    _stop_process(session.proc, "tegrastats")
    try:
        session.file_handle.close()
    except Exception:
        pass


def _parse_tegrastats_log(log_path: Path) -> dict[str, Any]:
    if not log_path.exists():
        return {
            "tegrastats_available": False,
            "tegrastats_log": "",
            "thermal_throttling_detected": False,
        }

    gpu_util: list[float] = []
    cpu_util: list[float] = []
    ram_used_mb: list[float] = []
    ram_used_pct: list[float] = []
    gpu_mem_used_mb: list[float] = []
    power_w: list[float] = []
    temp_c: list[float] = []
    samples = 0
    ram_total_mb = 0.0
    thermal_throttling = False

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            samples += 1
            lower = text.lower()
            if "throt" in lower or "throttle" in lower or "over-current" in lower or "soctherm" in lower:
                thermal_throttling = True

            ram_match = re.search(r"RAM\s+(\d+)\/(\d+)MB", text)
            if ram_match:
                used = float(ram_match.group(1))
                total = float(ram_match.group(2))
                ram_used_mb.append(used)
                ram_total_mb = max(ram_total_mb, total)
                if total > 0:
                    ram_used_pct.append(100.0 * used / total)

            cpu_match = re.search(r"CPU\s+\[([^\]]+)\]", text)
            if cpu_match:
                util_values: list[float] = []
                for token in cpu_match.group(1).split(","):
                    util_match = re.search(r"(\d+)%", token)
                    if util_match:
                        util_values.append(float(util_match.group(1)))
                if util_values:
                    cpu_util.append(float(np.mean(np.array(util_values, dtype=np.float64))))

            gr3d_match = re.search(r"GR3D_FREQ\s+(\d+)%", text)
            if gr3d_match:
                gpu_util.append(float(gr3d_match.group(1)))

            gpu_mem_match = re.search(r"(?:GPU|GR3D_MEM|FB)\s+(\d+)\/(\d+)MB", text)
            if gpu_mem_match:
                gpu_mem_used_mb.append(float(gpu_mem_match.group(1)))

            pom_match = re.search(r"POM_5V_IN\s+(\d+)(?:\/(\d+))?", text)
            if pom_match:
                now_mw = float(pom_match.group(1))
                power_w.append(now_mw / 1000.0)
            else:
                vdd_match = re.search(r"VDD_IN\s+(\d+)mW(?:\/(\d+)mW)?", text)
                if vdd_match:
                    power_w.append(float(vdd_match.group(1)) / 1000.0)

            for temp_match in re.finditer(r"([A-Za-z0-9_]+)@([0-9]+(?:\.[0-9]+)?)C", text):
                temp_c.append(float(temp_match.group(2)))

    summary: dict[str, Any] = {
        "tegrastats_available": samples > 0,
        "tegrastats_log": str(log_path),
        "tegrastats_samples": samples,
        "ram_total_mb": ram_total_mb if ram_total_mb > 0 else float("nan"),
        "thermal_throttling_detected": thermal_throttling,
        "max_temperature_c": max(temp_c) if temp_c else float("nan"),
    }
    summary.update(_series_stats(cpu_util, "cpu_util", "pct"))
    summary.update(_series_stats(gpu_util, "gpu_util", "pct"))
    summary.update(_series_stats(ram_used_mb, "ram_used", "mb"))
    summary.update(_series_stats(ram_used_pct, "ram_used", "pct"))
    summary.update(_series_stats(gpu_mem_used_mb, "gpu_mem_used", "mb"))
    summary.update(_series_stats(power_w, "power", "w"))
    summary.update(_series_stats(temp_c, "temperature", "c"))
    return summary


def _extract_header_stamp_ns(msg: Any) -> int | None:
    header = getattr(msg, "header", None)
    if header is None:
        return None
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return None
    sec = getattr(stamp, "sec", None)
    nanosec = getattr(stamp, "nanosec", None)
    if sec is None or nanosec is None:
        return None
    return _stamp_ns(int(sec), int(nanosec))


def _fps_from_stamps_ns(stamps_ns: list[int]) -> float:
    if len(stamps_ns) < 2:
        return float("nan")
    span_ns = stamps_ns[-1] - stamps_ns[0]
    if span_ns <= 0:
        return float("nan")
    return float((len(stamps_ns) - 1) * 1e9 / span_ns)


def _camera_to_topic_latency(
    camera_bag_stamps_ns: list[int],
    camera_header_sort_indices: list[int],
    camera_header_sorted_ns: list[int],
    output_bag_stamps_ns: list[int],
    output_header_stamps_ns: list[int],
    header_match_tolerance_ns: int,
    pairing_window_ns: int,
) -> tuple[list[float], int]:
    latencies_ms: list[float] = []
    matched_camera_indices: set[int] = set()
    if not camera_bag_stamps_ns:
        return latencies_ms, 0

    for bag_ns, hdr_ns in zip(output_bag_stamps_ns, output_header_stamps_ns):
        camera_idx: int | None = None

        if hdr_ns > 0 and camera_header_sorted_ns:
            right = bisect.bisect_left(camera_header_sorted_ns, hdr_ns)
            candidate_positions = []
            if right < len(camera_header_sorted_ns):
                candidate_positions.append(right)
            if right > 0:
                candidate_positions.append(right - 1)
            best_delta = None
            for pos in candidate_positions:
                delta = abs(camera_header_sorted_ns[pos] - hdr_ns)
                if delta <= header_match_tolerance_ns and (best_delta is None or delta < best_delta):
                    best_delta = delta
                    camera_idx = camera_header_sort_indices[pos]

        if camera_idx is None:
            idx = bisect.bisect_right(camera_bag_stamps_ns, bag_ns) - 1
            if idx >= 0 and bag_ns - camera_bag_stamps_ns[idx] <= pairing_window_ns:
                camera_idx = idx

        if camera_idx is None:
            continue
        camera_bag_ns = camera_bag_stamps_ns[camera_idx]
        if bag_ns < camera_bag_ns:
            continue
        matched_camera_indices.add(camera_idx)
        latencies_ms.append((bag_ns - camera_bag_ns) / 1e6)

    return latencies_ms, len(matched_camera_indices)


def _empty_latency_summary(prefix: str) -> dict[str, float]:
    return {
        f"{prefix}_latency_median_ms": float("nan"),
        f"{prefix}_latency_p95_ms": float("nan"),
        f"{prefix}_latency_p99_ms": float("nan"),
        f"{prefix}_latency_mean_ms": float("nan"),
        f"{prefix}_effective_fps": float("nan"),
    }


def _build_runtime_performance_metrics(
    bag_path: Path,
    input_camera_topic: str,
    inference_topic: str,
    disparity_topic: str | None,
    pointcloud_topic: str | None,
    costmap_topic: str | None,
    header_match_tolerance_ms: float,
    pairing_window_ms: float,
) -> dict[str, Any]:
    requested_topics = [input_camera_topic, inference_topic]
    for opt in (disparity_topic, pointcloud_topic, costmap_topic):
        if opt:
            requested_topics.append(opt)
    unique_topics = list(dict.fromkeys(requested_topics))

    reader, topic_types = _reader_for_bag(bag_path)
    present_topics = [topic for topic in unique_topics if topic in topic_types]
    type_map = {topic: topic_types[topic] for topic in present_topics}
    msg_cls_map = {topic: get_message(msg_type) for topic, msg_type in type_map.items()}
    series = {topic: {"bag_ns": [], "header_ns": []} for topic in present_topics}

    while reader.has_next():
        topic, serialized, bag_stamp_ns = reader.read_next()
        if topic not in series:
            continue
        msg = deserialize_message(serialized, msg_cls_map[topic])
        header_ns = _extract_header_stamp_ns(msg)
        series[topic]["bag_ns"].append(int(bag_stamp_ns))
        series[topic]["header_ns"].append(int(header_ns) if header_ns is not None else 0)

    if input_camera_topic not in series:
        raise RuntimeError(f"Input camera topic '{input_camera_topic}' not found in {bag_path}")

    cam_bag = series[input_camera_topic]["bag_ns"]
    cam_hdr = series[input_camera_topic]["header_ns"]
    camera_bag_stamps_ns = [int(v) for v in cam_bag]
    camera_header_stamps_ns = [int(h) if int(h) > 0 else int(b) for b, h in zip(cam_bag, cam_hdr)]
    camera_count = len(camera_bag_stamps_ns)
    camera_header_sort_indices = sorted(range(camera_count), key=lambda i: camera_header_stamps_ns[i])
    camera_header_sorted_ns = [camera_header_stamps_ns[i] for i in camera_header_sort_indices]

    header_match_tolerance_ns = int(max(0.0, header_match_tolerance_ms) * 1e6)
    pairing_window_ns = int(max(1.0, pairing_window_ms) * 1e6)

    def summarize(topic: str | None, label: str) -> tuple[dict[str, Any], int]:
        if not topic or topic not in series:
            base = _empty_latency_summary(label)
            base[f"{label}_topic_present"] = False
            base[f"{label}_frames"] = 0
            return base, 0

        out_bag = series[topic]["bag_ns"]
        out_hdr = series[topic]["header_ns"]
        lat_ms, matched = _camera_to_topic_latency(
            camera_bag_stamps_ns=camera_bag_stamps_ns,
            camera_header_sort_indices=camera_header_sort_indices,
            camera_header_sorted_ns=camera_header_sorted_ns,
            output_bag_stamps_ns=out_bag,
            output_header_stamps_ns=out_hdr,
            header_match_tolerance_ns=header_match_tolerance_ns,
            pairing_window_ns=pairing_window_ns,
        )
        summary = _empty_latency_summary(label)
        summary.update(
            {
                f"{label}_latency_median_ms": _quantile(lat_ms, 0.50),
                f"{label}_latency_p95_ms": _quantile(lat_ms, 0.95),
                f"{label}_latency_p99_ms": _quantile(lat_ms, 0.99),
                f"{label}_latency_mean_ms": float(np.mean(np.array(lat_ms, dtype=np.float64))) if lat_ms else float("nan"),
                f"{label}_effective_fps": _fps_from_stamps_ns(out_bag),
                f"{label}_topic_present": True,
                f"{label}_frames": len(out_bag),
            }
        )
        return summary, matched

    inference_summary, inference_matched = summarize(inference_topic, "inference")
    disparity_summary, _ = summarize(disparity_topic, "camera_to_disparity")
    pointcloud_summary, _ = summarize(pointcloud_topic, "camera_to_pointcloud")
    costmap_summary, _ = summarize(costmap_topic, "camera_to_costmap")

    dropped_frames = max(0, camera_count - inference_matched)
    dropped_pct = 100.0 * dropped_frames / camera_count if camera_count > 0 else float("nan")
    runtime: dict[str, Any] = {
        "input_camera_topic": input_camera_topic,
        "inference_topic": inference_topic,
        "disparity_topic": disparity_topic or "",
        "pointcloud_topic": pointcloud_topic or "",
        "costmap_topic": costmap_topic or "",
        "input_camera_frames": camera_count,
        "input_camera_effective_fps": _fps_from_stamps_ns(cam_bag),
        "dropped_frames": dropped_frames,
        "dropped_frames_pct": dropped_pct,
    }
    runtime.update(inference_summary)
    runtime.update(disparity_summary)
    runtime.update(pointcloud_summary)
    runtime.update(costmap_summary)
    return runtime


def _quaternion_to_rot_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    xx = x * x
    yy = y * y
    zz = z * z
    xy = x * y
    xz = x * z
    yz = y * z
    wx = w * x
    wy = w * y
    wz = w * z
    return np.array(
        [
            [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)],
            [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)],
            [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)],
        ],
        dtype=np.float64,
    )


def _parse_laserscan(msg: LaserScan) -> np.ndarray:
    ranges = np.asarray(msg.ranges, dtype=np.float32)
    if ranges.size == 0:
        return np.empty((0, 3), dtype=np.float32)
    angles = msg.angle_min + np.arange(ranges.size, dtype=np.float32) * msg.angle_increment
    valid = np.isfinite(ranges) & (ranges >= msg.range_min) & (ranges <= msg.range_max)
    if not np.any(valid):
        return np.empty((0, 3), dtype=np.float32)
    rr = ranges[valid]
    aa = angles[valid]
    xx = rr * np.cos(aa)
    yy = rr * np.sin(aa)
    zz = np.zeros_like(xx)
    return np.column_stack((xx, yy, zz)).astype(np.float32, copy=False)


def _parse_pointcloud(msg: PointCloud2) -> np.ndarray:
    pts = np.array(list(point_cloud2.read_points(msg, field_names=("x", "y", "z"), skip_nans=True)), dtype=np.float32)
    if pts.size == 0:
        return np.empty((0, 3), dtype=np.float32)
    return pts


def _depth_image_to_meters(msg: Image) -> np.ndarray:
    if msg.encoding in ("16UC1", "mono16"):
        depth = np.frombuffer(msg.data, dtype=np.uint16).reshape(msg.height, msg.width).astype(np.float32)
        depth *= 0.001
        return depth
    if msg.encoding == "32FC1":
        depth = np.frombuffer(msg.data, dtype=np.float32).reshape(msg.height, msg.width).astype(np.float32)
        return depth
    raise RuntimeError(f"Unsupported depth encoding '{msg.encoding}'. Expected 16UC1/mono16/32FC1.")


def _stamp_ns(sec: int, nanosec: int) -> int:
    return sec * 1_000_000_000 + nanosec


def _decode_image_msg_to_bgr(msg: Image) -> np.ndarray:
    h = int(msg.height)
    w = int(msg.width)
    if h <= 0 or w <= 0:
        raise RuntimeError("Image has invalid shape")
    rows = np.frombuffer(msg.data, dtype=np.uint8).reshape(h, int(msg.step))
    enc = msg.encoding
    if enc == "bgr8":
        return rows[:, : w * 3].reshape(h, w, 3).copy()
    if enc == "rgb8":
        rgb = rows[:, : w * 3].reshape(h, w, 3)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    if enc == "mono8":
        gray = rows[:, :w]
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if enc == "bgra8":
        bgra = rows[:, : w * 4].reshape(h, w, 4)
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
    if enc == "rgba8":
        rgba = rows[:, : w * 4].reshape(h, w, 4)
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    raise RuntimeError(f"Unsupported image encoding '{enc}'.")


def _decode_compressed_image_to_bgr(msg: CompressedImage) -> np.ndarray:
    arr = np.frombuffer(msg.data, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("Failed to decode compressed image")
    return frame


def _collect_reference_data(
    bag_path: Path,
    lidar_topic: str,
    camera_info_topic: str,
) -> tuple[tf2_ros.Buffer, CameraIntrinsics, list[LidarFrame]]:
    reader, topic_types = _reader_for_bag(bag_path)
    lidar_type = _ensure_topic(topic_types, lidar_topic, bag_path)
    _ensure_topic(topic_types, camera_info_topic, bag_path)
    tf_cache = Duration(seconds=7200.0)
    tf_buffer = tf2_ros.Buffer(cache_time=tf_cache)
    camera_intrinsics: CameraIntrinsics | None = None
    lidar_frames: list[LidarFrame] = []

    lidar_cls = get_message(lidar_type)

    while reader.has_next():
        topic, serialized, stamp_ns = reader.read_next()
        if topic == "/tf" or topic == "/tf_static":
            tf_msg = deserialize_message(serialized, TFMessage)
            for transform in tf_msg.transforms:
                try:
                    if topic == "/tf_static":
                        tf_buffer.set_transform_static(transform, SOURCE_TAG)
                    else:
                        tf_buffer.set_transform(transform, SOURCE_TAG)
                except Exception:
                    continue
            continue

        if topic == camera_info_topic and camera_intrinsics is None:
            info_msg = deserialize_message(serialized, CameraInfo)
            camera_intrinsics = CameraIntrinsics(
                frame_id=info_msg.header.frame_id,
                width=int(info_msg.width),
                height=int(info_msg.height),
                fx=float(info_msg.k[0]),
                fy=float(info_msg.k[4]),
                cx=float(info_msg.k[2]),
                cy=float(info_msg.k[5]),
            )
            continue

        if topic != lidar_topic:
            continue

        msg = deserialize_message(serialized, lidar_cls)
        header_stamp_ns = _extract_header_stamp_ns(msg) or 0
        if lidar_type == "sensor_msgs/msg/LaserScan":
            points = _parse_laserscan(msg)
            frame_id = msg.header.frame_id
        elif lidar_type == "sensor_msgs/msg/PointCloud2":
            points = _parse_pointcloud(msg)
            frame_id = msg.header.frame_id
        else:
            raise RuntimeError(f"Unsupported lidar type '{lidar_type}'. Use LaserScan or PointCloud2.")

        if points.size == 0:
            continue
        lidar_frames.append(
            LidarFrame(
                stamp_ns=stamp_ns,
                header_stamp_ns=header_stamp_ns,
                frame_id=frame_id,
                points_xyz=points,
            )
        )

    if camera_intrinsics is None:
        raise RuntimeError(f"No CameraInfo found on '{camera_info_topic}' in {bag_path}")
    if not lidar_frames:
        raise RuntimeError(f"No usable lidar frames found on '{lidar_topic}' in {bag_path}")
    return tf_buffer, camera_intrinsics, lidar_frames


def _nearest_lidar_idx(stamps_ns: list[int], target_ns: int) -> int:
    idx = bisect.bisect_left(stamps_ns, target_ns)
    if idx <= 0:
        return 0
    if idx >= len(stamps_ns):
        return len(stamps_ns) - 1
    prev_ns = stamps_ns[idx - 1]
    next_ns = stamps_ns[idx]
    if abs(target_ns - prev_ns) <= abs(next_ns - target_ns):
        return idx - 1
    return idx


def _collect_depth_frames(bag_path: Path, depth_topic: str) -> list[DepthFrame]:
    reader, topic_types = _reader_for_bag(bag_path)
    depth_type = _ensure_topic(topic_types, depth_topic, bag_path)
    if depth_type != "sensor_msgs/msg/Image":
        raise RuntimeError(f"Depth topic '{depth_topic}' in {bag_path} is type '{depth_type}', expected Image.")
    depth_frames: list[DepthFrame] = []
    while reader.has_next():
        topic, serialized, stamp_ns = reader.read_next()
        if topic != depth_topic:
            continue
        depth_msg = deserialize_message(serialized, Image)
        depth_frames.append(
            DepthFrame(
                stamp_ns=int(stamp_ns),
                header_stamp_ns=_stamp_ns(depth_msg.header.stamp.sec, depth_msg.header.stamp.nanosec),
                depth_m=_depth_image_to_meters(depth_msg),
            )
        )
    return depth_frames


def _lookup_transform_with_fallback(
    tf_buffer: tf2_ros.Buffer,
    target_frame: str,
    source_frame: str,
    candidate_stamps_ns: list[int],
    timeout: Duration,
) -> Any | None:
    for candidate_ns in candidate_stamps_ns:
        if candidate_ns <= 0:
            continue
        try:
            return tf_buffer.lookup_transform(
                target_frame,
                source_frame,
                Time(nanoseconds=candidate_ns),
                timeout=timeout,
            )
        except Exception:
            continue
    try:
        return tf_buffer.lookup_transform(
            target_frame,
            source_frame,
            Time(),
            timeout=timeout,
        )
    except Exception:
        return None


def _collect_error_samples(
    bag_path: Path,
    depth_topic: str,
    lidar_topic: str,
    camera_info_topic: str,
    max_lidar_sync_ms: float,
) -> ErrorSamples:
    tf_buffer, intrinsics, lidar_frames = _collect_reference_data(bag_path, lidar_topic, camera_info_topic)
    lidar_stamps = [frame.stamp_ns for frame in lidar_frames]
    max_sync_ns = int(max_lidar_sync_ms * 1e6)
    zero_timeout = Duration(seconds=0.0)

    reader, topic_types = _reader_for_bag(bag_path)
    depth_type = _ensure_topic(topic_types, depth_topic, bag_path)
    if depth_type != "sensor_msgs/msg/Image":
        raise RuntimeError(f"Depth topic '{depth_topic}' in {bag_path} is type '{depth_type}', expected Image.")

    lidar_depth_chunks: list[np.ndarray] = []
    model_depth_chunks: list[np.ndarray] = []
    abs_error_chunks: list[np.ndarray] = []
    rel_error_chunks: list[np.ndarray] = []
    u_chunks: list[np.ndarray] = []
    v_chunks: list[np.ndarray] = []

    while reader.has_next():
        topic, serialized, stamp_ns = reader.read_next()
        if topic != depth_topic:
            continue

        depth_msg = deserialize_message(serialized, Image)
        depth_header_stamp_ns = _stamp_ns(depth_msg.header.stamp.sec, depth_msg.header.stamp.nanosec)
        lidar_idx = _nearest_lidar_idx(lidar_stamps, int(stamp_ns))
        lidar_frame = lidar_frames[lidar_idx]
        if abs(int(stamp_ns) - lidar_frame.stamp_ns) > max_sync_ns:
            continue

        tf_msg = _lookup_transform_with_fallback(
            tf_buffer=tf_buffer,
            target_frame=intrinsics.frame_id,
            source_frame=lidar_frame.frame_id,
            candidate_stamps_ns=[depth_header_stamp_ns, lidar_frame.header_stamp_ns, int(stamp_ns)],
            timeout=zero_timeout,
        )
        if tf_msg is None:
            continue

        depth = _depth_image_to_meters(depth_msg)
        t = tf_msg.transform.translation
        q = tf_msg.transform.rotation
        rot = _quaternion_to_rot_matrix(q.x, q.y, q.z, q.w)
        trans = np.array([t.x, t.y, t.z], dtype=np.float64)
        points_cam = (rot @ lidar_frame.points_xyz.astype(np.float64).T).T + trans

        z = points_cam[:, 2]
        front = z > 0.05
        if not np.any(front):
            continue

        points_cam = points_cam[front]
        z = z[front]
        u = intrinsics.fx * points_cam[:, 0] / z + intrinsics.cx
        v = intrinsics.fy * points_cam[:, 1] / z + intrinsics.cy
        ui = np.rint(u).astype(np.int32)
        vi = np.rint(v).astype(np.int32)

        in_bounds = (ui >= 0) & (ui < depth.shape[1]) & (vi >= 0) & (vi < depth.shape[0])
        if not np.any(in_bounds):
            continue

        ui = ui[in_bounds]
        vi = vi[in_bounds]
        gt_depth = z[in_bounds].astype(np.float32, copy=False)
        pred_depth = depth[vi, ui].astype(np.float32, copy=False)
        valid = np.isfinite(pred_depth) & (pred_depth > 0.0)
        if not np.any(valid):
            continue

        ui = ui[valid]
        vi = vi[valid]
        gt_depth = gt_depth[valid]
        pred_depth = pred_depth[valid]
        abs_err = np.abs(pred_depth - gt_depth)
        rel_err = abs_err / np.maximum(gt_depth, 1e-6)

        u_chunks.append(ui)
        v_chunks.append(vi)
        lidar_depth_chunks.append(gt_depth)
        model_depth_chunks.append(pred_depth)
        abs_error_chunks.append(abs_err)
        rel_error_chunks.append(rel_err.astype(np.float32, copy=False))

    if not abs_error_chunks:
        empty_f32 = np.empty((0,), dtype=np.float32)
        empty_i32 = np.empty((0,), dtype=np.int32)
        return ErrorSamples(
            width=intrinsics.width,
            height=intrinsics.height,
            lidar_depth_m=empty_f32,
            model_depth_m=empty_f32,
            abs_error_m=empty_f32,
            rel_error=empty_f32,
            u_px=empty_i32,
            v_px=empty_i32,
        )

    return ErrorSamples(
        width=intrinsics.width,
        height=intrinsics.height,
        lidar_depth_m=np.concatenate(lidar_depth_chunks).astype(np.float32, copy=False),
        model_depth_m=np.concatenate(model_depth_chunks).astype(np.float32, copy=False),
        abs_error_m=np.concatenate(abs_error_chunks).astype(np.float32, copy=False),
        rel_error=np.concatenate(rel_error_chunks).astype(np.float32, copy=False),
        u_px=np.concatenate(u_chunks).astype(np.int32, copy=False),
        v_px=np.concatenate(v_chunks).astype(np.int32, copy=False),
    )


def _distance_labels(edges_m: list[float]) -> list[str]:
    labels: list[str] = []
    lower = 0.0
    for edge in edges_m:
        labels.append(f"{lower:.2f}-{edge:.2f}")
        lower = edge
    labels.append(f"{edges_m[-1]:.2f}+")
    return labels


def _distance_mask(values: np.ndarray, edges_m: list[float], idx: int) -> np.ndarray:
    lower = 0.0 if idx == 0 else edges_m[idx - 1]
    if idx < len(edges_m):
        upper = edges_m[idx]
        return (values >= lower) & (values < upper)
    return values >= lower


def _stats(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {
            "mean": float("nan"),
            "median": float("nan"),
            "p90": float("nan"),
            "p95": float("nan"),
            "count": 0.0,
        }
    return {
        "mean": float(np.mean(values)),
        "median": float(np.median(values)),
        "p90": float(np.quantile(values, 0.90)),
        "p95": float(np.quantile(values, 0.95)),
        "count": float(values.size),
    }


def _write_distance_stats_csv(
    out_path: Path,
    samples_by_model: dict[str, ErrorSamples],
    edges_m: list[float],
) -> None:
    labels = _distance_labels(edges_m)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["model", "distance_bin_m", "count", "mean_abs_error_m", "median_abs_error_m", "p90_abs_error_m", "p95_abs_error_m"],
        )
        writer.writeheader()
        for model, samples in samples_by_model.items():
            for idx, label in enumerate(labels):
                mask = _distance_mask(samples.lidar_depth_m, edges_m, idx)
                vals = samples.abs_error_m[mask]
                stats = _stats(vals)
                writer.writerow(
                    {
                        "model": model,
                        "distance_bin_m": label,
                        "count": int(stats["count"]),
                        "mean_abs_error_m": stats["mean"],
                        "median_abs_error_m": stats["median"],
                        "p90_abs_error_m": stats["p90"],
                        "p95_abs_error_m": stats["p95"],
                    }
                )


def _write_frame_grid_stats_csv(
    out_path: Path,
    samples_by_model: dict[str, ErrorSamples],
    grid_rows: int,
    grid_cols: int,
) -> None:
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "model",
                "row",
                "col",
                "count",
                "mean_abs_error_m",
                "median_abs_error_m",
                "p90_abs_error_m",
                "p95_abs_error_m",
            ],
        )
        writer.writeheader()
        for model, samples in samples_by_model.items():
            if samples.abs_error_m.size == 0:
                continue
            row_idx = np.clip((samples.v_px.astype(np.float64) * grid_rows / max(samples.height, 1)).astype(np.int32), 0, grid_rows - 1)
            col_idx = np.clip((samples.u_px.astype(np.float64) * grid_cols / max(samples.width, 1)).astype(np.int32), 0, grid_cols - 1)
            for r in range(grid_rows):
                for c in range(grid_cols):
                    mask = (row_idx == r) & (col_idx == c)
                    vals = samples.abs_error_m[mask]
                    stats = _stats(vals)
                    writer.writerow(
                        {
                            "model": model,
                            "row": r,
                            "col": c,
                            "count": int(stats["count"]),
                            "mean_abs_error_m": stats["mean"],
                            "median_abs_error_m": stats["median"],
                            "p90_abs_error_m": stats["p90"],
                            "p95_abs_error_m": stats["p95"],
                        }
                    )


def _plot_distance_error_distribution(
    out_path: Path,
    samples_by_model: dict[str, ErrorSamples],
    edges_m: list[float],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    model_names = list(samples_by_model.keys())
    labels = _distance_labels(edges_m)
    palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    group_span = len(model_names) + 1

    box_data: list[np.ndarray] = []
    box_pos: list[float] = []
    box_colors: list[str] = []
    for bin_idx, _ in enumerate(labels):
        for model_idx, model in enumerate(model_names):
            vals = samples_by_model[model].abs_error_m[_distance_mask(samples_by_model[model].lidar_depth_m, edges_m, bin_idx)]
            if vals.size == 0:
                continue
            box_data.append(vals)
            box_pos.append(bin_idx * group_span + model_idx)
            box_colors.append(palette[model_idx % len(palette)])

    if not box_data:
        raise RuntimeError("No valid error samples were found for distance distribution chart.")

    fig, ax = plt.subplots(figsize=(14, 6))
    bp = ax.boxplot(
        box_data,
        positions=box_pos,
        widths=0.7,
        patch_artist=True,
        showfliers=False,
        medianprops={"color": "black", "linewidth": 1.2},
    )
    for patch, color in zip(bp["boxes"], box_colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.55)
        patch.set_edgecolor(color)

    tick_pos = [idx * group_span + (len(model_names) - 1) / 2.0 for idx in range(len(labels))]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(labels, rotation=20)
    ax.set_xlabel("LiDAR distance bin (m)")
    ax.set_ylabel("Absolute error |M-L| (m)")
    ax.set_title("Error distribution by distance (all valid projected points)")
    ax.grid(axis="y", alpha=0.3)

    legend = [Patch(facecolor=palette[i % len(palette)], edgecolor=palette[i % len(palette)], alpha=0.55, label=name) for i, name in enumerate(model_names)]
    ax.legend(handles=legend, loc="upper left")

    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def _plot_frame_region_heatmap(
    out_path: Path,
    samples_by_model: dict[str, ErrorSamples],
    grid_rows: int,
    grid_cols: int,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    model_names = list(samples_by_model.keys())
    if not model_names:
        raise RuntimeError("No models provided for frame region chart.")

    all_errors = np.concatenate([samples.abs_error_m for samples in samples_by_model.values() if samples.abs_error_m.size > 0])
    if all_errors.size == 0:
        raise RuntimeError("No valid error samples were found for frame region chart.")
    vmax = float(np.quantile(all_errors, 0.95))
    vmax = max(vmax, 0.01)

    fig, axes = plt.subplots(1, len(model_names), figsize=(5 * len(model_names), 4.5), squeeze=False)
    ims = []
    for col, model in enumerate(model_names):
        ax = axes[0][col]
        samples = samples_by_model[model]
        med = np.full((grid_rows, grid_cols), np.nan, dtype=np.float32)
        counts = np.zeros((grid_rows, grid_cols), dtype=np.int32)
        if samples.abs_error_m.size > 0:
            row_idx = np.clip(
                (samples.v_px.astype(np.float64) * grid_rows / max(samples.height, 1)).astype(np.int32),
                0,
                grid_rows - 1,
            )
            col_idx = np.clip(
                (samples.u_px.astype(np.float64) * grid_cols / max(samples.width, 1)).astype(np.int32),
                0,
                grid_cols - 1,
            )
            for r in range(grid_rows):
                for c in range(grid_cols):
                    mask = (row_idx == r) & (col_idx == c)
                    vals = samples.abs_error_m[mask]
                    counts[r, c] = int(vals.size)
                    if vals.size > 0:
                        med[r, c] = float(np.median(vals))

        im = ax.imshow(med, cmap="magma", vmin=0.0, vmax=vmax)
        ims.append(im)
        ax.set_title(model)
        ax.set_xlabel("Frame X region")
        ax.set_ylabel("Frame Y region")
        ax.set_xticks(range(grid_cols))
        ax.set_yticks(range(grid_rows))
        ax.set_xticklabels([str(v + 1) for v in range(grid_cols)])
        ax.set_yticklabels([str(v + 1) for v in range(grid_rows)])

        for r in range(grid_rows):
            for c in range(grid_cols):
                txt = "n=0"
                if counts[r, c] > 0 and np.isfinite(med[r, c]):
                    txt = f"{med[r, c]:.2f}m\nn={counts[r, c]}"
                ax.text(c, r, txt, ha="center", va="center", color="white", fontsize=8)

    cbar = fig.colorbar(ims[0], ax=axes.ravel().tolist(), fraction=0.02, pad=0.04)
    cbar.set_label("Median |M-L| (m)")
    fig.suptitle("Frame-region impact on depth error (all valid projected points)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def charts_from_summary(
    summary_json_path: Path,
    model_filter: list[str] | None,
    output_dir: Path | None,
    max_lidar_sync_ms: float,
    distance_bins_m: list[float],
    grid_rows: int,
    grid_cols: int,
) -> int:
    rows_raw = json.loads(summary_json_path.read_text(encoding="utf-8"))
    if not isinstance(rows_raw, list):
        raise RuntimeError(f"Expected list in summary JSON: {summary_json_path}")
    rows: list[dict[str, Any]] = [row for row in rows_raw if isinstance(row, dict)]

    selected_rows = rows
    if model_filter:
        names = set(model_filter)
        selected_rows = [row for row in rows if str(row.get("model", "")) in names]
    if not selected_rows:
        raise RuntimeError("No models selected for chart generation.")

    out_dir = output_dir if output_dir is not None else summary_json_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    bins = sorted({float(v) for v in distance_bins_m if float(v) > 0.0})
    if not bins:
        bins = [0.5, 1.0, 1.5, 2.0, 3.0]

    samples_by_model: dict[str, ErrorSamples] = {}
    for row in selected_rows:
        model = str(row["model"])
        run_bag = Path(str(row["run_bag"])).expanduser().resolve()
        depth_topic = str(row["depth_topic"])
        lidar_topic = str(row.get("lidar_topic", "/scan"))
        camera_info_topic = str(row.get("camera_info_topic", "/mars/main_camera/left/camera_info"))
        print(f"[{model}] collecting per-point errors from {run_bag}")
        samples = _collect_error_samples(
            bag_path=run_bag,
            depth_topic=depth_topic,
            lidar_topic=lidar_topic,
            camera_info_topic=camera_info_topic,
            max_lidar_sync_ms=max_lidar_sync_ms,
        )
        if samples.abs_error_m.size == 0:
            print(f"[{model}] no valid pairs found; skipping")
            continue
        samples_by_model[model] = samples
        print(f"[{model}] valid projected points: {samples.abs_error_m.size}")

    if not samples_by_model:
        raise RuntimeError("No valid projected points were found for the selected models.")

    stem = summary_json_path.stem
    distance_png = out_dir / f"{stem}_distance_error_distribution.png"
    frame_png = out_dir / f"{stem}_frame_region_error_heatmap.png"
    distance_csv = out_dir / f"{stem}_distance_error_distribution.csv"
    frame_csv = out_dir / f"{stem}_frame_region_error_heatmap.csv"

    _write_distance_stats_csv(distance_csv, samples_by_model, bins)
    _write_frame_grid_stats_csv(frame_csv, samples_by_model, grid_rows=grid_rows, grid_cols=grid_cols)
    _plot_distance_error_distribution(distance_png, samples_by_model, bins)
    _plot_frame_region_heatmap(frame_png, samples_by_model, grid_rows=grid_rows, grid_cols=grid_cols)

    print(f"Distance chart : {distance_png}")
    print(f"Frame chart    : {frame_png}")
    print(f"Distance stats : {distance_csv}")
    print(f"Frame stats    : {frame_csv}")
    return 0


def evaluate_run(
    bag_path: Path,
    depth_topic: str,
    lidar_topic: str,
    camera_info_topic: str,
    max_lidar_sync_ms: float,
) -> dict[str, Any]:
    tf_buffer, intrinsics, lidar_frames = _collect_reference_data(bag_path, lidar_topic, camera_info_topic)
    lidar_stamps = [frame.stamp_ns for frame in lidar_frames]
    max_sync_ns = int(max_lidar_sync_ms * 1e6)

    reader, topic_types = _reader_for_bag(bag_path)
    depth_type = _ensure_topic(topic_types, depth_topic, bag_path)
    if depth_type != "sensor_msgs/msg/Image":
        raise RuntimeError(f"Depth topic '{depth_topic}' in {bag_path} is type '{depth_type}', expected Image.")

    metrics = MetricsAccumulator()
    zero_timeout = Duration(seconds=0.0)

    while reader.has_next():
        topic, serialized, stamp_ns = reader.read_next()
        if topic != depth_topic:
            continue

        metrics.depth_frames_seen += 1
        depth_msg = deserialize_message(serialized, Image)
        depth_header_stamp_ns = _stamp_ns(depth_msg.header.stamp.sec, depth_msg.header.stamp.nanosec)
        lidar_idx = _nearest_lidar_idx(lidar_stamps, stamp_ns)
        lidar_frame = lidar_frames[lidar_idx]
        if abs(stamp_ns - lidar_frame.stamp_ns) > max_sync_ns:
            continue
        metrics.depth_frames_with_sync += 1

        tf_msg = _lookup_transform_with_fallback(
            tf_buffer=tf_buffer,
            target_frame=intrinsics.frame_id,
            source_frame=lidar_frame.frame_id,
            candidate_stamps_ns=[depth_header_stamp_ns, lidar_frame.header_stamp_ns, int(stamp_ns)],
            timeout=zero_timeout,
        )
        if tf_msg is None:
            continue
        metrics.depth_frames_with_transform += 1

        depth = _depth_image_to_meters(depth_msg)

        t = tf_msg.transform.translation
        q = tf_msg.transform.rotation
        rot = _quaternion_to_rot_matrix(q.x, q.y, q.z, q.w)
        trans = np.array([t.x, t.y, t.z], dtype=np.float64)
        points_cam = (rot @ lidar_frame.points_xyz.astype(np.float64).T).T + trans

        z = points_cam[:, 2]
        front = z > 0.05
        if not np.any(front):
            continue
        points_cam = points_cam[front]
        z = z[front]

        u = intrinsics.fx * points_cam[:, 0] / z + intrinsics.cx
        v = intrinsics.fy * points_cam[:, 1] / z + intrinsics.cy
        ui = np.rint(u).astype(np.int32)
        vi = np.rint(v).astype(np.int32)

        in_bounds = (ui >= 0) & (ui < depth.shape[1]) & (vi >= 0) & (vi < depth.shape[0])
        if not np.any(in_bounds):
            continue

        ui = ui[in_bounds]
        vi = vi[in_bounds]
        gt_depth = z[in_bounds]
        pred_depth = depth[vi, ui]
        metrics.projected_points += int(gt_depth.size)

        valid = np.isfinite(pred_depth) & (pred_depth > 0.0)
        if not np.any(valid):
            continue

        metrics.add(gt=gt_depth[valid], pred=pred_depth[valid])

    return metrics.summary()


def _project_lidar_to_image(
    lidar_points_xyz: np.ndarray,
    transform: Any,
    intrinsics: CameraIntrinsics,
    image_shape: tuple[int, int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t = transform.translation
    q = transform.rotation
    rot = _quaternion_to_rot_matrix(q.x, q.y, q.z, q.w)
    trans = np.array([t.x, t.y, t.z], dtype=np.float64)
    points_cam = (rot @ lidar_points_xyz.astype(np.float64).T).T + trans

    z = points_cam[:, 2]
    front = z > 0.05
    if not np.any(front):
        return (
            np.empty((0,), dtype=np.int32),
            np.empty((0,), dtype=np.int32),
            np.empty((0,), dtype=np.float32),
        )

    points_cam = points_cam[front]
    z = z[front]
    u = intrinsics.fx * points_cam[:, 0] / z + intrinsics.cx
    v = intrinsics.fy * points_cam[:, 1] / z + intrinsics.cy
    ui = np.rint(u).astype(np.int32)
    vi = np.rint(v).astype(np.int32)

    h, w = image_shape[0], image_shape[1]
    in_bounds = (ui >= 0) & (ui < w) & (vi >= 0) & (vi < h)
    if not np.any(in_bounds):
        return (
            np.empty((0,), dtype=np.int32),
            np.empty((0,), dtype=np.int32),
            np.empty((0,), dtype=np.float32),
        )
    return ui[in_bounds], vi[in_bounds], z[in_bounds].astype(np.float32, copy=False)


def _draw_lidar_tags(
    frame_bgr: np.ndarray,
    ui: np.ndarray,
    vi: np.ndarray,
    lidar_depth_m: np.ndarray,
    model_depth_m: np.ndarray | None,
    depth_min_m: float,
    depth_max_m: float,
    error_max_m: float,
    max_points_draw: int,
    max_labels: int,
    min_label_spacing_px: int,
) -> np.ndarray:
    if lidar_depth_m.size == 0:
        return frame_bgr

    order = np.argsort(lidar_depth_m)
    if order.size > max_points_draw:
        keep = np.linspace(0, order.size - 1, max_points_draw).astype(np.int32)
        order = order[keep]

    d = lidar_depth_m[order]
    uu = ui[order]
    vv = vi[order]
    pred = model_depth_m[order] if model_depth_m is not None and model_depth_m.size == lidar_depth_m.size else None
    if pred is not None:
        abs_err = np.abs(pred - d)
        norm = np.clip(abs_err / max(error_max_m, 1e-6), 0.0, 1.0)
        red = np.where(norm < 0.5, norm * 2.0 * 255.0, 255.0).astype(np.uint8)
        green = np.where(norm < 0.5, 255.0, (1.0 - norm) * 2.0 * 255.0).astype(np.uint8)
        blue = np.zeros_like(red, dtype=np.uint8)
        colors = np.stack((blue, green, red), axis=1)
    else:
        denom = max(depth_max_m - depth_min_m, 1e-6)
        norm = np.clip((d - depth_min_m) / denom, 0.0, 1.0)
        color_idx = (255.0 * (1.0 - norm)).astype(np.uint8)
        lut = cv2.applyColorMap(np.arange(256, dtype=np.uint8).reshape(256, 1), cv2.COLORMAP_TURBO).reshape(256, 3)
        colors = lut[color_idx]

    annotated = frame_bgr.copy()
    for x, y, color in zip(uu, vv, colors):
        cv2.circle(annotated, (int(x), int(y)), 2, tuple(int(c) for c in color.tolist()), -1, lineType=cv2.LINE_AA)

    selected: list[tuple[int, int, float, float, float]] = []
    min_sq = float(min_label_spacing_px * min_label_spacing_px)
    for idx in np.argsort(d):
        x = int(uu[idx])
        y = int(vv[idx])
        if any((x - sx) * (x - sx) + (y - sy) * (y - sy) < min_sq for sx, sy, _, _, _ in selected):
            continue
        pred_depth = float("nan")
        err_depth = float("nan")
        if pred is not None and np.isfinite(pred[idx]) and pred[idx] > 0.0:
            pred_depth = float(pred[idx])
            err_depth = abs(pred_depth - float(d[idx]))
        selected.append((x, y, float(d[idx]), pred_depth, err_depth))
        if len(selected) >= max_labels:
            break

    labels: list[tuple[int, int, str]] = []
    for x, y, depth, pred_depth, err_depth in selected:
        if np.isfinite(pred_depth):
            text = f"L:{depth:.2f} M:{pred_depth:.2f} D:{err_depth:.2f}"
        else:
            text = f"L:{depth:.2f}m"
        labels.append((x, y, text))

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.4
    thickness = 1
    metrics = [cv2.getTextSize(text, font, font_scale, thickness)[0] for _, _, text in labels]
    if metrics:
        max_w = max(size[0] for size in metrics)
        line_h = max(size[1] for size in metrics) + 8
        h, w = annotated.shape[0], annotated.shape[1]
        capacity_per_side = max(1, (h - 10) // max(line_h, 1))
        max_labels_total = max(2, capacity_per_side * 2)
        labels = labels[:max_labels_total]
        metrics = metrics[:max_labels_total]

        left_items = [(x, y, text, metrics[idx]) for idx, (x, y, text) in enumerate(labels) if x < w // 2]
        right_items = [(x, y, text, metrics[idx]) for idx, (x, y, text) in enumerate(labels) if x >= w // 2]

        def _positions(count: int) -> list[int]:
            if count <= 0:
                return []
            if count == 1:
                return [h // 2]
            top = 10 + line_h // 2
            bottom = max(top, h - 10 - line_h // 2)
            return [int(v) for v in np.linspace(top, bottom, count)]

        left_items.sort(key=lambda item: item[1])
        right_items.sort(key=lambda item: item[1])
        left_y = _positions(len(left_items))
        right_y = _positions(len(right_items))

        for slot_y, (x, y, text, (tw, th)) in zip(left_y, left_items):
            tx = 8
            ty = min(max(th + 2, slot_y + th // 2), h - 2)
            cv2.rectangle(annotated, (tx - 2, ty - th - 2), (tx + tw + 2, ty + 2), (0, 0, 0), -1)
            cv2.putText(annotated, text, (tx, ty), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
            tail = (tx + tw + 4, ty - th // 2)
            cv2.arrowedLine(
                annotated,
                tail,
                (int(x), int(y)),
                (255, 255, 255),
                1,
                cv2.LINE_AA,
                tipLength=0.2,
            )

        for slot_y, (x, y, text, (tw, th)) in zip(right_y, right_items):
            tx = max(8, w - max_w - 10)
            ty = min(max(th + 2, slot_y + th // 2), h - 2)
            cv2.rectangle(annotated, (tx - 2, ty - th - 2), (tx + tw + 2, ty + 2), (0, 0, 0), -1)
            cv2.putText(annotated, text, (tx, ty), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
            tail = (tx - 4, ty - th // 2)
            cv2.arrowedLine(
                annotated,
                tail,
                (int(x), int(y)),
                (255, 255, 255),
                1,
                cv2.LINE_AA,
                tipLength=0.2,
            )

    return annotated


def annotate_run(
    bag_path: Path,
    image_topic: str,
    lidar_topic: str,
    camera_info_topic: str,
    depth_topic: str | None,
    max_lidar_sync_ms: float,
    depth_pairing_window_ms: float,
    output_video: Path | None,
    output_image: Path | None,
    image_frame_index: int,
    video_fps: float,
    max_points_draw: int,
    max_labels: int,
    min_label_spacing_px: int,
    depth_min_m: float,
    depth_max_m: float,
    error_max_m: float,
) -> dict[str, int]:
    tf_buffer, intrinsics, lidar_frames = _collect_reference_data(bag_path, lidar_topic, camera_info_topic)
    lidar_stamps = [frame.stamp_ns for frame in lidar_frames]
    lidar_min_ns = lidar_stamps[0]
    lidar_max_ns = lidar_stamps[-1]
    max_sync_ns = int(max_lidar_sync_ms * 1e6)
    depth_pairing_window_ns = int(max(1.0, depth_pairing_window_ms) * 1e6)
    depth_frames = _collect_depth_frames(bag_path, depth_topic) if depth_topic else []
    depth_stamps = [frame.stamp_ns for frame in depth_frames]

    reader, topic_types = _reader_for_bag(bag_path)
    image_type = _ensure_topic(topic_types, image_topic, bag_path)
    if image_type not in ("sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage"):
        raise RuntimeError(
            f"Unsupported image topic type '{image_type}' on {image_topic}. Use Image or CompressedImage."
        )
    image_cls = get_message(image_type)
    zero_timeout = Duration(seconds=0.0)

    writer: cv2.VideoWriter | None = None
    frame_seen = 0
    frame_written = 0
    sync_hits = 0
    transform_hits = 0
    labeled_hits = 0
    model_overlay_hits = 0
    saved_image = False

    if output_video:
        output_video.parent.mkdir(parents=True, exist_ok=True)
    if output_image:
        output_image.parent.mkdir(parents=True, exist_ok=True)

    try:
        while reader.has_next():
            topic, serialized, bag_stamp_ns = reader.read_next()
            if topic != image_topic:
                continue

            frame_seen += 1
            image_msg = deserialize_message(serialized, image_cls)
            if image_type == "sensor_msgs/msg/Image":
                frame = _decode_image_msg_to_bgr(image_msg)
                msg_stamp_ns = _stamp_ns(image_msg.header.stamp.sec, image_msg.header.stamp.nanosec)
            else:
                frame = _decode_compressed_image_to_bgr(image_msg)
                msg_stamp_ns = _stamp_ns(image_msg.header.stamp.sec, image_msg.header.stamp.nanosec)

            stamp_ns = bag_stamp_ns
            if msg_stamp_ns > 0 and lidar_min_ns - 2_000_000_000 <= msg_stamp_ns <= lidar_max_ns + 2_000_000_000:
                stamp_ns = msg_stamp_ns
            lidar_idx = _nearest_lidar_idx(lidar_stamps, stamp_ns)
            lidar_frame = lidar_frames[lidar_idx]

            annotated = frame
            if abs(stamp_ns - lidar_frame.stamp_ns) <= max_sync_ns:
                sync_hits += 1
                tf_msg = _lookup_transform_with_fallback(
                    tf_buffer=tf_buffer,
                    target_frame=intrinsics.frame_id,
                    source_frame=lidar_frame.frame_id,
                    candidate_stamps_ns=[msg_stamp_ns, lidar_frame.header_stamp_ns, int(stamp_ns)],
                    timeout=zero_timeout,
                )
                if tf_msg is not None:
                    transform_hits += 1
                    ui, vi, depth = _project_lidar_to_image(
                        lidar_points_xyz=lidar_frame.points_xyz,
                        transform=tf_msg.transform,
                        intrinsics=intrinsics,
                        image_shape=frame.shape,
                    )
                    model_depth: np.ndarray | None = None
                    if depth_frames:
                        depth_idx = _nearest_lidar_idx(depth_stamps, int(bag_stamp_ns))
                        depth_frame = depth_frames[depth_idx]
                        if abs(int(bag_stamp_ns) - depth_frame.stamp_ns) <= depth_pairing_window_ns:
                            depth_img = depth_frame.depth_m
                            model_depth_arr = np.full(depth.shape, np.nan, dtype=np.float32)
                            in_depth_bounds = (
                                (ui >= 0)
                                & (ui < depth_img.shape[1])
                                & (vi >= 0)
                                & (vi < depth_img.shape[0])
                            )
                            if np.any(in_depth_bounds):
                                model_depth_arr[in_depth_bounds] = depth_img[vi[in_depth_bounds], ui[in_depth_bounds]]
                                valid_model = np.isfinite(model_depth_arr) & (model_depth_arr > 0.0)
                                if np.any(valid_model):
                                    model_overlay_hits += 1
                                    model_depth = model_depth_arr
                    if depth.size > 0:
                        labeled_hits += 1
                    annotated = _draw_lidar_tags(
                        frame_bgr=frame,
                        ui=ui,
                        vi=vi,
                        lidar_depth_m=depth,
                        model_depth_m=model_depth,
                        depth_min_m=depth_min_m,
                        depth_max_m=depth_max_m,
                        error_max_m=error_max_m,
                        max_points_draw=max_points_draw,
                        max_labels=max_labels,
                        min_label_spacing_px=min_label_spacing_px,
                    )

            if output_video:
                if writer is None:
                    h, w = annotated.shape[0], annotated.shape[1]
                    writer = cv2.VideoWriter(
                        str(output_video),
                        cv2.VideoWriter_fourcc(*"mp4v"),
                        float(video_fps),
                        (w, h),
                    )
                    if not writer.isOpened():
                        raise RuntimeError(f"Failed to open video writer for {output_video}")
                writer.write(annotated)
                frame_written += 1

            if output_image and not saved_image and frame_seen >= image_frame_index:
                if not cv2.imwrite(str(output_image), annotated):
                    raise RuntimeError(f"Failed to write image to {output_image}")
                saved_image = True

        if output_image and not saved_image:
            raise RuntimeError(f"image_frame_index {image_frame_index} exceeds available frames ({frame_seen})")
    finally:
        if writer is not None:
            writer.release()

    return {
        "frames_seen": frame_seen,
        "frames_written": frame_written,
        "frames_with_lidar_sync": sync_hits,
        "frames_with_transform": transform_hits,
        "frames_with_labels": labeled_hits,
        "frames_with_model_overlay": model_overlay_hits,
    }


def _parse_models(config: dict[str, Any]) -> list[ModelSpec]:
    defaults = config.get("defaults", {})
    default_lidar = str(defaults.get("lidar_topic", "/lidar"))
    default_camera_info = str(defaults.get("camera_info_topic", "/left/camera_info"))
    default_input_camera = str(defaults.get("input_camera_topic", "/left/image_raw"))
    default_inference_topic = defaults.get("inference_topic")
    default_disparity_topic = defaults.get("disparity_topic")
    default_pointcloud_topic = defaults.get("pointcloud_topic")
    default_costmap_topic = defaults.get("costmap_topic")
    default_warmup = float(defaults.get("warmup_sec", 6.0))
    default_settle = float(defaults.get("settle_sec", 2.0))

    models: list[ModelSpec] = []
    for raw in config.get("models", []):
        if not raw.get("enabled", True):
            continue
        name = str(raw["name"])
        depth_topic = str(raw["depth_topic"])
        lidar_topic = str(raw.get("lidar_topic", default_lidar))
        camera_info_topic = str(raw.get("camera_info_topic", default_camera_info))
        input_camera_topic = str(raw.get("input_camera_topic", default_input_camera))
        inference_topic_raw = raw.get("inference_topic", default_inference_topic)
        inference_topic = str(inference_topic_raw) if inference_topic_raw else depth_topic
        disparity_topic_raw = raw.get("disparity_topic", default_disparity_topic)
        disparity_topic = str(disparity_topic_raw) if disparity_topic_raw else None
        pointcloud_topic_raw = raw.get("pointcloud_topic", default_pointcloud_topic)
        pointcloud_topic = str(pointcloud_topic_raw) if pointcloud_topic_raw else None
        costmap_topic_raw = raw.get("costmap_topic", default_costmap_topic)
        costmap_topic = str(costmap_topic_raw) if costmap_topic_raw else None
        tegrastats_log_raw = raw.get("tegrastats_log")
        tegrastats_log = Path(tegrastats_log_raw).expanduser().resolve() if tegrastats_log_raw else None
        launch_command = raw.get("launch_command")
        run_bag_raw = raw.get("run_bag")
        run_bag = Path(run_bag_raw).expanduser().resolve() if run_bag_raw else None
        warmup = float(raw.get("warmup_sec", default_warmup))
        settle = float(raw.get("settle_sec", default_settle))
        models.append(
            ModelSpec(
                name=name,
                depth_topic=depth_topic,
                lidar_topic=lidar_topic,
                camera_info_topic=camera_info_topic,
                input_camera_topic=input_camera_topic,
                inference_topic=inference_topic,
                disparity_topic=disparity_topic,
                pointcloud_topic=pointcloud_topic,
                costmap_topic=costmap_topic,
                tegrastats_log=tegrastats_log,
                launch_command=str(launch_command) if launch_command else None,
                run_bag=run_bag,
                warmup_sec=warmup,
                settle_sec=settle,
            )
        )
    return models


def _make_output_dir(path_text: str | None) -> Path:
    if path_text:
        out = Path(path_text).expanduser().resolve()
    else:
        out = Path.cwd() / "stereo_depth_benchmark_runs"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _sanitize_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name).strip("_")


def _bag_timestamp_tag(canonical_bag: Path) -> str:
    match = re.search(r"(20\d{6}_\d{6})", canonical_bag.name)
    if match:
        return match.group(1)
    return "unknown_bag_time"


def _models_slug(models: list[ModelSpec], max_chars: int = 96) -> str:
    parts = [_sanitize_name(model.name) for model in models if _sanitize_name(model.name)]
    if not parts:
        return "models"
    slug = "_vs_".join(parts)
    if len(slug) <= max_chars:
        return slug
    kept: list[str] = []
    chars = 0
    for idx, part in enumerate(parts):
        add_len = len(part) if idx == 0 else len("_vs_") + len(part)
        if chars + add_len > max_chars:
            break
        kept.append(part)
        chars += add_len
    if not kept:
        return slug[:max_chars]
    remaining = len(parts) - len(kept)
    if remaining > 0:
        return f"{'_vs_'.join(kept)}_plus_{remaining}"
    return "_vs_".join(kept)


def _run_folder_name(models: list[ModelSpec], canonical_bag: Path, run_stamp: str) -> str:
    return f"{_models_slug(models)}__bag_{_bag_timestamp_tag(canonical_bag)}__run_{run_stamp}"


def _prepare_run_output_dir(output_root: Path, models: list[ModelSpec], canonical_bag: Path) -> Path:
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = _run_folder_name(models=models, canonical_bag=canonical_bag, run_stamp=run_stamp)
    run_dir = output_root / candidate
    suffix = 2
    while run_dir.exists():
        run_dir = output_root / f"{candidate}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _resolve_tegrastats_log_path(model: ModelSpec, run_bag: Path) -> Path | None:
    if model.tegrastats_log is not None and model.tegrastats_log.exists():
        return model.tegrastats_log
    sibling = run_bag.parent / f"{run_bag.name}_tegrastats.log"
    if sibling.exists():
        return sibling
    return None


def _active_nodes() -> list[str]:
    try:
        out = subprocess.check_output(["ros2", "node", "list"], text=True, stderr=subprocess.STDOUT)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("WARNING:")]


def _validate_graph_isolation(allow_live_graph: bool) -> None:
    if allow_live_graph:
        return
    nodes = _active_nodes()
    if not nodes:
        return
    conflict_markers = (
        "/main_camera_driver",
        "/camera_container",
        "/stereo_depth_estimator",
        "/robot_state_publisher",
        "/stereo_calibration_manager",
    )
    active_conflicts = sorted({n for n in nodes if any(marker in n for marker in conflict_markers)})
    if not active_conflicts:
        return
    conflict_text = ", ".join(active_conflicts)
    raise RuntimeError(
        "Benchmark must run in an isolated ROS graph. Active live nodes detected: "
        f"{conflict_text}. Run 'innate service stop' and re-run, or use --allow-live-graph."
    )


def run_model_trial(
    canonical_bag: Path,
    model: ModelSpec,
    output_dir: Path,
    playback_rate: float,
    record_topics: list[str],
    enable_tegrastats: bool,
    tegrastats_interval_ms: int,
) -> tuple[Path, dict[str, Any]]:
    if not model.launch_command:
        raise RuntimeError(f"Model '{model.name}' is missing launch_command")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_bag = output_dir / f"{stamp}_{_sanitize_name(model.name)}"
    tegrastats_log = output_dir / f"{run_bag.name}_tegrastats.log"
    model_topics = [
        model.depth_topic,
        model.lidar_topic,
        model.camera_info_topic,
        model.input_camera_topic,
        model.inference_topic,
    ]
    for opt in (model.disparity_topic, model.pointcloud_topic, model.costmap_topic):
        if opt:
            model_topics.append(opt)
    topics = list(dict.fromkeys(record_topics + model_topics))

    tegra_session: TegrastatsSession | None = None
    if enable_tegrastats:
        tegra_session = _start_tegrastats(tegrastats_log, tegrastats_interval_ms)
        if tegra_session is None:
            print(f"[{model.name}] tegrastats unavailable; continuing without hardware telemetry")

    print(f"[{model.name}] launch: {model.launch_command}")
    model_proc = subprocess.Popen(
        model.launch_command,
        shell=True,
        executable="/bin/bash",
        preexec_fn=os.setsid,
    )
    record_proc: subprocess.Popen[Any] | None = None
    try:
        time.sleep(model.warmup_sec)
        if model_proc.poll() is not None:
            raise RuntimeError(f"[{model.name}] launch process exited before replay started")
        record_cmd = ["ros2", "bag", "record", "-o", str(run_bag), *topics]
        print(f"[{model.name}] record: {' '.join(record_cmd)}")
        record_proc = subprocess.Popen(record_cmd, preexec_fn=os.setsid)
        time.sleep(1.0)

        play_cmd = ["ros2", "bag", "play", str(canonical_bag), "--clock", "--rate", str(playback_rate)]
        print(f"[{model.name}] play: {' '.join(play_cmd)}")
        play_rc = subprocess.call(play_cmd)
        if play_rc != 0:
            raise RuntimeError(f"[{model.name}] bag play failed with exit code {play_rc}")

        time.sleep(model.settle_sec)
        if record_proc is not None:
            _stop_process(record_proc, f"{model.name}/record")
        _stop_process(model_proc, f"{model.name}/launch")
    except BaseException:
        if record_proc is not None:
            _stop_process(record_proc, f"{model.name}/record")
        _stop_process(model_proc, f"{model.name}/launch")
        _stop_tegrastats(tegra_session)
        raise

    _stop_tegrastats(tegra_session)
    tegrastats_summary = _parse_tegrastats_log(tegrastats_log) if tegra_session is not None else {
        "tegrastats_available": False,
        "tegrastats_log": "",
        "thermal_throttling_detected": False,
    }
    return run_bag, tegrastats_summary


def _write_summary(output_dir: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"stereo_depth_benchmark_{stamp}.json"
    csv_path = output_dir / f"stereo_depth_benchmark_{stamp}.csv"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    preferred = [
        "model",
        "run_bag",
        "depth_topic",
        "lidar_topic",
        "camera_info_topic",
        "input_camera_topic",
        "inference_topic",
        "disparity_topic",
        "pointcloud_topic",
        "costmap_topic",
        "inference_latency_median_ms",
        "inference_latency_p95_ms",
        "inference_latency_p99_ms",
        "inference_effective_fps",
        "camera_to_disparity_latency_median_ms",
        "camera_to_disparity_latency_p95_ms",
        "camera_to_disparity_latency_p99_ms",
        "camera_to_pointcloud_latency_median_ms",
        "camera_to_pointcloud_latency_p95_ms",
        "camera_to_pointcloud_latency_p99_ms",
        "camera_to_costmap_latency_median_ms",
        "camera_to_costmap_latency_p95_ms",
        "camera_to_costmap_latency_p99_ms",
        "dropped_frames",
        "dropped_frames_pct",
        "mae_m",
        "rmse_m",
        "median_abs_error_m",
        "p90_abs_error_m",
        "mean_relative_error",
        "pct_within_5cm",
        "pct_within_10cm",
        "valid_pairs",
        "projected_points",
        "depth_frames_seen",
        "depth_frames_with_sync",
        "depth_frames_with_transform",
        "cpu_util_median_pct",
        "cpu_util_p95_pct",
        "cpu_util_p99_pct",
        "gpu_util_median_pct",
        "gpu_util_p95_pct",
        "gpu_util_p99_pct",
        "ram_used_median_mb",
        "ram_used_p95_mb",
        "gpu_mem_used_median_mb",
        "gpu_mem_used_p95_mb",
        "power_median_w",
        "power_p95_w",
        "power_p99_w",
        "max_temperature_c",
        "thermal_throttling_detected",
        "tegrastats_available",
        "tegrastats_samples",
        "tegrastats_log",
    ]
    extra_keys = sorted({key for row in rows for key in row.keys() if key not in preferred})
    fieldnames = preferred + extra_keys
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fieldnames})

    return json_path, csv_path


def _print_row(row: dict[str, Any]) -> None:
    print(
        f"{row['model']}: "
        f"inference P95={row.get('inference_latency_p95_ms', float('nan')):.1f}ms "
        f"(median={row.get('inference_latency_median_ms', float('nan')):.1f}ms, "
        f"P99={row.get('inference_latency_p99_ms', float('nan')):.1f}ms), "
        f"FPS={row.get('inference_effective_fps', float('nan')):.2f}, "
        f"costmap P95={row.get('camera_to_costmap_latency_p95_ms', float('nan')):.1f}ms, "
        f"GPU P95={row.get('gpu_util_p95_pct', float('nan')):.1f}%, "
        f"power P95={row.get('power_p95_w', float('nan')):.2f}W, "
        f"dropped={row.get('dropped_frames', 0)} ({row.get('dropped_frames_pct', float('nan')):.1f}%), "
        f"depth RMSE={row.get('rmse_m', float('nan')):.3f}m"
    )


def run_from_config(config_path: Path, no_evaluate: bool, allow_live_graph: bool) -> int:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    _validate_graph_isolation(allow_live_graph=allow_live_graph)
    canonical_bag = Path(str(config["canonical_bag"])).expanduser().resolve()
    if not canonical_bag.exists():
        raise RuntimeError(f"Canonical bag does not exist: {canonical_bag}")

    output_dir = _make_output_dir(config.get("output_dir"))
    playback_rate = float(config.get("playback_rate", 1.0))
    record_topics = list(config.get("record_topics", CANONICAL_TOPICS))
    perf_cfg = config.get("performance", {})
    telemetry_cfg = config.get("telemetry", {})
    header_match_tolerance_ms = float(perf_cfg.get("header_match_tolerance_ms", 5.0))
    pairing_window_ms = float(perf_cfg.get("camera_pairing_window_ms", 500.0))
    enable_tegrastats = bool(telemetry_cfg.get("enable_tegrastats", True))
    tegrastats_interval_ms = int(telemetry_cfg.get("tegrastats_interval_ms", 500))

    models = _parse_models(config)
    if not models:
        raise RuntimeError("No enabled models in config.")
    run_output_dir = _prepare_run_output_dir(output_root=output_dir, models=models, canonical_bag=canonical_bag)
    print(f"Run output dir: {run_output_dir}")

    max_lidar_sync_ms = float(config.get("max_lidar_sync_ms", 80.0))
    rows: list[dict[str, Any]] = []
    for model in models:
        run_bag, tegrastats_summary = run_model_trial(
            canonical_bag=canonical_bag,
            model=model,
            output_dir=run_output_dir,
            playback_rate=playback_rate,
            record_topics=record_topics,
            enable_tegrastats=enable_tegrastats,
            tegrastats_interval_ms=tegrastats_interval_ms,
        )
        print(f"[{model.name}] run bag: {run_bag}")
        runtime_metrics = _build_runtime_performance_metrics(
            bag_path=run_bag,
            input_camera_topic=model.input_camera_topic,
            inference_topic=model.inference_topic,
            disparity_topic=model.disparity_topic,
            pointcloud_topic=model.pointcloud_topic,
            costmap_topic=model.costmap_topic,
            header_match_tolerance_ms=header_match_tolerance_ms,
            pairing_window_ms=pairing_window_ms,
        )
        if no_evaluate:
            row = {
                "model": model.name,
                "run_bag": str(run_bag),
                "depth_topic": model.depth_topic,
                "lidar_topic": model.lidar_topic,
                "camera_info_topic": model.camera_info_topic,
                **runtime_metrics,
                **tegrastats_summary,
            }
            rows.append(row)
            _print_row(row)
            continue

        metrics = evaluate_run(
            bag_path=run_bag,
            depth_topic=model.depth_topic,
            lidar_topic=model.lidar_topic,
            camera_info_topic=model.camera_info_topic,
            max_lidar_sync_ms=max_lidar_sync_ms,
        )
        row = {
            "model": model.name,
            "run_bag": str(run_bag),
            "depth_topic": model.depth_topic,
            "lidar_topic": model.lidar_topic,
            "camera_info_topic": model.camera_info_topic,
            **runtime_metrics,
            **tegrastats_summary,
            **metrics,
        }
        rows.append(row)
        _print_row(row)

    if rows:
        json_path, csv_path = _write_summary(run_output_dir, rows)
        print(f"Summary JSON: {json_path}")
        print(f"Summary CSV : {csv_path}")
    return 0


def evaluate_from_config(config_path: Path) -> int:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    models = _parse_models(config)
    if not models:
        raise RuntimeError("No enabled models in config.")

    output_dir = _make_output_dir(config.get("output_dir"))
    max_lidar_sync_ms = float(config.get("max_lidar_sync_ms", 80.0))
    perf_cfg = config.get("performance", {})
    header_match_tolerance_ms = float(perf_cfg.get("header_match_tolerance_ms", 5.0))
    pairing_window_ms = float(perf_cfg.get("camera_pairing_window_ms", 500.0))
    rows: list[dict[str, Any]] = []

    for model in models:
        if model.run_bag is None:
            print(f"[{model.name}] skipped: set run_bag in config for evaluate mode")
            continue
        if not model.run_bag.exists():
            print(f"[{model.name}] skipped: run_bag path does not exist: {model.run_bag}")
            continue

        metrics = evaluate_run(
            bag_path=model.run_bag,
            depth_topic=model.depth_topic,
            lidar_topic=model.lidar_topic,
            camera_info_topic=model.camera_info_topic,
            max_lidar_sync_ms=max_lidar_sync_ms,
        )
        runtime_metrics = _build_runtime_performance_metrics(
            bag_path=model.run_bag,
            input_camera_topic=model.input_camera_topic,
            inference_topic=model.inference_topic,
            disparity_topic=model.disparity_topic,
            pointcloud_topic=model.pointcloud_topic,
            costmap_topic=model.costmap_topic,
            header_match_tolerance_ms=header_match_tolerance_ms,
            pairing_window_ms=pairing_window_ms,
        )
        tegra_log = _resolve_tegrastats_log_path(model, model.run_bag)
        tegrastats_summary = _parse_tegrastats_log(tegra_log) if tegra_log is not None else {
            "tegrastats_available": False,
            "tegrastats_log": "",
            "thermal_throttling_detected": False,
        }
        row = {
            "model": model.name,
            "run_bag": str(model.run_bag),
            "depth_topic": model.depth_topic,
            "lidar_topic": model.lidar_topic,
            "camera_info_topic": model.camera_info_topic,
            **runtime_metrics,
            **tegrastats_summary,
            **metrics,
        }
        rows.append(row)
        _print_row(row)

    if not rows:
        print("No runs evaluated.")
        return 1
    json_path, csv_path = _write_summary(output_dir, rows)
    print(f"Summary JSON: {json_path}")
    print(f"Summary CSV : {csv_path}")
    return 0


def record_canonical(output_bag: Path, topics: list[str]) -> int:
    output_bag.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ros2", "bag", "record", "-o", str(output_bag), *topics]
    print("Recording canonical dataset. Stop with Ctrl-C.")
    print(" ".join(cmd))
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Canonical stereo-depth benchmark runner and evaluator.")
    sub = parser.add_subparsers(dest="command", required=True)

    record_cmd = sub.add_parser("record-canonical", help="Record one canonical dataset bag.")
    record_cmd.add_argument("--output-bag", required=True, help="Output bag directory path.")
    record_cmd.add_argument(
        "--topic",
        action="append",
        dest="topics",
        help="Topic to record. Repeat to add multiple topics. Defaults to canonical topic set.",
    )

    run_cmd = sub.add_parser("run", help="Replay canonical bag through each model launch command.")
    run_cmd.add_argument("--config", required=True, help="Path to benchmark YAML config.")
    run_cmd.add_argument("--no-evaluate", action="store_true", help="Only run/record, skip metric evaluation.")
    run_cmd.add_argument(
        "--allow-live-graph",
        action="store_true",
        help="Allow running while live camera/TF nodes are active (not recommended for benchmarks).",
    )

    eval_cmd = sub.add_parser("evaluate", help="Evaluate already-recorded model run bags from config.")
    eval_cmd.add_argument("--config", required=True, help="Path to benchmark YAML config.")

    charts_cmd = sub.add_parser("charts", help="Build depth error charts from benchmark summary JSON.")
    charts_cmd.add_argument("--summary-json", required=True, help="Path to stereo_depth_benchmark_*.json.")
    charts_cmd.add_argument("--output-dir", help="Directory for output charts and CSV stats.")
    charts_cmd.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Model name from summary JSON. Repeat to compare specific models.",
    )
    charts_cmd.add_argument(
        "--max-lidar-sync-ms",
        type=float,
        default=80.0,
        help="Max lidar/depth sync gap used when collecting per-point samples.",
    )
    charts_cmd.add_argument(
        "--distance-bin-m",
        action="append",
        dest="distance_bins_m",
        type=float,
        help="Distance bin upper edge in meters. Repeat for multiple edges.",
    )
    charts_cmd.add_argument("--grid-rows", type=int, default=3, help="Frame-region grid rows.")
    charts_cmd.add_argument("--grid-cols", type=int, default=4, help="Frame-region grid columns.")

    annotate_cmd = sub.add_parser("annotate", help="Render lidar depth tags on video or static image from a run bag.")
    annotate_cmd.add_argument("--bag", required=True, help="Path to run bag directory.")
    annotate_cmd.add_argument("--image-topic", required=True, help="Image topic used as annotation backdrop.")
    annotate_cmd.add_argument("--lidar-topic", default="/lidar", help="Reference lidar topic.")
    annotate_cmd.add_argument("--camera-info-topic", default="/left/camera_info", help="CameraInfo topic.")
    annotate_cmd.add_argument("--depth-topic", help="Model depth topic to overlay lidar-vs-model error coloring.")
    annotate_cmd.add_argument("--max-lidar-sync-ms", type=float, default=80.0, help="Max image/lidar sync gap (ms).")
    annotate_cmd.add_argument(
        "--depth-pairing-window-ms",
        type=float,
        default=300.0,
        help="Max image/depth bag-time pairing gap (ms) for model overlay.",
    )
    annotate_cmd.add_argument("--output-video", help="Output MP4 path.")
    annotate_cmd.add_argument("--output-image", help="Output PNG/JPG path.")
    annotate_cmd.add_argument(
        "--image-frame-index",
        type=int,
        default=1,
        help="1-based frame index used for --output-image.",
    )
    annotate_cmd.add_argument("--video-fps", type=float, default=10.0, help="Output video FPS.")
    annotate_cmd.add_argument("--max-points-draw", type=int, default=1200, help="Max lidar points drawn per frame.")
    annotate_cmd.add_argument("--max-labels", type=int, default=24, help="Max depth text tags per frame.")
    annotate_cmd.add_argument(
        "--min-label-spacing-px",
        type=int,
        default=36,
        help="Minimum pixel spacing between labels.",
    )
    annotate_cmd.add_argument("--depth-min-m", type=float, default=0.25, help="Min depth for color scaling.")
    annotate_cmd.add_argument("--depth-max-m", type=float, default=3.0, help="Max depth for color scaling.")
    annotate_cmd.add_argument(
        "--error-max-m",
        type=float,
        default=1.0,
        help="Absolute depth error (meters) mapped to max error color when --depth-topic is set.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "record-canonical":
            topics = args.topics if args.topics else list(CANONICAL_TOPICS)
            return record_canonical(Path(args.output_bag).expanduser().resolve(), topics)
        if args.command == "run":
            return run_from_config(
                Path(args.config).expanduser().resolve(),
                no_evaluate=args.no_evaluate,
                allow_live_graph=bool(args.allow_live_graph),
            )
        if args.command == "evaluate":
            return evaluate_from_config(Path(args.config).expanduser().resolve())
        if args.command == "charts":
            out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else None
            bins = args.distance_bins_m if args.distance_bins_m else [0.5, 1.0, 1.5, 2.0, 3.0]
            return charts_from_summary(
                summary_json_path=Path(args.summary_json).expanduser().resolve(),
                model_filter=list(args.models) if args.models else None,
                output_dir=out_dir,
                max_lidar_sync_ms=float(args.max_lidar_sync_ms),
                distance_bins_m=[float(v) for v in bins],
                grid_rows=max(1, int(args.grid_rows)),
                grid_cols=max(1, int(args.grid_cols)),
            )
        if args.command == "annotate":
            output_video = Path(args.output_video).expanduser().resolve() if args.output_video else None
            output_image = Path(args.output_image).expanduser().resolve() if args.output_image else None
            if output_video is None and output_image is None:
                raise RuntimeError("Provide at least one output target: --output-video and/or --output-image")
            stats = annotate_run(
                bag_path=Path(args.bag).expanduser().resolve(),
                image_topic=str(args.image_topic),
                lidar_topic=str(args.lidar_topic),
                camera_info_topic=str(args.camera_info_topic),
                depth_topic=str(args.depth_topic) if args.depth_topic else None,
                max_lidar_sync_ms=float(args.max_lidar_sync_ms),
                depth_pairing_window_ms=float(args.depth_pairing_window_ms),
                output_video=output_video,
                output_image=output_image,
                image_frame_index=max(1, int(args.image_frame_index)),
                video_fps=float(args.video_fps),
                max_points_draw=max(1, int(args.max_points_draw)),
                max_labels=max(1, int(args.max_labels)),
                min_label_spacing_px=max(1, int(args.min_label_spacing_px)),
                depth_min_m=float(args.depth_min_m),
                depth_max_m=float(args.depth_max_m),
                error_max_m=float(args.error_max_m),
            )
            print(
                "Annotated frames: "
                f"seen={stats['frames_seen']} written={stats['frames_written']} "
                f"sync={stats['frames_with_lidar_sync']} tf={stats['frames_with_transform']} "
                f"labeled={stats['frames_with_labels']} model_overlay={stats['frames_with_model_overlay']}"
            )
            if output_video:
                print(f"Tagged video: {output_video}")
            if output_image:
                print(f"Tagged image: {output_image}")
            return 0
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
