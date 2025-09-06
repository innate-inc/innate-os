#!/usr/bin/env python3
"""
Interactive stereo capture for side-by-side ELP-style cameras.

Features:
- Opens /dev/video0 (or given device), requests 3840x1080 MJPG.
- Splits frames into left/right, detects chessboard on both halves.
- Shows live preview with corners and guidance.
- Auto-saves only when both sides have detections, image is sharp, and pose is sufficiently different.

Usage:
  python3 -m maurice_bringup.stereo_capture --out_dir ./calibration_frames \
      --cols 9 --rows 6 --device /dev/video0 --width 3840 --height 1080 --fps 15 --use_sb

Keys:
  s: force save current frame (even if detection fails)
  space/enter: toggle autosave enable/disable
  q/esc: quit
"""

import argparse
import os
import time
from typing import Tuple, List

import cv2
import numpy as np


def parse_args():
    p = argparse.ArgumentParser(description="Stereo capture with chessboard detection")
    p.add_argument("--device", type=str, default="/dev/video0")
    p.add_argument("--width", type=int, default=3840)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--fourcc", type=str, default="MJPG", help="FOURCC, e.g. MJPG or YUYV")
    p.add_argument("--out_dir", type=str, required=True)
    p.add_argument("--cols", type=int, default=9, help="Chessboard inner corners (columns)")
    p.add_argument("--rows", type=int, default=6, help="Chessboard inner corners (rows)")
    p.add_argument("--use_sb", action="store_true", help="Use findChessboardCornersSB")
    p.add_argument("--min_lap_var", type=float, default=80.0, help="Sharpness threshold (Laplacian variance)")
    p.add_argument("--min_move", type=float, default=35.0, help="Min pixel move since last save (corner centroid)")
    p.add_argument("--autosave", action="store_true", help="Start with autosave enabled")
    return p.parse_args()


def split_lr(img: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    h, w = img.shape[:2]
    mid = w // 2
    return img[:, :mid], img[:, mid:]


def detect(gray: np.ndarray, pattern_size: Tuple[int, int], use_sb: bool):
    if use_sb and hasattr(cv2, "findChessboardCornersSB"):
        found, corners = cv2.findChessboardCornersSB(gray, pattern_size, flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not found:
            return False, None, None
        if corners.ndim == 2:
            corners = corners.reshape((-1, 1, 2))
        corners = corners.astype(np.float32)
    else:
        flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_FAST_CHECK | cv2.CALIB_CB_NORMALIZE_IMAGE
        found, corners = cv2.findChessboardCorners(gray, pattern_size, flags)
        if not found:
            return False, None, None
        term = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), term)
    centroid = corners.reshape(-1, 2).mean(axis=0)
    return True, corners, centroid


def laplacian_variance(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def main():
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    cap = cv2.VideoCapture()
    cap.open(args.device, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    if len(args.fourcc) == 4:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*args.fourcc))

    ok, frame = cap.read()
    if not ok:
        print("ERROR: cannot read from device")
        return 1

    autosave = args.autosave
    last_centroid_l = None
    last_centroid_r = None
    saved = 0
    t0 = time.time()

    pattern = (args.cols, args.rows)
    font = cv2.FONT_HERSHEY_SIMPLEX

    while True:
        ok, frame = cap.read()
        if not ok:
            print("WARN: dropped frame")
            continue

        left, right = split_lr(frame)
        gray_l = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        gray_r = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)

        sharp_l = laplacian_variance(gray_l)
        sharp_r = laplacian_variance(gray_r)

        found_l, corners_l, centroid_l = detect(gray_l, pattern, args.use_sb)
        found_r, corners_r, centroid_r = detect(gray_r, pattern, args.use_sb)

        vis_l = left.copy()
        vis_r = right.copy()
        if found_l:
            cv2.drawChessboardCorners(vis_l, pattern, corners_l, True)
        if found_r:
            cv2.drawChessboardCorners(vis_r, pattern, corners_r, True)

        stacked = np.hstack([vis_l, vis_r])
        status = f"autosave={'ON' if autosave else 'OFF'}  saved={saved}  sharp(L/R)={sharp_l:.0f}/{sharp_r:.0f}"
        cv2.putText(stacked, status, (20, 40), font, 1.0, (0,255,0), 2, cv2.LINE_AA)
        if found_l and found_r:
            cv2.putText(stacked, "corners: OK", (20, 80), font, 1.0, (0,255,0), 2, cv2.LINE_AA)
        else:
            cv2.putText(stacked, "corners: MISSING", (20, 80), font, 1.0, (0,0,255), 2, cv2.LINE_AA)

        cv2.imshow("stereo capture (L|R)", stacked)
        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q')):
            break
        if key in (13, 32):  # enter or space: toggle autosave
            autosave = not autosave
        if key in (ord('s'),):
            ts = time.strftime("%Y%m%d_%H%M%S")
            out = os.path.join(args.out_dir, f"frame_{ts}.jpg")
            cv2.imwrite(out, frame)
            print("Saved (forced):", out)
            saved += 1

        # autosave logic
        if autosave and found_l and found_r and sharp_l >= args.min_lap_var and sharp_r >= args.min_lap_var:
            moved_ok = True
            if last_centroid_l is not None and last_centroid_r is not None:
                dl = np.linalg.norm(centroid_l - last_centroid_l)
                dr = np.linalg.norm(centroid_r - last_centroid_r)
                moved_ok = (dl > args.min_move) or (dr > args.min_move)
            if moved_ok:
                ts = time.strftime("%Y%m%d_%H%M%S")
                out = os.path.join(args.out_dir, f"frame_{ts}.jpg")
                cv2.imwrite(out, frame)
                print("Saved:", out, f"sharp(L/R)={sharp_l:.1f}/{sharp_r:.1f}")
                saved += 1
                last_centroid_l = centroid_l
                last_centroid_r = centroid_r

    cap.release()
    cv2.destroyAllWindows()
    dt = time.time() - t0
    print(f"Done. Saved {saved} frames in {dt:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


