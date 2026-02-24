#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
import numpy as np
import matplotlib.pyplot as plt
import cv2
import h5py
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from skimage.metrics import structural_similarity as ssim

class ImageCapture(Node):
    def __init__(self):
        super().__init__('image_capture_node')
        self.get_logger().info("Image capture node started.")
        self.bridge = CvBridge()
        self.image_size = (640, 480)
        
        # Variables to hold the first captured live images
        self.first_live_image1 = None
        self.first_live_image2 = None
        self.images_captured = False
        
        # Load HDF5 images first
        self.hdf5_image1, self.hdf5_image2 = self.load_hdf5_images()
        
        # Set up sensor QoS profile (same as policy.py)
        image_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        
        # Subscribers for images with sensor QoS (same topics as policy.py)
        self.create_subscription(Image, '/mars/main_camera/left/image_raw', self.image1_callback, image_qos)
        self.create_subscription(Image, '/mars/arm/image_raw', self.image2_callback, image_qos)
        
        self.get_logger().info("Waiting for first images from both live cameras...")
    
    def load_hdf5_images(self):
        """Load first frames from HDF5 dataset"""
        file_path = "/home/vignesh/maurice-prod/data/TestData/episode_0.h5"
        try:
            with h5py.File(file_path, 'r') as f:
                # Extract first frames from both cameras
                hdf5_cam1 = np.array(f['observations/images/camera_1'][0])
                hdf5_cam2 = np.array(f['observations/images/camera_2'][0])
                
                self.get_logger().info(f"HDF5 Camera 1 shape: {hdf5_cam1.shape}")
                self.get_logger().info(f"HDF5 Camera 2 shape: {hdf5_cam2.shape}")
                
                return hdf5_cam1, hdf5_cam2
                
        except Exception as e:
            self.get_logger().error(f"Error loading HDF5 images: {e}")
            return None, None
    
    def image1_callback(self, msg: Image):
        """Same callback as policy.py"""
        if self.first_live_image1 is None:
            try:
                self.first_live_image1 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
                self.get_logger().info("First live image from camera 1 captured!")
                self.check_and_process_images()
            except Exception as e:
                self.get_logger().error(f"Error converting live image1: {e}")
    
    def image2_callback(self, msg: Image):
        """Same callback as policy.py"""
        if self.first_live_image2 is None:
            try:
                self.first_live_image2 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
                self.get_logger().info("First live image from camera 2 captured!")
                self.check_and_process_images()
            except Exception as e:
                self.get_logger().error(f"Error converting live image2: {e}")
    
    def check_and_process_images(self):
        """Process images once both live images are captured"""
        if (self.first_live_image1 is not None and 
            self.first_live_image2 is not None and 
            not self.images_captured and
            self.hdf5_image1 is not None and
            self.hdf5_image2 is not None):
            
            self.images_captured = True
            self.process_and_display_all_images()
    
    def process_and_display_all_images(self):
        """Process and display all 4 images with comparisons"""
        try:
            # Process HDF5 images (resize to match live images)
            hdf5_img1_resized = cv2.resize(self.hdf5_image1, self.image_size)
            hdf5_img2_resized = cv2.resize(self.hdf5_image2, self.image_size)
            
            # Process live images same way as policy.py
            live_img1 = cv2.resize(self.first_live_image1, self.image_size)
            live_img2 = cv2.resize(self.first_live_image2, self.image_size)
            
            # Convert all to float32 and normalize for metrics
            hdf5_img1_float = hdf5_img1_resized.astype(np.float32) / 255.0
            hdf5_img2_float = hdf5_img2_resized.astype(np.float32) / 255.0
            live_img1_float = live_img1.astype(np.float32) / 255.0
            live_img2_float = live_img2.astype(np.float32) / 255.0
            
            # Print image information
            print("Image Information:")
            print("=" * 50)
            print(f"HDF5 Camera 1 - Shape: {hdf5_img1_resized.shape}, Range: {hdf5_img1_resized.min()}-{hdf5_img1_resized.max()}")
            print(f"HDF5 Camera 2 - Shape: {hdf5_img2_resized.shape}, Range: {hdf5_img2_resized.min()}-{hdf5_img2_resized.max()}")
            print(f"Live Camera 1 - Shape: {live_img1.shape}, Range: {live_img1.min()}-{live_img1.max()}")
            print(f"Live Camera 2 - Shape: {live_img2.shape}, Range: {live_img2.min()}-{live_img2.max()}")
            print()
            
            # Compute distance metrics for corresponding pairs
            print("Camera 1 Comparison (HDF5 vs Live):")
            self.compute_distance_metrics(hdf5_img1_float, live_img1_float)
            
            print("Camera 2 Comparison (HDF5 vs Live):")
            self.compute_distance_metrics(hdf5_img2_float, live_img2_float)
            
            # Display all 4 images in 2x2 grid
            fig, axes = plt.subplots(2, 2, figsize=(15, 12))
            
            # Convert BGR to RGB and normalize to 0-1 range for matplotlib
            hdf5_img1_rgb = cv2.cvtColor(hdf5_img1_resized, cv2.COLOR_BGR2RGB) / 255.0
            hdf5_img2_rgb = cv2.cvtColor(hdf5_img2_resized, cv2.COLOR_BGR2RGB) / 255.0
            live_img1_rgb = cv2.cvtColor(live_img1, cv2.COLOR_BGR2RGB) / 255.0
            live_img2_rgb = cv2.cvtColor(live_img2, cv2.COLOR_BGR2RGB) / 255.0
            
            # Top row: HDF5 images
            axes[0, 0].imshow(hdf5_img1_rgb)
            axes[0, 0].set_title('HDF5 Camera 1 (First Frame)')
            axes[0, 0].axis('off')
            
            axes[0, 1].imshow(hdf5_img2_rgb)
            axes[0, 1].set_title('HDF5 Camera 2 (First Frame)')
            axes[0, 1].axis('off')
            
            # Bottom row: Live images
            axes[1, 0].imshow(live_img1_rgb)
            axes[1, 0].set_title('Live Camera 1 (/mars/main_camera/left/image_raw)')
            axes[1, 0].axis('off')
            
            axes[1, 1].imshow(live_img2_rgb)
            axes[1, 1].set_title('Live Camera 2 (/mars/arm/image_raw)')
            axes[1, 1].axis('off')
            
            plt.tight_layout()
            plt.show()
            
            self.get_logger().info("All 4 images processed and displayed successfully!")
            
        except Exception as e:
            self.get_logger().error(f"Error processing images: {e}")
    
    def compute_distance_metrics(self, img1, img2):
        """Compute various distance metrics between two images"""
        print("=" * 40)
        
        # 1. Mean Squared Error (MSE)
        mse = np.mean((img1 - img2) ** 2)
        print(f"Mean Squared Error (MSE): {mse:.6f}")
        
        # 2. Root Mean Squared Error (RMSE)
        rmse = np.sqrt(mse)
        print(f"Root Mean Squared Error (RMSE): {rmse:.6f}")
        
        # 3. Mean Absolute Error (MAE)
        mae = np.mean(np.abs(img1 - img2))
        print(f"Mean Absolute Error (MAE): {mae:.6f}")
        
        # 4. Structural Similarity Index (SSIM)
        # Convert to grayscale for SSIM
        img1_gray = cv2.cvtColor((img1 * 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
        img2_gray = cv2.cvtColor((img2 * 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
        ssim_value = ssim(img1_gray, img2_gray)
        print(f"Structural Similarity Index (SSIM): {ssim_value:.6f}")
        
        # 5. Peak Signal-to-Noise Ratio (PSNR)
        if mse > 0:
            psnr = 20 * np.log10(1.0 / np.sqrt(mse))
            print(f"Peak Signal-to-Noise Ratio (PSNR): {psnr:.2f} dB")
        else:
            print("Peak Signal-to-Noise Ratio (PSNR): Infinite (identical images)")
        
        # 6. Histogram correlation
        hist1 = cv2.calcHist([img1_gray], [0], None, [256], [0, 256])
        hist2 = cv2.calcHist([img2_gray], [0], None, [256], [0, 256])
        hist_corr = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
        print(f"Histogram Correlation: {hist_corr:.6f}")
        
        print("=" * 40)
        print()

def main(args=None):
    rclpy.init(args=args)
    node = ImageCapture()
    
    try:
        # Spin until both live images are captured
        while not node.images_captured and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.1)
        
        # Keep node alive briefly to allow processing
        if node.images_captured:
            node.get_logger().info("All images captured and processed!")
        
    except KeyboardInterrupt:
        node.get_logger().info("Image capture node shutting down.")
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == "__main__":
    main()
