"""
Debug visualization utilities for stereo calibration.

Provides mosaic generation for per-capture debugging and
post-calibration corner/rectified visualizations.
"""

import cv2
import numpy as np


def generate_debug_mosaic(node, result):
    """Generate a 3x2 debug mosaic image after each capture attempt.

    Layout:
        [ Left + ArUco markers (cyan)        ] [ Right + ArUco markers (cyan)        ]
        [ Left + ChArUco corners (red + IDs) ] [ Right + ChArUco corners (green + IDs) ]
        [ Common corners (red=L, green=R)    ] [ Object points (orange)               ]

    Args:
        node: StereoCalibrator node instance (for logger, dimensions, paths, counters).
        result: DetectionResult from ``_process_image_pair``.
    """
    try:
        # Unpack result for readability
        left_img = result.left_img
        right_img = result.right_img
        marker_corners_left = result.marker_corners_left
        marker_ids_left = result.marker_ids_left
        marker_corners_right = result.marker_corners_right
        marker_ids_right = result.marker_ids_right
        charuco_corners_left = result.charuco_corners_left
        charuco_ids_left = result.charuco_ids_left
        charuco_corners_right = result.charuco_corners_right
        charuco_ids_right = result.charuco_ids_right
        corners_left_filtered = result.corners_left_filtered
        corners_right_filtered = result.corners_right_filtered
        obj_pts = result.obj_pts_common
        capture_success = result.success

        h, w = node.image_height, node.image_width
        status_color = (0, 255, 0) if capture_success else (0, 0, 255)
        status_text = 'OK' if capture_success else 'FAIL'

        # --- Row 1: Raw images with ArUco marker outlines overlaid ---
        left_markers_panel = left_img.copy()
        right_markers_panel = right_img.copy()

        n_markers_left = len(marker_ids_left) if marker_ids_left is not None else 0
        n_markers_right = len(marker_ids_right) if marker_ids_right is not None else 0

        if marker_corners_left is not None and marker_ids_left is not None:
            cv2.aruco.drawDetectedMarkers(left_markers_panel, marker_corners_left, marker_ids_left)
        if marker_corners_right is not None and marker_ids_right is not None:
            cv2.aruco.drawDetectedMarkers(right_markers_panel, marker_corners_right, marker_ids_right)

        cv2.putText(left_markers_panel, f'LEFT {status_text} - {n_markers_left} markers', (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)
        cv2.putText(left_markers_panel, f'[{node.images_captured}/{node.num_images_required}]', (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(right_markers_panel, f'RIGHT {status_text} - {n_markers_right} markers', (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)

        # --- Row 2: Raw images with ChArUco corners overlaid ---
        # Common corners are drawn darker, non-common lighter
        left_charuco_panel = left_img.copy()
        right_charuco_panel = right_img.copy()

        common_ids = result.common_ids or set()
        n_corners_left = len(charuco_ids_left) if charuco_ids_left is not None else 0
        n_corners_right = len(charuco_ids_right) if charuco_ids_right is not None else 0
        n_common = len(common_ids)

        if charuco_corners_left is not None and charuco_ids_left is not None:
            for i, corner in enumerate(charuco_corners_left):
                x, y = int(corner[0, 0]), int(corner[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cid = charuco_ids_left[i].item()
                    # Dark red = common, bright red = this camera only
                    color = (0, 0, 150) if cid in common_ids else (100, 100, 255)
                    cv2.circle(left_charuco_panel, (x, y), 4, color, -1)
                    cv2.putText(left_charuco_panel, str(cid),
                                (x + 5, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 255, 255), 1)

        if charuco_corners_right is not None and charuco_ids_right is not None:
            for i, corner in enumerate(charuco_corners_right):
                x, y = int(corner[0, 0]), int(corner[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cid = charuco_ids_right[i].item()
                    # Dark green = common, bright green = this camera only
                    color = (0, 150, 0) if cid in common_ids else (100, 255, 100)
                    cv2.circle(right_charuco_panel, (x, y), 4, color, -1)
                    cv2.putText(right_charuco_panel, str(cid),
                                (x + 5, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 255, 255), 1)

        cv2.putText(left_charuco_panel,
                    f'LEFT - {n_corners_left} corners ({n_common} common)', (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        cv2.putText(right_charuco_panel,
                    f'RIGHT - {n_corners_right} corners ({n_common} common)', (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        # --- Row 3: Common corners and object points ---
        corners_panel = np.zeros((h, w, 3), dtype=np.uint8)
        obj_panel = np.zeros((h, w, 3), dtype=np.uint8)

        if corners_left_filtered is not None and corners_right_filtered is not None:
            for corner in corners_left_filtered:
                x, y = int(corner[0, 0]), int(corner[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(corners_panel, (x, y), 4, (0, 0, 255), -1)
            for corner in corners_right_filtered:
                x, y = int(corner[0, 0]), int(corner[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(corners_panel, (x, y), 4, (0, 255, 0), -1)
            n_common = len(corners_left_filtered)
            cv2.putText(corners_panel, f'Common corners: {n_common} (red=L, green=R)', (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        else:
            cv2.putText(corners_panel, 'No common corners', (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

        if obj_pts is not None and len(obj_pts) > 0:
            pts = obj_pts.reshape(-1, 3)
            obj_x = pts[:, 0]
            obj_y = pts[:, 1]
            x_min, x_max = obj_x.min(), obj_x.max()
            y_min, y_max = obj_y.min(), obj_y.max()
            margin = 50
            scale_x = (w - 2 * margin) / (x_max - x_min) if x_max > x_min else 1
            scale_y = (h - 2 * margin) / (y_max - y_min) if y_max > y_min else 1
            scale = min(scale_x, scale_y)
            for pt in pts:
                px = int((pt[0] - x_min) * scale + margin)
                py = int((pt[1] - y_min) * scale + margin)
                if 0 <= px < w and 0 <= py < h:
                    cv2.circle(obj_panel, (px, py), 4, (0, 165, 255), -1)
            cv2.putText(obj_panel, f'Object points: {len(pts)} (3D->2D)', (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 165, 255), 2)
        else:
            cv2.putText(obj_panel, 'No object points', (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

        # Assemble 3x2 mosaic
        row1 = np.hstack([left_markers_panel, right_markers_panel])
        row2 = np.hstack([left_charuco_panel, right_charuco_panel])
        row3 = np.hstack([corners_panel, obj_panel])
        mosaic = np.vstack([row1, row2, row3])

        # Save mosaic
        mosaic_path = node.tmp_image_dir / f'debug_mosaic_{node.capture_attempts:03d}.png'
        cv2.imwrite(str(mosaic_path), mosaic)
        mosaic_path = node.tmp_image_dir / 'debug_mosaic.png'  # for live viewing
        cv2.imwrite(str(mosaic_path), mosaic)
        node.get_logger().info(f'Debug mosaic saved: {mosaic_path}')

    except Exception as e:
        node.get_logger().warn(f'Failed to generate debug mosaic: {e}')


def generate_visualizations(node):
    """Generate visualization images showing detected corners and rectified corners.

    Called after calibration completes. Requires ``node.calibration_data`` to be
    populated and ``node.all_corners_left`` / ``node.all_corners_right`` to contain
    the collected corner data.

    Args:
        node: StereoCalibrator node instance.
    """
    try:
        w, h = node.image_width, node.image_height

        # --- Raw corner scatter (red = left, green = right) ---
        # Individual-only corners are bright, common corners are dark
        corners_viz = np.zeros((h, w, 3), dtype=np.uint8)

        for corners in node.indiv_corners_left:
            for corner in corners:
                x, y = int(corner[0, 0]), int(corner[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(corners_viz, (x, y), 3, (100, 100, 255), -1)

        for corners in node.indiv_corners_right:
            for corner in corners:
                x, y = int(corner[0, 0]), int(corner[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(corners_viz, (x, y), 3, (100, 255, 100), -1)

        # Overdraw common corners darker
        for corners in node.common_corners_left:
            for corner in corners:
                x, y = int(corner[0, 0]), int(corner[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(corners_viz, (x, y), 3, (0, 0, 150), -1)

        for corners in node.common_corners_right:
            for corner in corners:
                x, y = int(corner[0, 0]), int(corner[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(corners_viz, (x, y), 3, (0, 150, 0), -1)

        corners_path = node.tmp_image_dir / 'corners_visualization.png'
        cv2.imwrite(str(corners_path), corners_viz)
        node.get_logger().info(f'  Saved corners visualization: {corners_path}')

        # --- Rectified corner scatter ---
        # Uses individual (all) corners per camera with per-camera undistortion
        rectified_viz = np.zeros((h, w, 3), dtype=np.uint8)

        K1 = node.calibration_data['K1']
        D1 = node.calibration_data['D1']
        K2 = node.calibration_data['K2']
        D2 = node.calibration_data['D2']
        R1 = node.calibration_data['R1']
        R2 = node.calibration_data['R2']
        P1 = node.calibration_data['P1']
        P2 = node.calibration_data['P2']

        for corners in node.indiv_corners_left:
            rectified = cv2.undistortPoints(corners, K1, D1, R=R1, P=P1)
            for pt in rectified:
                x, y = int(pt[0, 0]), int(pt[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(rectified_viz, (x, y), 3, (100, 100, 255), -1)

        for corners in node.indiv_corners_right:
            rectified = cv2.undistortPoints(corners, K2, D2, R=R2, P=P2)
            for pt in rectified:
                x, y = int(pt[0, 0]), int(pt[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(rectified_viz, (x, y), 3, (100, 255, 100), -1)

        # Overdraw common corners darker
        for corners in node.common_corners_left:
            rectified = cv2.undistortPoints(corners, K1, D1, R=R1, P=P1)
            for pt in rectified:
                x, y = int(pt[0, 0]), int(pt[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(rectified_viz, (x, y), 3, (0, 0, 150), -1)

        for corners in node.common_corners_right:
            rectified = cv2.undistortPoints(corners, K2, D2, R=R2, P=P2)
            for pt in rectified:
                x, y = int(pt[0, 0]), int(pt[0, 1])
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(rectified_viz, (x, y), 3, (0, 150, 0), -1)

        rectified_path = node.tmp_image_dir / 'corners_rectified_visualization.png'
        cv2.imwrite(str(rectified_path), rectified_viz)
        node.get_logger().info(f'  Saved rectified corners visualization: {rectified_path}')

    except Exception as e:
        node.get_logger().warn(f'Failed to generate visualizations: {e}')
