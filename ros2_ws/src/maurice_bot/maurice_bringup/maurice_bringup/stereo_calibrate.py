#!/usr/bin/env python3
"""
Stereo calibration tool for side-by-side ELP-style frames (e.g., 3840x1080 with two 1920x1080 images concatenated horizontally).

Inputs:
- A directory of images captured from the stereo camera (side-by-side layout).
- Chessboard/charuco pattern parameters: inner corners (cols, rows) and square size (meters).

Outputs:
- Left and right camera intrinsics + distortion (YAML files for ROS CameraInfo).
- Stereo extrinsics (R, T, E, F) and rectification (R1, R2, P1, P2, Q) (NPZ + optional YAML).
- Optional debug draws and a rectified example image.

Usage examples:
  python3 stereo_calibrate.py --images_dir ./stereo_frames --pattern_cols 9 --pattern_rows 6 --square_size 0.0245 --output_dir ./calib_out

Dependencies:
- OpenCV (cv2), NumPy
Note: On Jetson, prefer system OpenCV (apt). Ensure `python3-opencv` and `python3-numpy` are installed.
"""

import argparse
import os
import sys
import glob
from typing import List, Tuple

import cv2
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stereo calibration for side-by-side frames")
    parser.add_argument("--images_dir", type=str, required=True, help="Directory containing side-by-side stereo images")
    parser.add_argument("--pattern_cols", type=int, default=9, help="Chessboard inner corners (columns)")
    parser.add_argument("--pattern_rows", type=int, default=6, help="Chessboard inner corners (rows)")
    parser.add_argument("--square_size", type=float, default=0.0245, help="Square size in meters")
    parser.add_argument("--output_dir", type=str, default="./calib_out", help="Output directory for results")
    parser.add_argument("--show", action="store_true", help="Show corner detections and rectified preview")
    parser.add_argument("--max_images", type=int, default=0, help="Limit number of images used (0 = all)")
    parser.add_argument("--pattern", type=str, default="*.jpg,*.jpeg,*.png", help="Comma-separated glob(s) to match input images")
    parser.add_argument("--fisheye", action="store_true", help="Use fisheye calibration model (wide-angle lenses)")
    parser.add_argument("--use_sb", action="store_true", help="Use findChessboardCornersSB (more robust on wide-angle)")
    return parser.parse_args()


def list_images(images_dir: str, pattern: str) -> List[str]:
    patterns = [p.strip() for p in pattern.split(",") if p.strip()]
    files: List[str] = []
    for p in patterns:
        files.extend(glob.glob(os.path.join(images_dir, p)))
    files.sort()
    return files


def split_side_by_side(image: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    h, w = image.shape[:2]
    mid = w // 2
    left = image[:, 0:mid]
    right = image[:, mid:w]
    return left, right


def build_object_points(pattern_size: Tuple[int, int], square_size: float) -> np.ndarray:
    cols, rows = pattern_size
    objp = np.zeros((rows * cols, 3), np.float32)
    grid_x, grid_y = np.meshgrid(np.arange(cols), np.arange(rows))
    objp[:, 0] = grid_x.flatten() * square_size
    objp[:, 1] = grid_y.flatten() * square_size
    # Z stays 0 (planar chessboard)
    return objp


def find_corners(gray: np.ndarray, pattern_size: Tuple[int, int], use_sb: bool) -> Tuple[bool, np.ndarray]:
    if use_sb and hasattr(cv2, "findChessboardCornersSB"):
        found, corners = cv2.findChessboardCornersSB(gray, pattern_size, flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not found:
            return False, None
        # Ensure shape is (N,1,2)
        if corners.ndim == 2:
            corners = corners.reshape((-1, 1, 2))
        return True, corners.astype(np.float32)
    else:
        flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_FAST_CHECK | cv2.CALIB_CB_NORMALIZE_IMAGE
        found, corners = cv2.findChessboardCorners(gray, pattern_size, flags)
        if not found:
            return False, None
        term = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
        corners_refined = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), term)
        return True, corners_refined


