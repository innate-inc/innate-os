#!/usr/bin/env python3
"""
Simple disparity computation following OpenCV documentation example
Using rectified stereo images
"""

import numpy as np
import cv2 as cv
import matplotlib.pyplot as plt
import os
import glob

def load_calibration_data(calib_path):
    """Load stereo calibration parameters"""
    calib_file = os.path.join(calib_path, 'stereo_calibration.npz')
    calib_data = np.load(calib_file)
    
    return {
        'camera_matrix_left': calib_data['camera_matrix_left'],
        'dist_coeffs_left': calib_data['dist_coeffs_left'],
        'camera_matrix_right': calib_data['camera_matrix_right'],
        'dist_coeffs_right': calib_data['dist_coeffs_right'],
        'R': calib_data['R'],
        'T': calib_data['T']
    }

def rectify_stereo_images(left_img, right_img, calib_params):
    """Rectify stereo images using calibration parameters"""
    # Extract calibration parameters
    camera_matrix_left = calib_params['camera_matrix_left']
    dist_coeffs_left = calib_params['dist_coeffs_left']
    camera_matrix_right = calib_params['camera_matrix_right']
    dist_coeffs_right = calib_params['dist_coeffs_right']
    R = calib_params['R']
    T = calib_params['T']
    
    h, w = left_img.shape[:2]
    
    # Stereo rectification
    R1, R2, P1, P2, Q, _, _ = cv.stereoRectify(
        camera_matrix_left, dist_coeffs_left,
        camera_matrix_right, dist_coeffs_right,
        (w, h), R, T,
        flags=cv.CALIB_ZERO_DISPARITY,
        alpha=0
    )
    
    # Create rectification maps
    map1x, map1y = cv.initUndistortRectifyMap(
        camera_matrix_left, dist_coeffs_left, R1, P1, (w, h), cv.CV_32FC1
    )
    map2x, map2y = cv.initUndistortRectifyMap(
        camera_matrix_right, dist_coeffs_right, R2, P2, (w, h), cv.CV_32FC1
    )
    
    # Rectify images
    rectified_left = cv.remap(left_img, map1x, map1y, cv.INTER_LINEAR)
    rectified_right = cv.remap(right_img, map2x, map2y, cv.INTER_LINEAR)
    
    return rectified_left, rectified_right

def main():
    """Main function following OpenCV documentation example"""
    
    # Load calibration data
    calib_path = "/home/jetson1/maurice-prod/calib_out_simple"
    calib_params = load_calibration_data(calib_path)
    
    # Find stereo images
    image_files = ["/home/jetson1/maurice-prod/me/frame_20250906_145122.jpg"]
    
    if not image_files:
        print("No images found!")
        return
    
    # Load first stereo image
    img_path = image_files[0]
    print(f"Loading: {os.path.basename(img_path)}")
    
    # Load and split side-by-side stereo image
    img = cv.imread(img_path, cv.IMREAD_GRAYSCALE)
    h, w = img.shape
    imgL = img[:, :w//2]  # Left image
    imgR = img[:, w//2:]  # Right image
    
    print(f"Original image size: {img.shape}")
    print(f"Split image size: {imgL.shape}")
    
    # Rectify stereo images
    print("Rectifying stereo images...")
    imgL_rect, imgR_rect = rectify_stereo_images(imgL, imgR, calib_params)
    
    # Compute disparity using StereoBM (following OpenCV documentation)
    print("Computing disparity...")
    stereo = cv.StereoBM.create(numDisparities=64, blockSize=15)
    disparity = stereo.compute(imgL_rect, imgR_rect)
    
    print(f"Disparity range: {disparity.min()} to {disparity.max()}")
    print(f"Non-zero disparities: {np.count_nonzero(disparity)}")
    
    # Save images
    output_dir = "/home/jetson1/maurice-prod/simple_disparity_output"
    os.makedirs(output_dir, exist_ok=True)
    
    base_name = os.path.splitext(os.path.basename(img_path))[0]
    
    # Save original split images
    cv.imwrite(os.path.join(output_dir, f"{base_name}_left.jpg"), imgL)
    cv.imwrite(os.path.join(output_dir, f"{base_name}_right.jpg"), imgR)
    
    # Save rectified images
    cv.imwrite(os.path.join(output_dir, f"{base_name}_rectified_left.jpg"), imgL_rect)
    cv.imwrite(os.path.join(output_dir, f"{base_name}_rectified_right.jpg"), imgR_rect)
    
    # Save disparity map
    cv.imwrite(os.path.join(output_dir, f"{base_name}_disparity.jpg"), disparity)
    
    # Create normalized disparity for better visualization
    disparity_norm = cv.normalize(disparity, None, alpha=0, beta=255, norm_type=cv.NORM_MINMAX)
    cv.imwrite(os.path.join(output_dir, f"{base_name}_disparity_normalized.jpg"), disparity_norm)
    
    # Create colored disparity map
    disparity_colored = cv.applyColorMap(disparity_norm.astype(np.uint8), cv.COLORMAP_JET)
    cv.imwrite(os.path.join(output_dir, f"{base_name}_disparity_colored.jpg"), disparity_colored)
    
    print(f"\nResults saved to: {output_dir}")
    print(f"- Left/Right images: {base_name}_left.jpg, {base_name}_right.jpg")
    print(f"- Rectified images: {base_name}_rectified_left.jpg, {base_name}_rectified_right.jpg")
    print(f"- Disparity map: {base_name}_disparity.jpg")
    print(f"- Normalized disparity: {base_name}_disparity_normalized.jpg")
    print(f"- Colored disparity: {base_name}_disparity_colored.jpg")
    
    # Print statistics
    valid_disparities = disparity[disparity > 0]
    if len(valid_disparities) > 0:
        print(f"\nDisparity statistics:")
        print(f"- Valid pixels: {len(valid_disparities)} / {disparity.size}")
        print(f"- Range: {valid_disparities.min()} to {valid_disparities.max()}")
        print(f"- Mean: {valid_disparities.mean():.1f}")
        print(f"- Median: {np.median(valid_disparities):.1f}")
    
    print("\nDone! You can now view the disparity maps.")

if __name__ == "__main__":
    main()
