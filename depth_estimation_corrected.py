#!/usr/bin/env python3
"""
Corrected depth estimation for side-by-side stereo images
Each image contains both left and right camera views side by side
"""

import cv2
import numpy as np
import os
import glob
from pathlib import Path

def load_calibration_data(calib_path):
    """Load stereo calibration parameters from NPZ file"""
    calib_file = os.path.join(calib_path, 'stereo_calibration.npz')
    
    if not os.path.exists(calib_file):
        raise FileNotFoundError(f"Calibration file not found: {calib_file}")
    
    # Load calibration data
    calib_data = np.load(calib_file)
    
    # Extract calibration parameters
    camera_matrix_left = calib_data['camera_matrix_left']
    dist_coeffs_left = calib_data['dist_coeffs_left']
    camera_matrix_right = calib_data['camera_matrix_right']
    dist_coeffs_right = calib_data['dist_coeffs_right']
    R = calib_data['R']  # Rotation matrix
    T = calib_data['T']  # Translation vector
    E = calib_data['E']  # Essential matrix
    F = calib_data['F']  # Fundamental matrix
    
    print("Calibration data loaded successfully!")
    print(f"Left camera matrix:\n{camera_matrix_left}")
    print(f"Right camera matrix:\n{camera_matrix_right}")
    print(f"Rotation matrix:\n{R}")
    print(f"Translation vector:\n{T}")
    print(f"Baseline: {np.linalg.norm(T):.4f} units")
    
    return {
        'camera_matrix_left': camera_matrix_left,
        'dist_coeffs_left': dist_coeffs_left,
        'camera_matrix_right': camera_matrix_right,
        'dist_coeffs_right': dist_coeffs_right,
        'R': R,
        'T': T,
        'E': E,
        'F': F
    }