def collect_detections(image_paths: List[str], pattern_size: Tuple[int, int], show: bool = False, max_images: int = 0, use_sb: bool = False):
    objpoints = []  # 3D points in chessboard coordinate system
    imgpoints_left = []
    imgpoints_right = []
    used_images = []

    example_left_size = None

    objp = build_object_points(pattern_size, square_size=1.0)  # scaled later by square_size param

    used_count = 0
    for idx, path in enumerate(image_paths):
        image = cv2.imread(path, cv2.IMREAD_COLOR)
        if image is None:
            print(f"WARN: Could not read {path}")
            continue

        left_img, right_img = split_side_by_side(image)
        gray_l = cv2.cvtColor(left_img, cv2.COLOR_BGR2GRAY)
        gray_r = cv2.cvtColor(right_img, cv2.COLOR_BGR2GRAY)

        found_l, corners_l = find_corners(gray_l, pattern_size, use_sb)
        found_r, corners_r = find_corners(gray_r, pattern_size, use_sb)

        if found_l and found_r:
            if example_left_size is None:
                example_left_size = (gray_l.shape[1], gray_l.shape[0])

            objpoints.append(objp.copy())
            imgpoints_left.append(corners_l)
            imgpoints_right.append(corners_r)
            used_images.append(path)

            if show:
                vis_l = left_img.copy()
                vis_r = right_img.copy()
                cv2.drawChessboardCorners(vis_l, pattern_size, corners_l, True)
                cv2.drawChessboardCorners(vis_r, pattern_size, corners_r, True)
                stacked = np.hstack([vis_l, vis_r])
                cv2.imshow("Detections (L|R)", stacked)
                cv2.waitKey(200)

            used_count += 1
            if max_images > 0 and used_count >= max_images:
                break
        else:
            print(f"INFO: Pattern not found in {os.path.basename(path)} (L={found_l}, R={found_r})")

    if show:
        cv2.destroyAllWindows()

    return objpoints, imgpoints_left, imgpoints_right, example_left_size, used_images


def calibrate_mono(objpoints: List[np.ndarray], imgpoints: List[np.ndarray], image_size: Tuple[int, int],
                   square_size: float, fisheye: bool):
    # Scale object points by the real square size
    scaled_objpoints = [op * square_size for op in objpoints]

    if fisheye:
        # fisheye requires shapes (N,1,3) and (N,1,2)
        obj = [op.reshape(-1, 1, 3).astype(np.float64) for op in scaled_objpoints]
        img = [ip.reshape(-1, 1, 2).astype(np.float64) for ip in imgpoints]
        K = np.eye(3, dtype=np.float64)
        D = np.zeros((4, 1), dtype=np.float64)
        flags = (
            cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC |
            cv2.fisheye.CALIB_FIX_SKEW
        )
        rms, K, D, rvecs, tvecs = cv2.fisheye.calibrate(
            objectPoints=obj, imagePoints=img, image_size=image_size,
            K=K, D=D, flags=flags,
            criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6)
        )
        return rms, K, D, rvecs, tvecs
    else:
        camera_matrix = np.eye(3, dtype=np.float64)
        dist_coeffs = np.zeros((8, 1), dtype=np.float64)
        flags = (
            cv2.CALIB_RATIONAL_MODEL
        )
        rms, K, D, rvecs, tvecs = cv2.calibrateCamera(
            scaled_objpoints, imgpoints, image_size, camera_matrix, dist_coeffs,
            flags=flags
        )
        return rms, K, D, rvecs, tvecs


