#!/usr/bin/env python3
"""
Diagnostic script to analyze stereo images and calibration
"""

import cv2
import numpy as np
import os
import glob
import matplotlib.pyplot as plt

def analyze_images(image_dir):
    """Analyze the stereo images to understand their structure"""
    image_files = sorted(glob.glob(os.path.join(image_dir, "*.jpg")))
    
    print(f"Found {len(image_files)} images")
    
    # Load first few images to analyze
    for i in range(min(4, len(image_files))):
        img = cv2.imread(image_files[i], cv2.IMREAD_GRAYSCALE)
        if img is not None:
            print(f"Image {i+1}: {os.path.basename(image_files[i])}")
            print(f"  Size: {img.shape}")
            print(f"  Min/Max values: {img.min()}/{img.max()}")
            print(f"  Mean: {img.mean():.2f}")
            
            # Check if this looks like a stereo pair (split image)
            h, w = img.shape
            if w > h:  # Wide image, might be side-by-side stereo
                left_half = img[:, :w//2]
                right_half = img[:, w//2:]
                
                # Compute correlation between halves
                correlation = cv2.matchTemplate(left_half, right_half, cv2.TM_CCOEFF_NORMED)[0][0]
                print(f"  Left/Right correlation: {correlation:.3f}")
                
                # Check for vertical alignment
                left_mean = left_half.mean()
                right_mean = right_half.mean()
                print(f"  Left half mean: {left_mean:.2f}, Right half mean: {right_mean:.2f}")
            print()

def analyze_calibration(calib_path):
    """Analyze calibration parameters"""
    calib_file = os.path.join(calib_path, 'stereo_calibration.npz')
    calib_data = np.load(calib_file)
    
    print("=== CALIBRATION ANALYSIS ===")
    
    # Camera matrices
    K1 = calib_data['camera_matrix_left']
    K2 = calib_data['camera_matrix_right']
    D1 = calib_data['dist_coeffs_left']
    D2 = calib_data['dist_coeffs_right']
    R = calib_data['R']
    T = calib_data['T']
    
    print(f"Left camera matrix:\n{K1}")
    print(f"Right camera matrix:\n{K2}")
    print(f"Left distortion coefficients: {D1}")
    print(f"Right distortion coefficients: {D2}")
    print(f"Rotation matrix:\n{R}")
    print(f"Translation vector: {T.flatten()}")
    
    # Calculate baseline
    baseline = np.linalg.norm(T)
    print(f"Baseline: {baseline:.6f} units")
    
    # Check if this looks reasonable
    if baseline < 0.01:
        print("WARNING: Very small baseline - this might cause issues with depth estimation")
    elif baseline > 1.0:
        print("WARNING: Very large baseline - check units")
    
    # Check focal lengths
    fx1, fy1 = K1[0,0], K1[1,1]
    fx2, fy2 = K2[0,0], K2[1,1]
    print(f"Left focal lengths: fx={fx1:.2f}, fy={fy1:.2f}")
    print(f"Right focal lengths: fx={fx2:.2f}, fy={fy2:.2f}")
    
    # Check principal points
    cx1, cy1 = K1[0,2], K1[1,2]
    cx2, cy2 = K2[0,2], K2[1,2]
    print(f"Left principal point: ({cx1:.2f}, {cy1:.2f})")
    print(f"Right principal point: ({cx2:.2f}, {cy2:.2f})")

def test_stereo_matching(image_dir):
    """Test stereo matching on a sample image pair"""
    image_files = sorted(glob.glob(os.path.join(image_dir, "*.jpg")))
    
    if len(image_files) < 2:
        print("Need at least 2 images for stereo matching test")
        return
    
    # Load first two images
    img1 = cv2.imread(image_files[0], cv2.IMREAD_GRAYSCALE)
    img2 = cv2.imread(image_files[1], cv2.IMREAD_GRAYSCALE)
    
    print(f"\n=== STEREO MATCHING TEST ===")
    print(f"Image 1: {os.path.basename(image_files[0])}")
    print(f"Image 2: {os.path.basename(image_files[1])}")
    
    # Check if images are side-by-side stereo
    h, w = img1.shape
    if w > h and w > 2000:  # Likely side-by-side stereo
        print("Detected side-by-side stereo format")
        
        # Split into left and right
        left_img = img1[:, :w//2]
        right_img = img1[:, w//2:]
        
        print(f"Split image size: {left_img.shape}")
        
        # Try simple stereo matching
        stereo = cv2.StereoBM_create(numDisparities=64, blockSize=15)
        disparity = stereo.compute(left_img, right_img)
        
        print(f"Disparity range: {disparity.min()} to {disparity.max()}")
        print(f"Non-zero disparities: {np.count_nonzero(disparity)}")
        
        # Save disparity for inspection
        disparity_norm = cv2.normalize(disparity, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)
        cv2.imwrite('/home/jetson1/maurice-prod/test_disparity.jpg', disparity_norm)
        print("Saved test disparity map to test_disparity.jpg")
        
    else:
        print("Images don't appear to be side-by-side stereo")
        
        # Try treating as separate stereo pair
        print("Trying as separate stereo pair...")
        stereo = cv2.StereoBM_create(numDisparities=64, blockSize=15)
        disparity = stereo.compute(img1, img2)
        
        print(f"Disparity range: {disparity.min()} to {disparity.max()}")
        print(f"Non-zero disparities: {np.count_nonzero(disparity)}")

def main():
    """Main diagnostic function"""
    calib_path = "/home/jetson1/maurice-prod/calib_out_simple"
    image_dir = "/home/jetson1/maurice-prod/calibration_frames_new"
    
    print("=== STEREO DEPTH ESTIMATION DIAGNOSTICS ===\n")
    
    # Analyze images
    print("1. ANALYZING IMAGES")
    analyze_images(image_dir)
    
    # Analyze calibration
    print("2. ANALYZING CALIBRATION")
    analyze_calibration(calib_path)
    
    # Test stereo matching
    print("3. TESTING STEREO MATCHING")
    test_stereo_matching(image_dir)

if __name__ == "__main__":
    main()