def process_side_by_side_stereo(stereo_img_path, calib_params, output_dir):
    """Process a side-by-side stereo image"""
    
    # Load the stereo image
    stereo_img = cv2.imread(stereo_img_path, cv2.IMREAD_GRAYSCALE)
    
    if stereo_img is None:
        print(f"Error loading image: {stereo_img_path}")
        return None
    
    print(f"Processing: {os.path.basename(stereo_img_path)}")
    print(f"Image size: {stereo_img.shape}")
    
    # Split into left and right images
    h, w = stereo_img.shape
    left_img = stereo_img[:, :w//2]
    right_img = stereo_img[:, w//2:]
    
    print(f"Split image size: {left_img.shape}")
    
    # Extract calibration parameters
    camera_matrix_left = calib_params['camera_matrix_left']
    dist_coeffs_left = calib_params['dist_coeffs_left']
    camera_matrix_right = calib_params['camera_matrix_right']
    dist_coeffs_right = calib_params['dist_coeffs_right']
    R = calib_params['R']
    T = calib_params['T']
    
    # Get image dimensions
    h, w = left_img.shape[:2]
    
    # Stereo rectification
    print("Performing stereo rectification...")
    R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(
        camera_matrix_left, dist_coeffs_left,
        camera_matrix_right, dist_coeffs_right,
        (w, h), R, T,
        flags=cv2.CALIB_ZERO_DISPARITY,
        alpha=0
    )
    
    # Create rectification maps
    map1x, map1y = cv2.initUndistortRectifyMap(
        camera_matrix_left, dist_coeffs_left, R1, P1, (w, h), cv2.CV_32FC1
    )
    map2x, map2y = cv2.initUndistortRectifyMap(
        camera_matrix_right, dist_coeffs_right, R2, P2, (w, h), cv2.CV_32FC1
    )
    
    # Rectify images
    rectified_left = cv2.remap(left_img, map1x, map1y, cv2.INTER_LINEAR)
    rectified_right = cv2.remap(right_img, map2x, map2y, cv2.INTER_LINEAR)
    
    # Compute disparity map using StereoSGBM (better quality)
    print("Computing disparity map...")
    stereo_sgbm = cv2.StereoSGBM_create(
        minDisparity=0,
        numDisparities=96,  # Adjust based on your setup
        blockSize=5,
        P1=8 * 3 * 5**2,
        P2=32 * 3 * 5**2,
        disp12MaxDiff=1,
        uniquenessRatio=10,
        speckleWindowSize=100,
        speckleRange=32,
        preFilterCap=63,
        mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY
    )
    
    disparity_sgbm = stereo_sgbm.compute(rectified_left, rectified_right)
    disparity = disparity_sgbm.astype(np.float32) / 16.0  # SGBM returns scaled disparity
    
    # Also try StereoBM for comparison
    stereo_bm = cv2.StereoBM_create(
        numDisparities=96,
        blockSize=15
    )
    stereo_bm.setMinDisparity(0)
    stereo_bm.setTextureThreshold(10)
    stereo_bm.setUniquenessRatio(15)
    stereo_bm.setSpeckleWindowSize(100)
    stereo_bm.setSpeckleRange(32)
    stereo_bm.setDisp12MaxDiff(1)
    
    disparity_bm = stereo_bm.compute(rectified_left, rectified_right)
    
    # Normalize disparity for visualization
    disparity_normalized = cv2.normalize(disparity, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)
    disparity_normalized = np.uint8(disparity_normalized)
    
    # Convert disparity to depth
    print("Converting disparity to depth...")
    focal_length = P1[0, 0]  # Focal length from projection matrix
    baseline = np.linalg.norm(T)  # Baseline distance
    
    print(f"Focal length: {focal_length:.2f}")
    print(f"Baseline: {baseline:.4f}")
    
    # Avoid division by zero and invalid disparities
    valid_mask = disparity > 0
    depth_map = np.zeros_like(disparity, dtype=np.float32)
    
    # Calculate depth only for valid disparities
    depth_map[valid_mask] = (focal_length * baseline) / disparity[valid_mask]
    
    # Clip depth values to reasonable range (e.g., 0.1m to 100m)
    depth_map = np.clip(depth_map, 0.1, 100.0)
    
    # Save results
    base_name = os.path.splitext(os.path.basename(stereo_img_path))[0]
    
    # Save original split images
    cv2.imwrite(os.path.join(output_dir, f"{base_name}_left.jpg"), left_img)
    cv2.imwrite(os.path.join(output_dir, f"{base_name}_right.jpg"), right_img)
    
    # Save rectified images
    cv2.imwrite(os.path.join(output_dir, f"{base_name}_rectified_left.jpg"), rectified_left)
    cv2.imwrite(os.path.join(output_dir, f"{base_name}_rectified_right.jpg"), rectified_right)
    
    # Save disparity maps
    cv2.imwrite(os.path.join(output_dir, f"{base_name}_disparity_bm.jpg"), 
                cv2.normalize(disparity_bm, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX).astype(np.uint8))
    cv2.imwrite(os.path.join(output_dir, f"{base_name}_disparity_sgbm.jpg"), disparity_normalized)
    
    # Save depth map (normalized for visualization)
    depth_normalized = cv2.normalize(depth_map, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)
    cv2.imwrite(os.path.join(output_dir, f"{base_name}_depth.jpg"), depth_normalized)
    
    # Save raw depth data
    np.save(os.path.join(output_dir, f"{base_name}_depth_raw.npy"), depth_map)
    
    # Create a colorized depth map for better visualization
    depth_colored = cv2.applyColorMap(depth_normalized.astype(np.uint8), cv2.COLORMAP_JET)
    cv2.imwrite(os.path.join(output_dir, f"{base_name}_depth_colored.jpg"), depth_colored)
    
    # Print statistics
    valid_depths = depth_map[valid_mask]
    if len(valid_depths) > 0:
        print(f"Valid depth pixels: {len(valid_depths)} / {depth_map.size}")
        print(f"Depth range: {valid_depths.min():.3f}m to {valid_depths.max():.3f}m")
        print(f"Mean depth: {valid_depths.mean():.3f}m")
        print(f"Median depth: {np.median(valid_depths):.3f}m")
        
        # Print disparity statistics
        valid_disparities = disparity[valid_mask]
        print(f"Disparity range: {valid_disparities.min():.1f} to {valid_disparities.max():.1f}")
        print(f"Mean disparity: {valid_disparities.mean():.1f}")
    else:
        print("No valid depth values found!")
    
    print(f"Results saved to {output_dir}")
    
    return {
        'left_img': left_img,
        'right_img': right_img,
        'rectified_left': rectified_left,
        'rectified_right': rectified_right,
        'disparity_bm': disparity_bm,
        'disparity_sgbm': disparity_sgbm,
        'depth_map': depth_map,
        'valid_mask': valid_mask,
        'focal_length': focal_length,
        'baseline': baseline
    }

def analyze_results(output_dir):
    """Analyze the depth estimation results"""
    print("\n=== DEPTH ESTIMATION ANALYSIS ===")
    
    # Find all depth files
    depth_files = glob.glob(os.path.join(output_dir, "*_depth_raw.npy"))
    
    if not depth_files:
        print("No depth files found for analysis")
        return
    
    print(f"Analyzing {len(depth_files)} depth maps...")
    
    all_depths = []
    valid_counts = []
    
    for depth_file in depth_files:
        depth_map = np.load(depth_file)
        valid_mask = depth_map > 0.1  # Exclude invalid depths
        valid_depths = depth_map[valid_mask]
        
        if len(valid_depths) > 0:
            all_depths.extend(valid_depths.flatten())
            valid_counts.append(np.sum(valid_mask))
            
            print(f"{os.path.basename(depth_file)}: {len(valid_depths)} valid pixels, "
                  f"depth range {valid_depths.min():.2f}-{valid_depths.max():.2f}m")
    
    if all_depths:
        all_depths = np.array(all_depths)
        print(f"\nOverall statistics:")
        print(f"Total valid depth pixels: {len(all_depths)}")
        print(f"Global depth range: {all_depths.min():.2f}m to {all_depths.max():.2f}m")
        print(f"Mean depth: {all_depths.mean():.2f}m")
        print(f"Median depth: {np.median(all_depths):.2f}m")
        print(f"Std deviation: {all_depths.std():.2f}m")

def main():
    """Main function to run corrected depth estimation"""
    
    # Set up paths
    calib_path = "/home/jetson1/maurice-prod/calib_out_simple"
    image_dir = "/home/jetson1/maurice-prod/calibration_frames_new"
    output_dir = "/home/jetson1/maurice-prod/depth_results_corrected"
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Load calibration data
        print("Loading calibration data...")
        calib_params = load_calibration_data(calib_path)
        
        # Find all stereo images
        image_files = sorted(glob.glob(os.path.join(image_dir, "*.jpg")))
        
        if not image_files:
            print("No images found!")
            return
        
        print(f"Found {len(image_files)} stereo images")
        
        # Process first few images for testing
        test_images = image_files[:5]  # Process first 5 images
        print(f"Processing {len(test_images)} stereo images...")
        
        for i, img_path in enumerate(test_images):
            print(f"\n--- Processing image {i+1}/{len(test_images)} ---")
            
            try:
                result = process_side_by_side_stereo(img_path, calib_params, output_dir)
                if result is not None:
                    print(f"Successfully processed image {i+1}")
                else:
                    print(f"Failed to process image {i+1}")
            except Exception as e:
                print(f"Error processing image {i+1}: {e}")
                continue
        
        # Analyze results
        analyze_results(output_dir)
        
        print(f"\nCorrected depth estimation complete! Results saved to: {output_dir}")
        
    except Exception as e:
        print(f"Error: {e}")
        return

if __name__ == "__main__":
    main()