def calibrate_stereo(objpoints: List[np.ndarray], imgpoints_l: List[np.ndarray], imgpoints_r: List[np.ndarray],
                     K1: np.ndarray, D1: np.ndarray, K2: np.ndarray, D2: np.ndarray, image_size: Tuple[int, int],
                     square_size: float, fisheye: bool):
    if fisheye:
        obj = [op.reshape(-1, 1, 3).astype(np.float64) for op in objpoints]
        img_l = [ip.reshape(-1, 1, 2).astype(np.float64) for ip in imgpoints_l]
        img_r = [ip.reshape(-1, 1, 2).astype(np.float64) for ip in imgpoints_r]

        flags = cv2.fisheye.CALIB_FIX_INTRINSIC
        rms, K1, D1, K2, D2, R, T = cv2.fisheye.stereoCalibrate(
            objectPoints=obj,
            imagePoints1=img_l,
            imagePoints2=img_r,
            K1=K1, D1=D1, K2=K2, D2=D2,
            imageSize=image_size,
            flags=flags,
            criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6)
        )

        R1, R2, P1, P2, Q = cv2.fisheye.stereoRectify(
            K1=K1, D1=D1, K2=K2, D2=D2, image_size=image_size, R=R, T=T
        )
        E = np.zeros((3, 3), dtype=np.float64)
        F = np.zeros((3, 3), dtype=np.float64)
        return rms, (R, T, E, F), (R1, R2, P1, P2, Q)
    else:
        flags = cv2.CALIB_FIX_INTRINSIC
        criteria = (cv2.TERM_CRITERIA_MAX_ITER + cv2.TERM_CRITERIA_EPS, 100, 1e-6)
        rms, K1, D1, K2, D2, R, T, E, F = cv2.stereoCalibrate(
            objectPoints=objpoints,
            imagePoints1=imgpoints_l,
            imagePoints2=imgpoints_r,
            cameraMatrix1=K1,
            distCoeffs1=D1,
            cameraMatrix2=K2,
            distCoeffs2=D2,
            imageSize=image_size,
            criteria=criteria,
            flags=flags
        )

        R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(
            cameraMatrix1=K1, distCoeffs1=D1,
            cameraMatrix2=K2, distCoeffs2=D2,
            imageSize=image_size, R=R, T=T, alpha=0
        )
        return rms, (R, T, E, F), (R1, R2, P1, P2, Q)


def write_camera_info_yaml(path: str, camera_name: str, width: int, height: int,
                           K: np.ndarray, D: np.ndarray, R: np.ndarray, P: np.ndarray,
                           distortion_model: str = "plumb_bob"):
    def flat(a):
        return ", ".join([f"{x:.8e}" for x in a.flatten()])

    yaml = f"""
image_width: {width}
image_height: {height}
camera_name: {camera_name}
camera_matrix:
  rows: 3
  cols: 3
  data: [{flat(K)}]
distortion_model: {distortion_model}
distortion_coefficients:
  rows: 1
  cols: {D.size}
  data: [{flat(D)}]
rectification_matrix:
  rows: 3
  cols: 3
  data: [{flat(R)}]
projection_matrix:
  rows: 3
  cols: 4
  data: [{flat(P)}]
""".lstrip()
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml)


def visualize_rectification(left_img: np.ndarray, right_img: np.ndarray,
                            K1: np.ndarray, D1: np.ndarray, K2: np.ndarray, D2: np.ndarray,
                            R1: np.ndarray, R2: np.ndarray, P1: np.ndarray, P2: np.ndarray,
                            fisheye: bool) -> np.ndarray:
    size = (left_img.shape[1], left_img.shape[0])
    if fisheye:
        map1x, map1y = cv2.fisheye.initUndistortRectifyMap(K1, D1, R1, P1, size, cv2.CV_32FC1)
        map2x, map2y = cv2.fisheye.initUndistortRectifyMap(K2, D2, R2, P2, size, cv2.CV_32FC1)
    else:
        map1x, map1y = cv2.initUndistortRectifyMap(K1, D1, R1, P1, size, cv2.CV_32FC1)
        map2x, map2y = cv2.initUndistortRectifyMap(K2, D2, R2, P2, size, cv2.CV_32FC1)
    rect_l = cv2.remap(left_img, map1x, map1y, interpolation=cv2.INTER_LINEAR)
    rect_r = cv2.remap(right_img, map2x, map2y, interpolation=cv2.INTER_LINEAR)
    stacked = np.hstack([rect_l, rect_r])
    # draw epipolar lines
    for y in range(0, stacked.shape[0], 40):
        cv2.line(stacked, (0, y), (stacked.shape[1], y), (0, 255, 0), 1)
    return stacked


