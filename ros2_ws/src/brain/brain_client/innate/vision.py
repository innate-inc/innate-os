#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Stateless vision helpers: decode, Gemini parse, LK track, color seg.

Import as ``from innate import vision`` (moved from workspace/skill_lib)."""

import base64
import json
import math
import re
from typing import Any

import cv2
import numpy as np

from innate.geometry import IMG_H, IMG_W


def b64_to_gray(image_b64):
    """base64 JPEG -> gray ndarray, or None."""
    try:
        arr = np.frombuffer(base64.b64decode(image_b64), np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    except Exception:  # noqa: BLE001 — a bad frame just skips a cycle
        return None


def b64_to_hsv(image_b64):
    """base64 JPEG -> HSV ndarray, or None."""
    try:
        arr = np.frombuffer(base64.b64decode(image_b64), np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV) if bgr is not None else None


def parse_dets(text):
    """Parse Gemini JSON list of detection dicts."""
    if not text:
        return []
    text = re.sub(r"```(?:json)?", "", text)
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        dets = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [d for d in dets if isinstance(d, dict)]


def _norm1k(v):
    """Gemini's normalized 0-1000 coord -> clamped float, or None when it is
    not a number. Clamped because the values drift slightly out of range and
    downstream slicing/CamShift must never see negative or off-image pixels;
    None because every match is now parsed, so one `null` coordinate late in
    the reply must not sink the usable matches ahead of it."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return min(1000.0, max(0.0, float(v)))


def _box_corners_px(det):
    """box_2d [ymin,xmin,ymax,xmax] 0-1000 -> (x0,y0,x1,y1) px."""
    b = det.get("box_2d")
    if not isinstance(b, (list, tuple)) or len(b) < 4:
        return None
    y0, x0, y1, x1 = (_norm1k(v) for v in b[:4])
    if y0 is None or x0 is None or y1 is None or x1 is None:
        return None
    y0, y1 = sorted((y0, y1))
    x0, x1 = sorted((x0, x1))
    return (x0 / 1000.0 * IMG_W, y0 / 1000.0 * IMG_H, x1 / 1000.0 * IMG_W, y1 / 1000.0 * IMG_H)


def _grasp_px(det):
    """grasp_point [y,x] 0-1000 -> (u, v) px, or None."""
    gp = det.get("grasp_point")
    if not isinstance(gp, (list, tuple)) or len(gp) < 2:
        return None
    y, x = _norm1k(gp[0]), _norm1k(gp[1])
    if x is None or y is None:
        return None
    return (x / 1000.0 * IMG_W, y / 1000.0 * IMG_H)


def _grip(det):
    """grip_strength as a float, or None when absent or not a number."""
    g = det.get("grip_strength")
    if isinstance(g, bool) or not isinstance(g, (int, float)) or not math.isfinite(g):
        return None
    return float(g)


def _center_px(det):
    c = _box_corners_px(det)
    return ((c[0] + c[2]) / 2.0, (c[1] + c[3]) / 2.0) if c else None


def parse_det_cands(text):
    """All detections -> [(u, v, grip_strength | None)], best first.
    (u, v) is the grasp_point when given, else the box center."""
    cands = []
    for det in parse_dets(text):
        px = _grasp_px(det) or _center_px(det)
        if px is None:
            continue
        cands.append((px[0], px[1], _grip(det)))
    return cands


def parse_det_px(text):
    """Best (u, v) from a Gemini detection reply."""
    cands = parse_det_cands(text)
    return (cands[0][0], cands[0][1]) if cands else None


def parse_det_grip(text):
    """grip_strength (float) from the best detection, or None."""
    for det in parse_dets(text):
        g = _grip(det)
        if g is not None:
            return g
    return None


def parse_det_boxes(text):
    """All (x, y, w, h) boxes from a Gemini detection reply, best first."""
    boxes = []
    for det in parse_dets(text):
        c = _box_corners_px(det)
        if not c:
            continue
        x, y = int(c[0]), int(c[1])
        w, h = int(c[2]) - x, int(c[3]) - y
        if w >= 8 and h >= 8:
            boxes.append((x, y, w, h))
    return boxes


def parse_det_box(text):
    """Best (x, y, w, h) from a Gemini detection reply."""
    boxes = parse_det_boxes(text)
    return boxes[0] if boxes else None


LK_PARAMS: dict[str, Any] = dict(
    winSize=(21, 21),
    maxLevel=3,
    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03),
)


def grid_pts(u, v, step=12, n=2):
    """(2n+1)^2 LK feature patch around (u,v)."""
    pts = [[u + dx * step, v + dy * step] for dx in range(-n, n + 1) for dy in range(-n, n + 1)]
    return np.array(pts, dtype=np.float32).reshape(-1, 1, 2)


def track_point(prev_gray, gray, grid):
    """One LK step -> median pixel, or None if lost."""
    # nextPts=None is the standard cv2 idiom; the stubs demand an array.
    nxt, status, _ = cv2.calcOpticalFlowPyrLK(
        prev_gray,
        gray,
        grid,
        None,  # pyright: ignore[reportCallIssue, reportArgumentType]
        **LK_PARAMS,
    )
    if nxt is None or status is None:
        return None
    good = nxt[status.flatten() == 1].reshape(-1, 2)
    if len(good) < 3:
        return None
    return float(np.median(good[:, 0])), float(np.median(good[:, 1]))


# Color seg for growing/deforming objects (LK slides off fabric during descent).
_SEG_BINS = [16, 8, 8]
_SEG_RANGES = [0, 180, 0, 256, 0, 256]


def seg_model(hsv, box):
    """Object/floor hist-ratio LUT for back-projection, or None."""
    x, y, w, h = box
    obj = hsv[y : y + h, x : x + w]
    rx0, ry0 = max(0, x - w // 2), max(0, y - h // 2)
    ring = hsv[ry0 : y + h + h // 2, rx0 : x + w + w // 2]
    h_obj = cv2.calcHist([obj], [0, 1, 2], None, _SEG_BINS, _SEG_RANGES)
    h_ring = cv2.calcHist([ring], [0, 1, 2], None, _SEG_BINS, _SEG_RANGES)
    ratio = h_obj / (np.maximum(h_ring - h_obj, 0.0) + 1.0)
    if ratio.max() <= 0:
        return None
    return (255.0 * ratio / ratio.max()).astype(np.uint8)


def seg_track(hsv, model, window, min_score=25.0):
    """Back-project + CamShift -> (center|None, window, score).
    Numpy bin lookup (cv2.calcBackProject broken for 3-D hist here)."""
    ih = (hsv[:, :, 0].astype(np.int32) * _SEG_BINS[0]) // 180
    i_s = (hsv[:, :, 1].astype(np.int32) * _SEG_BINS[1]) // 256
    iv = (hsv[:, :, 2].astype(np.int32) * _SEG_BINS[2]) // 256
    bp = model[np.clip(ih, 0, _SEG_BINS[0] - 1), i_s, iv]
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 1)
    _rot, window = cv2.CamShift(bp, window, crit)
    x, y, w, h = window
    if w < 4 or h < 4 or w * h > 0.4 * IMG_W * IMG_H:
        return None, window, 0.0
    score = float(bp[y : y + h, x : x + w].mean())
    if score < min_score:
        return None, window, score
    return (x + w / 2.0, y + h / 2.0), window, score