def main():
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    pattern_size = (args.pattern_cols, args.pattern_rows)
    image_paths = list_images(args.images_dir, args.pattern)
    if not image_paths:
        print("ERROR: No images found.")
        return 1

    print(f"Found {len(image_paths)} images. Searching for chessboard {pattern_size}...")
    objpoints, imgpoints_l, imgpoints_r, image_size, used = collect_detections(
        image_paths, pattern_size, show=args.show, max_images=args.max_images, use_sb=args.use_sb
    )

    if not used:
        print("ERROR: No valid stereo detections found.")
        return 2

    print(f"Using {len(used)} valid stereo pairs. Image size: {image_size}")

    # Calibrate left and right cameras independently
    print("Calibrating left camera...")
    rms_l, K1, D1, rvecs_l, tvecs_l = calibrate_mono(objpoints, imgpoints_l, image_size, args.square_size, args.fisheye)
    print(f"  Left RMS reprojection error: {rms_l:.4f}")

    print("Calibrating right camera...")
    rms_r, K2, D2, rvecs_r, tvecs_r = calibrate_mono(objpoints, imgpoints_r, image_size, args.square_size, args.fisheye)
    print(f"  Right RMS reprojection error: {rms_r:.4f}")

    # Stereo calibration with fixed intrinsics
    print("Stereo calibrating (estimating R, T, E, F) ...")
    rms_s, (R, T, E, F), (R1, R2, P1, P2, Q) = calibrate_stereo(
        [op * args.square_size for op in objpoints], imgpoints_l, imgpoints_r, K1, D1, K2, D2, image_size, args.square_size, args.fisheye
    )
    print(f"  Stereo RMS reprojection error: {rms_s:.4f}")

    # Save results
    npz_path = os.path.join(args.output_dir, "stereo_calib.npz")
    np.savez(npz_path, K1=K1, D1=D1, K2=K2, D2=D2, R=R, T=T, E=E, F=F, R1=R1, R2=R2, P1=P1, P2=P2, Q=Q,
             image_width=image_size[0], image_height=image_size[1])
    print(f"Saved NPZ: {npz_path}")

    # Write ROS CameraInfo YAMLs
    left_yaml = os.path.join(args.output_dir, "left_camera.yaml")
    right_yaml = os.path.join(args.output_dir, "right_camera.yaml")
    distortion_model = "fisheye" if args.fisheye else "plumb_bob"
    write_camera_info_yaml(left_yaml, "left", image_size[0], image_size[1], K1, D1, R1, P1, distortion_model)
    write_camera_info_yaml(right_yaml, "right", image_size[0], image_size[1], K2, D2, R2, P2, distortion_model)
    print(f"Saved CameraInfo YAMLs: {left_yaml}, {right_yaml}")

    # Optional rectified preview from the first used image
    if args.show:
        first = cv2.imread(used[0], cv2.IMREAD_COLOR)
        l_img, r_img = split_side_by_side(first)
        rect_preview = visualize_rectification(l_img, r_img, K1, D1, K2, D2, R1, R2, P1, P2, args.fisheye)
        cv2.imshow("Rectified preview (L|R)", rect_preview)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    # Also save rectified preview to disk
    preview_path = os.path.join(args.output_dir, "rectified_preview.jpg")
    try:
        first = cv2.imread(used[0], cv2.IMREAD_COLOR)
        l_img, r_img = split_side_by_side(first)
        rect_preview = visualize_rectification(l_img, r_img, K1, D1, K2, D2, R1, R2, P1, P2, args.fisheye)
        cv2.imwrite(preview_path, rect_preview)
        print(f"Saved rectified preview: {preview_path}")
    except Exception as e:
        print(f"WARN: Could not save rectified preview: {e}")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


